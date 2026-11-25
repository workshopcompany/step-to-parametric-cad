# Pipeline

`step2param` turns a STEP file (+ an optional 2D drawing) into a
parametric FreeCAD model. The pipeline is five stages; stage 5 is
the iterative loop that finds the parts NOT drawn and draws them.

## Stage 1 — Recognize (`recognize`)

Reads the exact geometry from the STEP with OCP (OpenCascade).
No guessing: the STEP is the exact-geometry source.

- Faces are read as cylinders / planes / cones with exact
  parameters (radius, axis, area, bbox).
- **Profiles**: the outline of every planar Z-normal face is traced
  and used as the sketch profile, exactly as it is. A circle stays
  a circle, a 12-gon stays 12 lines, a D-cut stays an arc + a line.
  No name is attached to a shape — the outline IS the feature.
- The depth of a profile comes from the side faces adjacent to its
  outer wire. Profiles with the same outline are merged across
  overlapping Z ranges (a feature seen from its top face and its
  bottom face is one feature, not two).
- An inner wire is a **hole** only when its adjacent side face is a
  void (not a boss footprint). A solid boss is built as its own
  profile from its own top face.
- Cones (chamfers / countersinks) are reported as `cone_unbuilt`
  and not built in v1.

**Datum.** One reference frame for the whole pipeline, set once
here and stored in `features.json`: origin = part bbox centre
(X, Y) + bottom (Z min), axes = the assembly axes. The macro builds
in datum coordinates; verification checks alignment against the
stored datum instead of absorbing it. This is what makes the
iterative loop land new sketches in the right place.

Output: `features.json` — `{step, bbox, volume_mm3, n_cylinders,
n_planes, n_cones, datum, features}`. A feature is
`{type: "profile" | "hole" | "cone_unbuilt", outline, zmin, zmax,
centre, name}`.

## Stage 2 — Cross-check (`cross_check`, optional)

Every recognized feature must appear in the 2D drawing views
(HLR). The drawing is the human-readable spec; the STEP is the
exact-geometry source. The drawing does not affect the build — it
is a cross-check only.

## Stage 3 — Build (`build_fcstd`)

Generates a FreeCAD macro from the feature list and runs it under
`freecadcmd` (PartDesign Body, one sketch per profile/hole, Pad /
Pocket). Saves `model.fcstd` + `model.step` and `build_macro.py`.

Rules the build follows (binding, learned the hard way):

- Pad direction changes every time; always CHECK, never hardcode.
  Volume sign does NOT indicate direction. The program verifies by
  image (stage 4), not by a rule.
- Sketch planes, not cross-sections. Sequential planes for now;
  the final goal is the FEWEST planes.

## Stage 4 — Verify

The built model is compared against the ORIGINAL STEP — exact
numbers, no ink ambiguity. Three checks, all must pass:

| Check | Method | Pass |
|---|---|---|
| `volume` | built vs original volume | error < 0.5% |
| `iso_mean` | 4 iso views, silhouette IoU | mean ≥ 0.90 |
| `ct_mean` | adaptive CT slices, section IoU | mean ≥ 0.90 |

Plus `datum_align` (built bbox centre at the datum origin, Z min
0) and `ct_align` (alignment offset equals the stored datum
origin, residual < 0.05 mm) — both real checks, not absorbed.

**Adaptive CT.** Coarse slices across the whole part — by COUNT
(default 25 evenly-spaced) or by SPACING (e.g. 2.0 mm) — plus
DENSE slices at 0.2 mm only inside intervals where the ORIGINAL's
section changes (adjacent-coarse mask IoU < 0.999). Uniform regions
need no dense slices: a constant wrong shape already shows in the
coarse slices. The CT mask chains section edges into closed loops
before filling (an open arc filled as a chord-closed polygon reads
as a square hole).

## Stage 5 — Refine (iterative loop)

The loop the user designed: verify, find the parts NOT drawn,
continue drawing those parts, iterate until complete. The program
does this itself.

```
build -> verify -> failing CT regions?
                    | no  -> PASS (if volume + iso + CT all pass)
                    | yes -> re-measure the ORIGINAL in those
                    |        regions, correct the features,
                    |        rebuild, re-verify
```

**Corrections** (both measured from the original, never guessed):

1. **Outline.** A rectangle/circle feature re-measured at several
   Z in its range, replaced by the most common (mode) rectangle /
   circle. (A fillet or hook only affects a few heights.)
2. **Z-range.** A feature spanning a section change is shrunk to
   the true range and the differing sub-range gets a new feature
   (rectangle/circle from the section there).

Implementation notes:

- The section loop that best matches a feature is the one whose
  bbox is CLOSEST to the feature's (a hole's bbox sits inside the
  disc's, so first-overlap picks the wrong loop).
- Runs group by signature KIND with a 1.0 mm size-change threshold
  (a gradual taper is NOT a split); runs tile without gaps; runs
  thinner than 0.1 mm are dropped (no degenerate Pad/Pocket).

### Loop policy

- **Up to 3 refinements run automatically** (`--refine-iter`,
  default 3).
- **From the 4th on, the program stops and asks the user.** It runs
  detached, so "ask" means: stop with status `needs_decision` in
  `refine_report` and the log, and the operator asks the user in
  chat.
- **If a refinement makes the model worse than the best so far, or
  shows no improvement, it stops** with status `no_improvement`
  and reports: "개선이 진행되지 않습니다. 개선방향을 알려주면 추가
  개선을 진행해보고 개선방향이 없다면 현재작업에서 완료처리
  하겠습니다." The person chooses (give a direction, or complete
  the work as-is).
- **The best iteration's model is kept, never a worse one.** A
  worse revision is reverted (features restored, model rebuilt).

**Improvement** is measured by a deficit: volume error above the
0.5% threshold + CT mean below 0.90 (×100) + iso mean below 0.90
(×100). Lower is better; 0 means all three checks pass. A change
within 0.01 counts as no improvement.

### Failure analysis (`refine_report.failures`)

When the loop stops, it analyzes WHAT is wrong and WHERE, so the
user can give a fix direction. For each failing CT interval:

- Z range, number of failing slices, worst / mean IoU.
- The features overlapping the interval (deduplicated).
- At the worst slice, how the ORIGINAL's section differs from the
  BUILT model's: loop count, section points, bbox.
- Diagnoses: original has far more section points (fine 3D detail
  the profile does not capture); loop count differs; original bbox
  moves across the interval (taper/step); section size/position
  differs at the worst slice.

The built model is in datum coordinates, so the analysis cuts it
at built Z = assembly Z − datum Z.

`refine_report` in `features.json` carries: `status`
(`pass` / `needs_decision` / `no_improvement`), `message`,
`detail`, `auto_budget`, `best_iteration`, per-iteration metrics
(`volume_pct`, `ct_mean`, `iso_mean`, `n_fail_slices`, `deficit`),
and `failures`.

## Example: motor cradle (HD60-32)

- Iteration 0: volume +5.495%, CT mean 0.870, 41 failing slices.
- Refinement corrected 4 snap arms 12×2.4 → 12×2.0 (measured at
  20/21 heights) and the bore notches Z 3.00 → 4.00 (plus a new
  smooth-bore hole Z 3.00–4.00).
- Iteration 1: volume +1.228%, CT mean 0.931 (PASS), iso 0.999
  (PASS), 20 failing slices. No further corrections found →
  status `no_improvement`.
- Remaining failures (failure analysis): arm-tip taper
  (Z −41.6…−40.2, section differs up to 1.00 mm) and bore 3D groove
  (Z 4.07…5.93, original 301 section points vs built 197). Both are
  3D details a single prismatic profile cannot capture — the input
  for the next direction.
