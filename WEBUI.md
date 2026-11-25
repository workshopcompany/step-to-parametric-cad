# step2param web UI

Browser interface for the step2param program (STEP -> parametric
FreeCAD model). The web UI is a thin wrapper: it runs the existing
CLI (`python -m step2param.step2param`) as a subprocess and shows
its inputs and outputs. The pipeline itself is not changed.

## Run it (Windows)

```bat
setup.bat        :: once: creates .venv, installs requirements, finds FreeCAD
run_webui.bat    :: starts the server and opens http://127.0.0.1:8092
```

Or from any terminal in the repo root:

```bash
python -m step2param.webui
```

The server listens on `127.0.0.1:8092` (this machine only). Keep the
terminal window open while you use it; Ctrl+C stops the server.

## What you can do in the UI

1. **Pick a part** — drop `.step` / `.stp` files into the `step/`
   folder and choose one from the dropdown (the **Refresh** button
   re-scans the folder while the server runs), or upload a file.
   An optional HLR `.json` (2D drawing) can be uploaded with it
   for the cross-check. The part appears in 3D immediately — drag
   to rotate, scroll to zoom.
2. **Run the conversion** — set the refinement iterations and
   whether to write isometric views, then press **Run parametric
   conversion**. The pipeline log streams live. The FreeCAD path
   defaults to the one `setup.bat` saved in `.venv/freecadcmd.txt`.
3. **Compare in 3D** — after a run, the viewer shows the original
   and the built model with five modes:
   - **Original** — the STEP as uploaded
   - **Built** — the parametric model (`model.step`)
   - **Side-by-side** — both, next to each other
   - **Overlay** — both in the same place, built semi-transparent
     (opacity slider)
   - **Diff** — the built model coloured by deviation from the
     original surface: green ≤ 0.05 mm, yellow ≤ 0.30 mm,
     red > 0.30 mm, with a histogram (share within 0.05 / 0.1 /
     0.3 / 1.0 mm, mean and max deviation)
4. **Results** — verdict (PASS / NEEDS DECISION / NO IMPROVEMENT),
   volume / iso / CT metrics, download links for `model.step` and
   `model.fcstd`, the recognized-features image, the visual report
   (`report.html`, embedded and also openable in its own window),
   and every iteration's preview + `model.step`.
5. **Past runs** — the dropdown lists the `out/` folders. Picking
   one loads that run's model, report, verdict and 3D comparison
   without re-running it. If a run has no `model.step` yet (still
   running or failed), its status/log is shown instead.

## How the 3D + diff work

Browsers cannot read STEP files, so the server converts each shape
to a triangle mesh first (`step2param/mesh_export.py`, OCP
tessellation, deflection 0.05 mm) and sends compact JSON
(`{vertices, triangles, bbox}`) to the Three.js viewer.

The built model is in **datum coordinates** (origin = part bbox
centre X/Y + Z min). The original mesh is translated by `-datum`
before display and comparison, so the two shapes overlay exactly.

The diff measures, for every vertex of the built mesh, the distance
to the original mesh surface (uniform-grid nearest-triangle search
in numpy; the search radius grows until a triangle is found, up to
5 mm). Colours: green ≤ 0.05 mm, yellow ≤ 0.30 mm, red above, with
a smooth gradient between the stops. Very large meshes are
decimated to 40000 query vertices for the search.

## API endpoints

| Endpoint | What it does |
|---|---|
| `GET /` | the web UI page |
| `GET /api/config` | freecadcmd path + whether it exists |
| `GET /api/step-files[?dir=]` | `.step`/`.stp` files in `step/` (or a custom folder) |
| `GET /api/results` | past runs in `out/` with status |
| `POST /api/upload` | multipart: `step` + optional `hlr` → new job |
| `POST /api/load-step` | `{"name"}` → new job from the `step/` folder |
| `POST /api/load-result` | `{"name"}` → job from a past run in `out/` |
| `POST /api/run/<job_id>` | `{"refine_iter","iso","ct_coarse_n","ct_coarse_spacing","ct_dense_spacing","freecadcmd"}` → starts the pipeline |
| `GET /api/job/<job_id>` | status, log tail, verdict, metrics, file list |
| `GET /api/job/<job_id>/log` | full log (plain text) |
| `GET /api/job/<job_id>/mesh/original` | original mesh JSON (datum-translated) |
| `GET /api/job/<job_id>/mesh/built` | built model mesh JSON |
| `GET /api/job/<job_id>/mesh/diff` | built mesh + per-vertex diff colours + histogram |
| `GET /api/job/<job_id>/file/<path>` | a result file from the job's out dir (`report.html`, `features.png`, `model.step`, `model.fcstd`, `iter_<n>/…`) |

Jobs live under `webui-jobs/<job_id>/` (uploads, `log.txt`,
cached meshes, and the pipeline `out/` for new runs). Past runs
are read directly from `out/<part>/`.

## Files

- `step2param/webui.py` — FastAPI server (all endpoints above)
- `step2param/webui.html` — the page (Three.js 0.160.0 from CDN)
- `step2param/mesh_export.py` — STEP → JSON mesh + diff colours
- `run_webui.bat` — Windows launcher
- `step/` — drop STEP files here (ignored by git except `.gitkeep`)
- `webui-jobs/` — per-job working folders (ignored by git)

The Three.js viewer loads from the jsDelivr CDN, so the browser
needs internet access. Everything else runs locally.
