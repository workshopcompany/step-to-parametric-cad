"""Mesh export for the step2param web UI.

Loads a STEP file with OCP, tessellates it, and emits a compact JSON
mesh ({vertices, triangles, bbox}) the browser viewer (Three.js) can
render directly. Also computes a per-vertex deviation (diff) between
a BUILT mesh and the ORIGINAL mesh, so the viewer can colour the
built model green/yellow/red by how far each point is from the
original surface.

The built model lives in DATUM coordinates (origin = part bbox centre
X/Y + Z min, see recognize() in step2param.py). To overlay the
original on the built model, translate the original mesh by -datum
before comparing or displaying.

Only OCP + numpy are used (both already in requirements.txt).
"""

import json
import math
import os

import numpy as np

# Deviation thresholds (mm) for the diff colours.
GREEN_MAX = 0.05    # <= 0.05 mm: close match (green)
YELLOW_MAX = 0.30   # <= 0.30 mm: small deviation (yellow)
RED_ABOVE = 1.0     # > 1.0 mm: full red (large deviation)

# Colours used for the diff (linear RGB, 0..1).
COL_GREEN = np.array([0.05, 0.75, 0.15])
COL_YELLOW = np.array([1.0, 0.80, 0.0])
COL_RED = np.array([1.0, 0.05, 0.05])


# ---------------------------------------------------------------------------
# STEP loading + tessellation
# ---------------------------------------------------------------------------

def read_shape(step_path):
    """Read a STEP file, return the OCP shape. Raises on failure."""
    from OCP.STEPControl import STEPControl_Reader
    reader = STEPControl_Reader()
    if reader.ReadFile(step_path) != 1:
        raise ValueError("cannot read STEP file: %s" % step_path)
    reader.TransferRoots()
    return reader.OneShape()


def shape_bbox(shape):
    """Bounding box of a shape, same method as recognize() in
    step2param.py: (xmin, ymin, zmin, xmax, ymax, zmax)."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    return tuple(float(v) for v in box.Get())


def datum_origin(shape):
    """The datum origin recognize() stores in features.json: bbox
    centre (X, Y) + bottom (Z min)."""
    xmin, ymin, zmin, xmax, ymax, zmax = shape_bbox(shape)
    return [round((xmin + xmax) / 2.0, 4),
            round((ymin + ymax) / 2.0, 4),
            round(zmin, 4)]


def tessellate(shape, deflection=0.05):
    """Tessellate a shape. Returns (verts, tris): verts is an (N, 3)
    float array, tris an (M, 3) int array of vertex indices.

    Same approach as _render_iso in step2param.py: BRepMesh with
    deflection 0.05, then collect the per-face triangulations.
    """
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.BRep import BRep_Tool
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    BRepMesh_IncrementalMesh(shape, deflection, False, 0.2, True)
    verts = []
    tris = []
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        face = TopoDS.Face_s(ex.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            ex.Next()
            continue
        trsf = loc.Transformation()
        base = len(verts)
        for i in range(1, tri.NbNodes() + 1):
            p = tri.Node(i).Transformed(trsf)
            verts.append([p.X(), p.Y(), p.Z()])
        for i in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(i).Get()
            tris.append([base + a - 1, base + b - 1, base + c - 1])
        ex.Next()
    return (np.array(verts, dtype=float),
            np.array(tris, dtype=np.int64).reshape(-1, 3))


def mesh_dict(verts, tris, colors=None):
    """Compact JSON-ready dict for a mesh. Vertices/triangles are flat
    lists rounded to 4 decimals to keep the payload small."""
    verts = np.asarray(verts, dtype=float)
    tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    d = {
        "n_vertices": int(len(verts)),
        "n_triangles": int(len(tris)),
        "vertices": [round(float(v), 4) for v in verts.ravel()],
        "triangles": [int(i) for i in tris.ravel()],
    }
    if len(verts):
        lo = verts.min(axis=0)
        hi = verts.max(axis=0)
        d["bbox"] = [round(float(v), 4) for v in
                     (lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])]
        d["centre"] = [round(float(v), 4) for v in (lo + hi) / 2.0]
    if colors is not None:
        cols = np.asarray(colors, dtype=float)
        d["colors"] = [round(float(v), 3) for v in cols.ravel()]
    return d


def step_to_mesh(step_path, deflection=0.05, translate=None):
    """Load a STEP file and return its mesh dict. translate is an
    optional (dx, dy, dz) shift applied to every vertex (used to move
    the original mesh into datum coordinates: translate = -datum)."""
    shape = read_shape(step_path)
    verts, tris = tessellate(shape, deflection=deflection)
    if translate is not None:
        verts = verts + np.array(translate, dtype=float)
    return mesh_dict(verts, tris)


def step_datum(step_path):
    """Datum origin of a STEP file, computed the same way recognize()
    does (bbox centre X/Y + Z min)."""
    return datum_origin(read_shape(step_path))


# ---------------------------------------------------------------------------
# Diff: distance from each BUILT vertex to the ORIGINAL mesh surface
# ---------------------------------------------------------------------------

def _closest_point_on_triangles(point, tri_a, tri_b, tri_c):
    """Closest point on each of N triangles to one point (Ericson,
    Real-Time Collision Detection 5.1.5), vectorized over triangles.
    point: (3,), tri_a/b/c: (N, 3). Returns (N, 3) closest points."""
    p = np.asarray(point, dtype=float)
    ab = tri_b - tri_a
    ac = tri_c - tri_a
    ap = p - tri_a
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)
    # Vertex region A
    closest = tri_a.copy()
    mask = (d1 <= 0) & (d2 <= 0)
    # Vertex region B
    bp = p - tri_b
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)
    m = (~mask) & (d3 >= 0) & (d4 <= d3)
    closest[m] = tri_b[m]
    mask = mask | m
    # Edge region AB
    vc = d1 * d4 - d3 * d2
    m = (~mask) & (vc <= 0) & (d1 >= 0) & (d3 <= 0)
    denom = d1 - d3
    denom = np.where(denom == 0, 1.0, denom)
    v = np.where(m, d1 / denom, 0.0)
    closest[m] = tri_a[m] + ab[m] * v[m][:, None]
    mask = mask | m
    # Vertex region C
    cp = p - tri_c
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)
    m = (~mask) & (d6 >= 0) & (d5 <= d6)
    closest[m] = tri_c[m]
    mask = mask | m
    # Edge region AC
    vb = d5 * d2 - d1 * d6
    m = (~mask) & (vb <= 0) & (d2 >= 0) & (d6 <= 0)
    denom = d2 - d6
    denom = np.where(denom == 0, 1.0, denom)
    w = np.where(m, d2 / denom, 0.0)
    closest[m] = tri_a[m] + ac[m] * w[m][:, None]
    mask = mask | m
    # Edge region BC
    va = d3 * d6 - d5 * d4
    m = (~mask) & (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
    denom = (d4 - d3) + (d5 - d6)
    denom = np.where(denom == 0, 1.0, denom)
    w = np.where(m, (d4 - d3) / denom, 0.0)
    closest[m] = tri_b[m] + (tri_c[m] - tri_b[m]) * w[m][:, None]
    mask = mask | m
    # Face region: barycentric interpolation.
    denom = va + vb + vc
    denom = np.where(denom == 0, 1.0, denom)
    vv = vb / denom
    ww = vc / denom
    m = ~mask
    closest[m] = (tri_a[m] + ab[m] * vv[m][:, None]
                  + ac[m] * ww[m][:, None])
    return closest


class _TriangleGrid(object):
    """Uniform-grid spatial hash over triangles, for nearest-surface
    lookup. Each triangle is stored in the cells its bounding box
    overlaps (capped, so one huge triangle cannot flood the grid).
    Triangles whose bbox spans more than 64 cells are kept in a
    separate list that is always checked: a huge flat face is stored
    in only a few cells, but a point in the middle of that face must
    still find it."""

    def __init__(self, verts, tris, cell_size):
        self.cell = float(cell_size)
        self.verts = verts
        self.tris = tris
        a = verts[tris[:, 0]]
        b = verts[tris[:, 1]]
        c = verts[tris[:, 2]]
        lo = np.minimum(np.minimum(a, b), c)
        hi = np.maximum(np.maximum(a, b), c)
        self.lo = lo
        self.hi = hi
        self.grid = {}
        self.big = []
        for i in range(len(tris)):
            i0 = np.floor(lo[i] / self.cell).astype(int)
            i1 = np.floor(hi[i] / self.cell).astype(int)
            span = i1 - i0 + 1
            if int(span.prod()) > 64:
                # Huge triangle: do not flood the grid; it is checked
                # directly for every query point instead.
                self.big.append(i)
                continue
            for ix in range(i0[0], i1[0] + 1):
                for iy in range(i0[1], i1[1] + 1):
                    for iz in range(i0[2], i1[2] + 1):
                        self.grid.setdefault((ix, iy, iz), []).append(i)
        self.big = np.array(self.big, dtype=np.int64)

    def nearby_triangles(self, point, ring):
        """Triangle indices in cells within Chebyshev distance `ring`
        of the cell containing `point`."""
        c = np.floor(np.asarray(point, dtype=float) / self.cell).astype(int)
        out = []
        for ix in range(c[0] - ring, c[0] + ring + 1):
            for iy in range(c[1] - ring, c[1] + ring + 1):
                for iz in range(c[2] - ring, c[2] + ring + 1):
                    out.extend(self.grid.get((ix, iy, iz), ()))
        return out


def _distance_to_surface(points, grid, max_dist=5.0):
    """Distance from each point to the gridded mesh surface.

    Rings grow until the best distance found so far cannot be beaten
    by any triangle not yet examined: a triangle whose bbox only
    overlaps cells beyond ring r is at least r*cell_size away, so
    once best <= r*cell_size the search stops. (Stopping at the
    first ring that contains ANY triangle is wrong: a big triangle
    stored in that cell by bbox overlap can be far from the point
    while the true nearest triangle sits in the next ring.)
    Points with nothing within max_dist get max_dist (far = red)."""
    verts, tris = grid.verts, grid.tris
    a = verts[tris[:, 0]]
    b = verts[tris[:, 1]]
    c = verts[tris[:, 2]]
    max_ring = max(1, int(math.ceil(max_dist / grid.cell)))
    dists = np.full(len(points), max_dist, dtype=float)
    for i, p in enumerate(points):
        best = float("inf")
        # Huge triangles are always checked (they are few).
        if len(grid.big):
            cp = _closest_point_on_triangles(p, a[grid.big],
                                             b[grid.big], c[grid.big])
            best = float(np.linalg.norm(cp - p, axis=1).min())
        seen = set()
        for ring in range(0, max_ring + 1):
            idx = [t for t in grid.nearby_triangles(p, ring)
                   if t not in seen]
            seen.update(idx)
            if idx:
                idx = np.array(sorted(idx), dtype=np.int64)
                cp = _closest_point_on_triangles(p, a[idx], b[idx], c[idx])
                d = float(np.linalg.norm(cp - p, axis=1).min())
                if d < best:
                    best = d
            # Triangles not yet examined are at least ring*cell away.
            if best <= ring * grid.cell:
                break
        if best != float("inf"):
            # Cap at the search radius: a big triangle far away only
            # proves the point is at least max_dist from the surface.
            dists[i] = min(best, max_dist)
    return dists


def deviation_colors(dists):
    """Per-vertex RGB colours from distances (mm): green (<=0.05) ->
    yellow (<=0.30) -> red (>0.30, full red above 1.0)."""
    dists = np.asarray(dists, dtype=float)
    cols = np.zeros((len(dists), 3), dtype=float)
    green = dists <= GREEN_MAX
    cols[green] = COL_GREEN
    mid = (dists > GREEN_MAX) & (dists <= YELLOW_MAX)
    t = ((dists[mid] - GREEN_MAX) / (YELLOW_MAX - GREEN_MAX))[:, None]
    cols[mid] = COL_GREEN * (1 - t) + COL_YELLOW * t
    far = (dists > YELLOW_MAX) & (dists <= RED_ABOVE)
    t = ((dists[far] - YELLOW_MAX) / (RED_ABOVE - YELLOW_MAX))[:, None]
    cols[far] = COL_YELLOW * (1 - t) + COL_RED * t
    cols[dists > RED_ABOVE] = COL_RED
    return cols


def diff_histogram(dists):
    """Summary of a distance array (mm): share of vertices within
    0.05 / 0.1 / 0.3 / 1.0 mm, mean and max deviation."""
    dists = np.asarray(dists, dtype=float)
    n = len(dists)
    if n == 0:
        return {"n_vertices": 0, "within_0.05mm_pct": 0.0,
                "within_0.1mm_pct": 0.0, "within_0.3mm_pct": 0.0,
                "within_1.0mm_pct": 0.0, "mean_mm": 0.0, "max_mm": 0.0}
    return {
        "n_vertices": int(n),
        "within_0.05mm_pct": round(100.0 * float((dists <= 0.05).mean()), 2),
        "within_0.1mm_pct": round(100.0 * float((dists <= 0.10).mean()), 2),
        "within_0.3mm_pct": round(100.0 * float((dists <= 0.30).mean()), 2),
        "within_1.0mm_pct": round(100.0 * float((dists <= 1.0).mean()), 2),
        "mean_mm": round(float(dists.mean()), 4),
        "max_mm": round(float(dists.max()), 4),
    }


def compute_diff(built_verts, built_tris, orig_verts, orig_tris,
                 deflection=0.05):
    """Per-vertex deviation of the BUILT mesh from the ORIGINAL mesh
    surface. Both meshes must already be in the SAME coordinates
    (original translated by -datum). Returns (colors, histogram).

    To keep the diff fast on big parts, the built mesh is decimated
    for the distance search when it has more than 40000 vertices:
    the colours are computed on the decimated set and then mapped
    back by nearest vertex.
    """
    built_verts = np.asarray(built_verts, dtype=float)
    orig_verts = np.asarray(orig_verts, dtype=float)
    orig_tris = np.asarray(orig_tris, dtype=np.int64).reshape(-1, 3)
    if len(built_verts) == 0 or len(orig_tris) == 0:
        return (np.zeros((len(built_verts), 3)),
                diff_histogram(np.array([])))
    # Grid cell: ~1/80 of the original bbox diagonal, at least 0.2 mm.
    lo = orig_verts.min(axis=0)
    hi = orig_verts.max(axis=0)
    diag = float(np.linalg.norm(hi - lo))
    cell = max(0.2, diag / 80.0)
    grid = _TriangleGrid(orig_verts, orig_tris, cell)
    # Decimate the query set if huge (keep every k-th vertex).
    step = 1
    if len(built_verts) > 40000:
        step = int(math.ceil(len(built_verts) / 40000.0))
    query = built_verts[::step]
    dists_q = _distance_to_surface(query, grid, max_dist=5.0)
    if step == 1:
        dists = dists_q
    else:
        # Map back: each built vertex takes the distance of the
        # nearest decimated vertex (same grid, cheap).
        dists = np.zeros(len(built_verts), dtype=float)
        for i in range(len(built_verts)):
            dists[i] = dists_q[min(i // step, len(dists_q) - 1)]
    return deviation_colors(dists), diff_histogram(dists)


def diff_mesh(built_step_path, orig_step_path, datum):
    """Full diff for two STEP files. datum is the origin the built
    model is in (from features.json); the original is translated by
    -datum so both meshes overlay. Returns a mesh dict for the BUILT
    model with per-vertex diff colours, plus the histogram."""
    b_verts, b_tris = tessellate(read_shape(built_step_path))
    o_verts, o_tris = tessellate(read_shape(orig_step_path))
    o_verts = o_verts - np.array(datum, dtype=float)
    colors, hist = compute_diff(b_verts, b_tris, o_verts, o_tris)
    d = mesh_dict(b_verts, b_tris, colors=colors)
    d["histogram"] = hist
    return d


def main(argv=None):
    """CLI for a quick check: python -m step2param.mesh_export
    --step part.step [--out mesh.json] [--datum x,y,z]
    [--diff-built built.step --diff-orig orig.step]."""
    import argparse
    ap = argparse.ArgumentParser(description="STEP -> JSON mesh for the web UI")
    ap.add_argument("--step", help="STEP file to mesh")
    ap.add_argument("--out", default=None, help="output JSON path")
    ap.add_argument("--datum", default=None,
                    help="datum origin x,y,z to subtract")
    ap.add_argument("--diff-built", default=None)
    ap.add_argument("--diff-orig", default=None)
    args = ap.parse_args(argv)
    if args.diff_built and args.diff_orig:
        datum = ([float(v) for v in args.datum.split(",")]
                 if args.datum else [0.0, 0.0, 0.0])
        d = diff_mesh(args.diff_built, args.diff_orig, datum)
        print("diff: %d vertices, mean %.4f mm, max %.4f mm, "
              "within 0.05mm %.1f%%" % (
                  d["histogram"]["n_vertices"],
                  d["histogram"]["mean_mm"], d["histogram"]["max_mm"],
                  d["histogram"]["within_0.05mm_pct"]))
    elif args.step:
        tr = ([-float(v) for v in args.datum.split(",")]
              if args.datum else None)
        d = step_to_mesh(args.step, translate=tr)
        print("mesh: %d vertices, %d triangles" % (
            d["n_vertices"], d["n_triangles"]))
    else:
        ap.error("give --step or --diff-built + --diff-orig")
    if args.out:
        with open(args.out, "w") as f:
            json.dump(d, f)
        print("wrote", args.out, os.path.getsize(args.out), "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
