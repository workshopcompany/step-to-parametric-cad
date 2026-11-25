# Examples

Test parts for `step2param`, with the expected results so you can
verify the program works. Each example has a STEP file and, where
available, its 2D drawing.

## output_flange — HD60-32 harmonic drive output flange

The example part. A Ø37 mm disc, 7.8 mm thick, with 7 holes
(central Ø8 + Ø3.2 × 3 + Ø3 × 3) and a hexagonal boss. From the
HD60-32 harmonic drive design (Grok Bot, CC BY 4.0).

Files:

| File | What it is |
|---|---|
| `output_flange.step` | The STEP file. The exact-geometry source. This is the input. |
| `hlr-output-flange.json` | The 2D drawing data (HLR — hidden-line removal). The human-readable spec. Optional input for the `--hlr` cross-check. |
| `drawing-output-flange.png` | The 2D drawing as an image, for looking at. Same drawing as the JSON, rendered. |
| `run_example.sh` | Runs the program on this part. |

### How to run

```bash
# From the repo root. freecadcmd defaults to "freecadcmd" on PATH.
bash examples/run_example.sh
# Or with an explicit freecadcmd path:
bash examples/run_example.sh /path/to/freecadcmd
```

This runs recognize + build + verify with `--ct --iso`. The output
goes to `out/output_flange/`.

### With the 2D drawing cross-check

```bash
python -m step2param.step2param --step examples/output_flange.step \
    --hlr examples/hlr-output-flange.json \
    --out out/output_flange_hlr --ct --iso \
    --freecadcmd /path/to/freecadcmd
```

The drawing does not affect the build — it is a cross-check only
(stage 2). The program reports whether every recognized feature
appears in the drawing views.

### Expected result

The output flange is the simplest passing case. You should see:

- `volume: built 3500.7 vs orig 3500.7 mm^3 (0.000%)` — exact.
- `bbox_size_X/Y/Z: 37.000 / 37.000 / 7.800 mm` — exact.
- `face_count: built 17 vs orig 17 faces` — exact.
- `iso_mean: mean silhouette IoU 1.000`.
- `ct_mean: mean slice IoU 1.000 (worst 1.000)`.
- `refine_report: status=pass best_iteration=0` — passes on the
  first build, no refinement needed.

If you get these numbers, the program works. If volume, iso or CT
do not pass, something is wrong with the install (usually the OCP
or FreeCAD version).

### What to look at

- `out/output_flange/features.json` — the recognized features
  (17 of them), the datum, and the verify results.
- `out/output_flange/model.fcstd` — the parametric FreeCAD model.
  Open it in FreeCAD: a PartDesign Body with one sketch per
  profile/hole.
- `out/output_flange/model.step` — the built model as STEP.
- `out/output_flange/ct/` — the adaptive CT slice images.
- `out/output_flange/iso/` — the 4 isometric views + montage.

## Adding your own example

1. Put the STEP file in `examples/`.
2. (Optional) Add its 2D drawing as `hlr-<name>.json` and a
   rendered `drawing-<name>.png`, the same way as above.
3. Add a section to this file: what the part is, where it came
   from, and the expected result (volume, faces, iso/CT) so the
   next person can tell whether it works.
