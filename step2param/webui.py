"""Web UI for step2param (STEP -> parametric FreeCAD model).

Runs the existing CLI as a subprocess (the pipeline itself is never
modified) and adds a browser front end:

- pick a STEP file from the repo's step/ folder, or upload one
- 3D preview of the original STEP (Three.js)
- run the parametric conversion, streaming the log
- 3D comparison: original vs built model, side-by-side / overlay /
  diff (green/yellow/red by deviation from the original surface)
- visual report outputs (features.png, report.html, per-iteration
  snapshots) and downloads for model.step / model.fcstd
- browse past runs from the out/ folder without re-running

Launch:  python -m step2param.webui   (or run_webui.bat on Windows)
Then open http://127.0.0.1:8092
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STEP_DIR = REPO_ROOT / "step"
OUT_DIR = REPO_ROOT / "out"
JOBS_DIR = REPO_ROOT / "webui-jobs"
WEBUI_HTML = Path(__file__).resolve().parent / "webui.html"

HOST = "127.0.0.1"
PORT = 8092

# Job statuses.
ST_IDLE = "idle"        # uploaded/picked, not run yet
ST_RUNNING = "running"
ST_DONE = "done"
ST_FAILED = "failed"

_jobs = {}          # job_id -> job dict (in-memory registry)
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Job storage
# ---------------------------------------------------------------------------

def _new_job_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def _job_dir(job_id):
    return JOBS_DIR / job_id


def _save_job(job):
    d = _job_dir(job["job_id"])
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "job.json", "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2)


def _load_job(job_id):
    with _jobs_lock:
        if job_id in _jobs:
            return _jobs[job_id]
    p = _job_dir(job_id) / "job.json"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        job = json.load(f)
    with _jobs_lock:
        _jobs[job_id] = job
    return job


def _register_job(job):
    with _jobs_lock:
        _jobs[job["job_id"]] = job
    _save_job(job)
    return job


def _log_path(job):
    return _job_dir(job["job_id"]) / "log.txt"


def _append_log(job, text):
    try:
        with open(_log_path(job), "a", encoding="utf-8",
                  errors="replace") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
    except OSError:
        pass


def _read_log(job, tail_lines=None):
    p = _log_path(job)
    if not p.exists():
        return ""
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    if tail_lines is not None:
        lines = lines[-tail_lines:]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# freecadcmd resolution
# ---------------------------------------------------------------------------

def default_freecadcmd():
    """Path remembered by setup.bat (.venv/freecadcmd.txt), else the
    plain command name (resolved from PATH)."""
    txt = REPO_ROOT / ".venv" / "freecadcmd.txt"
    if txt.exists():
        try:
            p = txt.read_text(encoding="utf-8").strip()
            if p:
                return p, "saved by setup.bat"
        except OSError:
            pass
    return "freecadcmd", "from PATH"


# ---------------------------------------------------------------------------
# features.json parsing (verdict + metrics)
# ---------------------------------------------------------------------------

def _parse_metrics(fl):
    """Mirror of _iteration_metrics in step2param.py: one number per
    run (volume error %, CT mean, iso mean, failing slices, deficit)."""
    vol_pct = None
    for c in fl.get("verify") or []:
        if c.get("check") == "volume":
            m = re.search(r"\(([\d.]+)%\)", c.get("detail", ""))
            if m:
                vol_pct = float(m.group(1))
    ct_mean = None
    n_fail = 0
    for c in fl.get("verify_ct") or []:
        if c.get("check") == "ct_mean":
            m = re.search(r"mean slice IoU ([\d.]+)", c.get("detail", ""))
            if m:
                ct_mean = float(m.group(1))
        elif c.get("check", "").startswith("ct_z") and not c.get("ok"):
            n_fail += 1
    iso_mean = None
    for c in fl.get("verify_iso") or []:
        if c.get("check") == "iso_mean":
            m = re.search(r"mean silhouette IoU ([\d.]+)",
                          c.get("detail", ""))
            if m:
                iso_mean = float(m.group(1))
    deficit = 0.0
    if vol_pct is not None:
        deficit += max(0.0, vol_pct - 0.5)
    if ct_mean is not None:
        deficit += max(0.0, 0.90 - ct_mean) * 100.0
    if iso_mean is not None:
        deficit += max(0.0, 0.90 - iso_mean) * 100.0
    return {"volume_pct": vol_pct, "ct_mean": ct_mean,
            "iso_mean": iso_mean, "n_fail_slices": n_fail,
            "deficit": round(deficit, 4)}


def _read_features(out_dir):
    p = Path(out_dir) / "features.json"
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _results_summary(job):
    """Verdict + metrics + file inventory for a job's out dir."""
    out_dir = Path(job["out_dir"])
    fl = _read_features(out_dir)
    files = {}
    for name in ("features.png", "report.html", "model.step",
                 "model.fcstd", "features.json", "build_macro.py"):
        if (out_dir / name).exists():
            files[name] = True
    iso_dir = out_dir / "iso_check_built"
    if iso_dir.is_dir():
        for img in sorted(iso_dir.glob("*.png")):
            files["iso_check_built/" + img.name] = True
    iterations = []
    if out_dir.is_dir():
        for idir in sorted(out_dir.glob("iter_*")):
            it = {"name": idir.name}
            if (idir / "model.step").exists():
                it["model_step"] = True
            iso = idir / "iso"
            if iso.is_dir():
                it["iso_images"] = [p.name for p in
                                    sorted(iso.glob("*.png"))]
            iterations.append(it)
    summary = {"files": files, "iterations": iterations,
               "metrics": None, "verdict": None, "message": None,
               "best_iteration": None, "failures": []}
    if fl is not None:
        summary["metrics"] = _parse_metrics(fl)
        rr = fl.get("refine_report") or {}
        summary["verdict"] = rr.get("status")
        summary["message"] = rr.get("message")
        summary["best_iteration"] = rr.get("best_iteration")
        summary["failures"] = rr.get("failures") or []
        summary["n_features"] = len(fl.get("features") or [])
        summary["volume_mm3"] = fl.get("volume_mm3")
    return summary


def _job_status(job):
    """Status of a job: running jobs are tracked live; finished jobs
    read their verdict from features.json."""
    if job.get("status") == ST_RUNNING:
        return ST_RUNNING
    if job.get("status") == ST_FAILED:
        return ST_FAILED
    summary = _results_summary(job)
    if summary["verdict"]:
        return ST_DONE
    return job.get("status", ST_IDLE)


def _job_public(job):
    """JSON-safe view of a job for the frontend."""
    summary = _results_summary(job)
    return {
        "job_id": job["job_id"],
        "kind": job.get("kind"),
        "name": job.get("name"),
        "step_name": job.get("step_name"),
        "has_hlr": bool(job.get("hlr_path")),
        "datum": job.get("datum"),
        "bbox": job.get("bbox"),
        "status": _job_status(job),
        "created": job.get("created"),
        "out_dir_name": Path(job["out_dir"]).name,
        "original_available": bool(job.get("step_path")
                                   and Path(job["step_path"]).exists()),
        "log": _read_log(job, tail_lines=200),
        "results": summary,
    }


# ---------------------------------------------------------------------------
# Mesh handling (cached per job)
# ---------------------------------------------------------------------------

def _mesh_cache_path(job, which):
    return _job_dir(job["job_id"]) / ("mesh_%s.json" % which)


def _cached_mesh(job, which):
    p = _mesh_cache_path(job, which)
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    return None


def _store_mesh(job, which, data):
    p = _mesh_cache_path(job, which)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _original_mesh(job):
    """Original mesh in DATUM coordinates (translated by -datum so it
    overlays the built model)."""
    cached = _cached_mesh(job, "original")
    if cached is not None:
        return cached
    from .mesh_export import step_to_mesh
    datum = job.get("datum") or [0.0, 0.0, 0.0]
    tr = [-float(v) for v in datum]
    data = step_to_mesh(job["step_path"], translate=tr)
    _store_mesh(job, "original", data)
    return data


def _built_mesh(job):
    """Built model mesh (model.step is already in datum coords)."""
    cached = _cached_mesh(job, "built")
    if cached is not None:
        return cached
    from .mesh_export import step_to_mesh
    built = Path(job["out_dir"]) / "model.step"
    if not built.exists():
        return None
    data = step_to_mesh(str(built))
    _store_mesh(job, "built", data)
    return data


def _diff_mesh(job):
    """Built mesh with per-vertex diff colours + histogram."""
    cached = _cached_mesh(job, "diff")
    if cached is not None:
        return cached
    from .mesh_export import diff_mesh
    built = Path(job["out_dir"]) / "model.step"
    if not built.exists() or not job.get("step_path"):
        return None
    datum = job.get("datum") or [0.0, 0.0, 0.0]
    data = diff_mesh(str(built), job["step_path"], datum)
    _store_mesh(job, "diff", data)
    return data


def _compute_original_info(step_path):
    """Datum + bbox of a STEP file (same datum recognize() uses)."""
    from .mesh_export import step_datum, read_shape, shape_bbox
    shape = read_shape(step_path)
    return {"datum": step_datum(step_path),
            "bbox": [round(float(v), 4) for v in shape_bbox(shape)]}


# ---------------------------------------------------------------------------
# Pipeline run (background thread, streams the log)
# ---------------------------------------------------------------------------

def _run_pipeline(job, options):
    """Run the CLI as a subprocess. Never modifies the pipeline."""
    out_dir = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    freecadcmd = options.get("freecadcmd") or default_freecadcmd()[0]
    cmd = [sys.executable, "-m", "step2param.step2param",
           "--step", job["step_path"],
           "--out", str(out_dir),
           "--refine-iter", str(int(options.get("refine_iter", 3))),
           "--freecadcmd", freecadcmd]
    if options.get("iso", True):
        cmd.append("--iso")
    if options.get("ct_coarse_n"):
        cmd += ["--ct-coarse-n", str(int(options["ct_coarse_n"]))]
    if options.get("ct_coarse_spacing"):
        cmd += ["--ct-coarse-spacing",
                str(float(options["ct_coarse_spacing"]))]
    if options.get("ct_dense_spacing"):
        cmd += ["--ct-dense-spacing",
                str(float(options["ct_dense_spacing"]))]
    if job.get("hlr_path"):
        cmd += ["--hlr", job["hlr_path"]]
    _append_log(job, "Running: %s\n" % " ".join(cmd))
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(REPO_ROOT), env=env)
    except OSError as e:
        _append_log(job, "[ERROR] could not start the pipeline: %s\n" % e)
        job["status"] = ST_FAILED
        _save_job(job)
        return
    for line in proc.stdout:
        _append_log(job, line)
    proc.wait()
    # A new model was built: drop the cached built/diff meshes.
    for which in ("built", "diff"):
        p = _mesh_cache_path(job, which)
        try:
            p.unlink()
        except OSError:
            pass
    if proc.returncode == 0:
        job["status"] = ST_DONE
        _append_log(job, "\n[DONE] pipeline finished (exit 0).\n")
    else:
        job["status"] = ST_FAILED
        _append_log(job, "\n[FAILED] pipeline exited with code %d.\n"
                    % proc.returncode)
    _save_job(job)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

def create_app():
    from fastapi import FastAPI, File, UploadFile, Request
    from fastapi.responses import FileResponse, JSONResponse

    app = FastAPI(title="step2param web UI")

    STEP_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)

    @app.get("/")
    def index():
        return FileResponse(WEBUI_HTML, media_type="text/html")

    @app.get("/api/config")
    def config():
        fc, src = default_freecadcmd()
        return {"freecadcmd": fc, "freecadcmd_source": src,
                "freecadcmd_exists": bool(shutil.which(fc))
                or Path(fc).exists()}

    # --- STEP folder -------------------------------------------------
    @app.get("/api/step-files")
    def step_files(dir: str = ""):
        folder = Path(dir) if dir else STEP_DIR
        if not folder.is_dir():
            return {"folder": str(folder), "files": []}
        out = []
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.suffix.lower() in (".step", ".stp"):
                st = p.stat()
                out.append({"name": p.name, "size": st.st_size,
                            "mtime": time.strftime("%Y-%m-%d %H:%M",
                                                   time.localtime(st.st_mtime))})
        return {"folder": str(folder), "files": out}

    # --- Results folders ----------------------------------------------
    @app.get("/api/results")
    def results():
        out = []
        if OUT_DIR.is_dir():
            for p in sorted(OUT_DIR.iterdir(), key=lambda q: q.stat().st_mtime,
                            reverse=True):
                if not p.is_dir():
                    continue
                st = p.stat()
                entry = {"name": p.name,
                         "mtime": time.strftime("%Y-%m-%d %H:%M",
                                                time.localtime(st.st_mtime)),
                         "has_model": (p / "model.step").exists(),
                         "has_features": (p / "features.json").exists(),
                         "status": None}
                fl = _read_features(p)
                if fl is not None:
                    rr = fl.get("refine_report") or {}
                    entry["status"] = rr.get("status")
                    entry["best_iteration"] = rr.get("best_iteration")
                # A live job writing into this folder means running.
                with _jobs_lock:
                    for j in _jobs.values():
                        if (j.get("status") == ST_RUNNING
                                and Path(j["out_dir"]) == p):
                            entry["status"] = ST_RUNNING
                out.append(entry)
        return {"folder": str(OUT_DIR), "results": out}

    # --- Upload -------------------------------------------------------
    @app.post("/api/upload")
    def upload(step: UploadFile = File(...),
               hlr: UploadFile = File(None)):
        job_id = _new_job_id()
        jdir = _job_dir(job_id)
        jdir.mkdir(parents=True, exist_ok=True)
        step_path = jdir / step.filename
        with open(step_path, "wb") as f:
            shutil.copyfileobj(step.file, f)
        hlr_path = None
        if hlr is not None and hlr.filename:
            hlr_path = jdir / hlr.filename
            with open(hlr_path, "wb") as f:
                shutil.copyfileobj(hlr.file, f)
        try:
            info = _compute_original_info(str(step_path))
        except Exception as e:
            return JSONResponse(
                {"error": "could not read the STEP file: %s" % e},
                status_code=400)
        job = {"job_id": job_id, "kind": "upload",
               "name": Path(step.filename).stem,
               "step_name": step.filename,
               "step_path": str(step_path),
               "hlr_path": str(hlr_path) if hlr_path else None,
               "out_dir": str(OUT_DIR / Path(step.filename).stem),
               "datum": info["datum"], "bbox": info["bbox"],
               "status": ST_IDLE,
               "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        _register_job(job)
        _append_log(job, "Uploaded %s (datum %s).\n"
                    % (step.filename, info["datum"]))
        return _job_public(job)

    # --- Pick from the step/ folder -----------------------------------
    @app.post("/api/load-step")
    async def load_step(request: Request):
        body = await request.json()
        name = body.get("name", "")
        folder = Path(body.get("dir", "")) if body.get("dir") else STEP_DIR
        step_path = folder / name
        if not step_path.is_file():
            return JSONResponse({"error": "STEP file not found: %s" % name},
                                status_code=404)
        job_id = _new_job_id()
        jdir = _job_dir(job_id)
        jdir.mkdir(parents=True, exist_ok=True)
        try:
            info = _compute_original_info(str(step_path))
        except Exception as e:
            return JSONResponse(
                {"error": "could not read the STEP file: %s" % e},
                status_code=400)
        job = {"job_id": job_id, "kind": "step",
               "name": step_path.stem,
               "step_name": step_path.name,
               "step_path": str(step_path),
               "hlr_path": None,
               "out_dir": str(OUT_DIR / step_path.stem),
               "datum": info["datum"], "bbox": info["bbox"],
               "status": ST_IDLE,
               "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        _register_job(job)
        _append_log(job, "Picked %s from %s (datum %s).\n"
                    % (step_path.name, folder, info["datum"]))
        return _job_public(job)

    # --- Load a past run from out/<part>/ ----------------------------
    @app.post("/api/load-result")
    async def load_result(request: Request):
        body = await request.json()
        name = body.get("name", "")
        out_dir = OUT_DIR / name
        if not out_dir.is_dir():
            return JSONResponse({"error": "result folder not found: %s" % name},
                                status_code=404)
        fl = _read_features(out_dir)
        if fl is None:
            return JSONResponse(
                {"error": "no features.json in %s (run not finished?)" % name},
                status_code=400)
        # Original STEP: features.json stores its basename; look in
        # the step/ folder first.
        step_path = None
        datum = (fl.get("datum") or {}).get("origin")
        orig_name = fl.get("step")
        if orig_name:
            cand = STEP_DIR / orig_name
            if cand.is_file():
                step_path = str(cand)
        if step_path is None:
            # Fall back to the datum + bbox from features.json.
            datum = datum or [0.0, 0.0, 0.0]
        else:
            try:
                info = _compute_original_info(step_path)
                datum = info["datum"]
            except Exception:
                step_path = None
        job_id = _new_job_id()
        jdir = _job_dir(job_id)
        jdir.mkdir(parents=True, exist_ok=True)
        job = {"job_id": job_id, "kind": "result",
               "name": name,
               "step_name": orig_name,
               "step_path": step_path,
               "hlr_path": None,
               "out_dir": str(out_dir),
               "datum": datum, "bbox": fl.get("bbox"),
               "status": ST_DONE if (fl.get("refine_report")) else ST_IDLE,
               "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        _register_job(job)
        _append_log(job, "Loaded past run %s.\n" % name)
        return _job_public(job)

    # --- Run ----------------------------------------------------------
    @app.post("/api/run/{job_id}")
    async def run_job(job_id: str, request: Request):
        job = _load_job(job_id)
        if job is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        if job.get("status") == ST_RUNNING:
            return JSONResponse({"error": "job already running"},
                                status_code=409)
        if not job.get("step_path"):
            return JSONResponse({"error": "no STEP file for this job"},
                                status_code=400)
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        job["status"] = ST_RUNNING
        _save_job(job)
        _append_log(job, "\n--- run started %s ---\n"
                    % time.strftime("%Y-%m-%d %H:%M:%S"))
        t = threading.Thread(target=_run_pipeline, args=(job, body),
                             daemon=True)
        t.start()
        return {"job_id": job_id, "status": ST_RUNNING}

    # --- Job status / log ---------------------------------------------
    @app.get("/api/job/{job_id}")
    def get_job(job_id: str):
        job = _load_job(job_id)
        if job is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        return _job_public(job)

    @app.get("/api/job/{job_id}/log")
    def get_log(job_id: str):
        job = _load_job(job_id)
        if job is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(_read_log(job))

    # --- Meshes --------------------------------------------------------
    @app.get("/api/job/{job_id}/mesh/original")
    def mesh_original(job_id: str):
        job = _load_job(job_id)
        if job is None or not job.get("step_path"):
            return JSONResponse({"error": "no original STEP"},
                                status_code=404)
        try:
            return _original_mesh(job)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/job/{job_id}/mesh/built")
    def mesh_built(job_id: str):
        job = _load_job(job_id)
        if job is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        try:
            data = _built_mesh(job)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        if data is None:
            return JSONResponse({"error": "no built model yet"},
                                status_code=404)
        return data

    @app.get("/api/job/{job_id}/mesh/diff")
    def mesh_diff(job_id: str):
        job = _load_job(job_id)
        if job is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        try:
            data = _diff_mesh(job)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        if data is None:
            return JSONResponse(
                {"error": "diff needs a built model and the original STEP"},
                status_code=404)
        return data

    # --- Result files --------------------------------------------------
    @app.get("/api/job/{job_id}/file/{rel_path:path}")
    def job_file(job_id: str, rel_path: str):
        job = _load_job(job_id)
        if job is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        out_dir = Path(job["out_dir"]).resolve()
        target = (out_dir / rel_path).resolve()
        if not str(target).startswith(str(out_dir)):
            return JSONResponse({"error": "path not allowed"},
                                status_code=403)
        if not target.is_file():
            return JSONResponse({"error": "file not found: %s" % rel_path},
                                status_code=404)
        import mimetypes
        mt, _ = mimetypes.guess_type(str(target))
        return FileResponse(target, media_type=mt or
                            "application/octet-stream")

    return app


app = create_app()


def main():
    import uvicorn
    print("step2param web UI: http://%s:%d" % (HOST, PORT))
    print("repo: %s" % REPO_ROOT)
    print("STEP folder: %s" % STEP_DIR)
    print("results folder: %s" % OUT_DIR)
    fc, src = default_freecadcmd()
    print("freecadcmd: %s (%s)" % (fc, src))
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
