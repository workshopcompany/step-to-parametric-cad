# step2param

STEP + 2D drawing → parametric FreeCAD model.

You make the 2D drawing (HLR) from the STEP. This program takes the
2D drawing + the STEP and builds a parametric FreeCAD (`.fcstd`) model.
Nobody builds the model by hand — the program does it.

## What it does

1. **Recognize** — reads the exact geometry from the STEP (OCP).
   Outline-based, not type-matching: the program traces the wire of
   every planar Z-normal face and uses it as the sketch profile,
   exactly as it is. A circle stays a circle, a 12-gon stays 12
   lines, a D-cut stays an arc + a line.
2. **Cross-check** (optional) — every feature must appear in the 2D
   drawing views. The drawing is the human-readable spec; the STEP
   is the exact-geometry source.
3. **Build** — generates a FreeCAD PartDesign model (`.fcstd` +
   `.step`) as a macro and runs it under `freecadcmd`.
4. **Verify** — compares the built model against the original STEP:
   volume, bounding box, face count, iso-silhouette IoU, and
   adaptive CT slice IoU.
5. **Refine (iterative loop)** — verify, find the parts NOT drawn,
   re-measure the original in the failing regions and correct the
   features, rebuild and re-verify. See `PIPELINE.md` for the loop
   policy (3 automatic refinements, then it asks you).

## Requirements

- Python 3.10+ with the `OCP` package (build123d's OpenCascade
  bindings) for STEP reading and sectioning.
- FreeCAD 1.x (`freecadcmd`) for building the parametric model.
- `Pillow`, `matplotlib`, `numpy` for the iso/CT rendering.

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Recognize + build + verify (one pass, no refinement)
python -m step2param.step2param --step part.step --out out/ \
    --freecadcmd /path/to/freecadcmd

# With the 2D drawing cross-check and adaptive CT
python -m step2param.step2param --step part.step --hlr drawing.json \
    --out out/ --ct --iso --freecadcmd /path/to/freecadcmd

# Iterative refinement (default: up to 3 automatic refinements)
python -m step2param.step2param --step part.step --out out/ --ct \
    --refine-iter 3 --freecadcmd /path/to/freecadcmd
```

Options:

- `--ct` — adaptive CT scan: coarse slices across the whole part
  (by count `--ct-coarse-n`, default 25, or by spacing
  `--ct-coarse-spacing`, e.g. 2.0 mm) + dense slices at
  `--ct-dense-spacing` (default 0.2 mm) only where the section
  changes.
- `--iso` — 4 isometric views + montage, for silhouette comparison.
- `--refine-iter N` — automatic refinement budget (default 3).
  `0` runs once with no refinement.
- `--hlr FILE` — 2D drawing data (`model_hlr.json`) for the
  cross-check.

Outputs in `--out`:

- `features.json` — recognized features, datum, verify results, and
  `refine_report` (status, per-iteration metrics, failure analysis).
- `model.fcstd`, `model.step` — the built parametric model (best
  iteration; never a worse revision).
- `build_macro.py` — the generated FreeCAD macro.
- `ct/` — CT slice PNGs + `slices.json` (with `--ct`).
- `iso/` — isometric views + montage (with `--iso`).

## Seeing the results (visual report)

Every run leaves images you can look at, so you can see the
intermediate results and the final result with your own eyes:

- `features.png` — the recognized features in top view (blue =
  profile, red dashed = hole), each labelled with its name + Z
  range. What stage 1 read from the STEP.
- `iter_<n>/` — one folder per iteration, each with the 4 shaded
  iso views of that iteration's built model + its `model.step`.
  Intermediate results are kept, never overwritten by the next
  iteration.
- `report.html` — open this one file and you see it all: the
  recognized features, every iteration's preview + metrics, the
  final (best) model, the verification detail, and what is still
  wrong and where.

## Pass criteria

The verdict is PASS only when **volume** (error < 0.5%), **iso
silhouette mean IoU** (≥ 0.90) and **CT mean slice IoU** (≥ 0.90)
all pass. A build that produces the wrong shape is not a success
even if it does not crash.

## Example

`examples/output_flange.step` is a Ø37 output flange from the
HD60-32 harmonic drive (Grok Bot design, CC BY 4.0). Run it:

```bash
bash examples/run_example.sh
```

It should finish with volume 0.000% and 17/17 faces. See
`examples/README.md` for the guide: what the part is, what each
file is (STEP, 2D drawing JSON + PNG), how to run it with and
without the drawing cross-check, and the expected result.

## License

MIT. See `LICENSE`.
