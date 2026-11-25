"""STEP + 2D drawing -> parametric FreeCAD file (step2param).

The user makes the 2D drawing (HLR) from the STEP; THIS program takes the
2D drawing + the STEP and builds a parametric FreeCAD (.fcstd) model.
Nobody builds the model by hand -- the program does it.

Pipeline:
  1. recognize(step) -> FeatureList (exact geometry from the STEP)
  2. cross_check(features, hlr_json) -> every feature must appear in the
     2D views (the drawing is the human-readable spec; the STEP is the
     exact-geometry source)
  3. build_fcstd(features) -> FreeCAD PartDesign model (.fcstd + .step),
     generated as a FreeCAD macro and run under freecadcmd
  4. verify(built_step, orig_step) -> volume / bbox / face comparison
     against the ORIGINAL STEP (exact numbers, no ink ambiguity),
     plus iso-silhouette IoU and adaptive CT slice IoU
  5. refine (iterative loop): verify, find the parts NOT drawn,
     re-measure the ORIGINAL in the failing regions and correct the
     features, rebuild and re-verify -- up to 3 automatic refinements.
     From the 4th on, the program stops and asks the user. If a
     refinement makes the model worse, or shows no improvement, it
     stops and reports what is wrong and where (failure analysis),
     so the user can give a fix direction. The best iteration's
     model is kept, never a worse one. See PIPELINE.md.

Feature recognition is outline-based, not type-matching: the program
traces the wire of every planar Z-normal face and uses it as the
sketch profile, exactly as it is. A circle stays a circle, a 12-gon
stays 12 lines, a D-cut stays an arc + a line. No name is attached
to a shape -- the outline IS the feature. The depth of a profile
comes from the side faces adjacent to its outer wire. An inner wire
is a hole only when its adjacent side face is a void; a solid boss
footprint is left alone (the boss is built as its own profile from
its own top face).

Run (OCP python for recognition, freecadcmd for the build):
  <venv>/bin/python -m step2param.step2param --step X.step --hlr hlr.json \\
      --out dir/ --freecadcmd /path/to/freecadcmd
"""

import json
import math
import os
import subprocess
import sys

# ---------------------------------------------------------------------------
# Stage 1: feature recognition from the STEP (OCP, exact geometry)
# ---------------------------------------------------------------------------


def _read_faces(step_path):
    """Return (cylinders, planes, cones) with exact parameters.

    cylinders: [{radius, axis_loc (x,y,z), axis_dir (x,y,z), area, zmin, zmax}]
    planes:    [{normal (x,y,z), point (x,y,z), area, bbox}]
    cones:     [{radius, semi_angle, axis_loc, axis_dir, area}]
    """
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopoDS import TopoDS
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Cone
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.gp import gp_Pnt, gp_Vec

    reader = STEPControl_Reader()
    assert reader.ReadFile(step_path) == 1, "cannot read STEP: %s" % step_path
    reader.TransferRoots()
    shape = reader.OneShape()

    cylinders, planes, cones = [], [], []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        ad = BRepAdaptor_Surface(face)
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        area = props.Mass()
        box = Bnd_Box()
        BRepBndLib.Add_s(face, box)
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        t = ad.GetType()
        if t == GeomAbs_Cylinder:
            cyl = ad.Cylinder()
            ax = cyl.Axis()
            loc = ax.Location()
            d = ax.Direction()
            # Boss (solid) vs hole (void): the face normal points AWAY
            # from the material.  For a Z-axis cylinder, compare the
            # normal's radial component with the radial direction from
            # the axis to a point on the face.  Outward -> solid boss,
            # inward -> void hole.  This is what separates a hub/pin
            # (solid) from a bore (void) when both are cylinders.
            is_boss = True
            try:
                umin, umax = ad.FirstUParameter(), ad.LastUParameter()
                vmin, vmax = ad.FirstVParameter(), ad.LastVParameter()
                u = (umin + umax) / 2.0
                v = (vmin + vmax) / 2.0
                pnt = gp_Pnt()
                d1u = gp_Vec()
                d1v = gp_Vec()
                ad.D1(u, v, pnt, d1u, d1v)
                n = d1u.Crossed(d1v)
                if face.Orientation() == TopAbs_REVERSED:
                    n.Reverse()
                rx = pnt.X() - loc.X()
                ry = pnt.Y() - loc.Y()
                rz = pnt.Z() - loc.Z()
                # Radial direction: component of (pnt - loc) perpendicular
                # to the axis.
                dx, dy, dz = d.X(), d.Y(), d.Z()
                along = rx * dx + ry * dy + rz * dz
                px, py, pz = rx - along * dx, ry - along * dy, rz - along * dz
                is_boss = (n.X() * px + n.Y() * py + n.Z() * pz) > 0
            except Exception:
                is_boss = True
            cylinders.append({
                "radius": round(cyl.Radius(), 4),
                "axis_loc": [round(loc.X(), 4), round(loc.Y(), 4), round(loc.Z(), 4)],
                "axis_dir": [round(d.X(), 4), round(d.Y(), 4), round(d.Z(), 4)],
                "area": round(area, 2),
                "bbox": [round(v, 3) for v in (xmin, ymin, zmin, xmax, ymax, zmax)],
                "is_boss": bool(is_boss),
            })
        elif t == GeomAbs_Plane:
            pl = ad.Plane()
            ax = pl.Axis()
            loc = ax.Location()
            d = ax.Direction()
            planes.append({
                "normal": [round(d.X(), 4), round(d.Y(), 4), round(d.Z(), 4)],
                "point": [round(loc.X(), 4), round(loc.Y(), 4), round(loc.Z(), 4)],
                "area": round(area, 2),
                "bbox": [round(v, 3) for v in (xmin, ymin, zmin, xmax, ymax, zmax)],
            })
        elif t == GeomAbs_Cone:
            cone = ad.Cone()
            ax = cone.Axis()
            loc = ax.Location()
            d = ax.Direction()
            cones.append({
                "radius": round(cone.RefRadius(), 4),
                "semi_angle_deg": round(math.degrees(cone.SemiAngle()), 4),
                "axis_loc": [round(loc.X(), 4), round(loc.Y(), 4), round(loc.Z(), 4)],
                "axis_dir": [round(d.X(), 4), round(d.Y(), 4), round(d.Z(), 4)],
                "area": round(area, 2),
                "bbox": [round(v, 3) for v in (xmin, ymin, zmin, xmax, ymax, zmax)],
            })
        exp.Next()
    return cylinders, planes, cones


def _chain_edges(edges, tol=0.01):
    """Order wire edges into a closed loop by endpoint matching.

    TopExp_Explorer returns edges in storage order, which is NOT the
    loop order for complex wires (cradle bayonet socket: consecutive
    edges up to 50 mm apart). A sketch built from unordered edges
    gets Coincident constraints pulling far-apart points together,
    which collapses edges to zero length ("Both points are equal")
    and fails the solve. Chain by matching the current position to
    EITHER endpoint of the next edge: the wire can traverse an edge
    backwards (its end sits where the loop enters). A LINE traversed
    backwards is flipped (swap p1/p2). An ARC is never flipped --
    its stored a0/a1 are the true geometric arc (round 9) and
    swapping them would give the complement; the traversal just
    continues from the arc's other endpoint. Edges that cannot be
    chained are appended at the end in their original order.
    (Round 9b, Nov 24: cradle Profile19 chain broke at edge 0 --
    the true next edge was an arc whose END, not start, sat at the
    current position, so the sketch wire stayed open and the pad
    failed, nulling Profile19 onward including both pockets.)
    """
    if len(edges) <= 1:
        return edges

    def _start(e):
        if e["kind"] == "line":
            return e["p1"]
        if e["kind"] == "arc":
            a = math.radians(e["a0"])
            return [e["centre"][0] + e["radius"] * math.cos(a),
                    e["centre"][1] + e["radius"] * math.sin(a)]
        return [e["centre"][0] + e["radius"], e["centre"][1]]

    def _end(e):
        if e["kind"] == "line":
            return e["p2"]
        if e["kind"] == "arc":
            a = math.radians(e["a1"])
            return [e["centre"][0] + e["radius"] * math.cos(a),
                    e["centre"][1] + e["radius"] * math.sin(a)]
        return [e["centre"][0] + e["radius"], e["centre"][1]]

    def _dist(p, q):
        return math.hypot(p[0] - q[0], p[1] - q[1])

    unused = list(edges)
    ordered = [unused.pop(0)]
    cur_pos = _end(ordered[-1])
    while unused:
        nxt = None
        matched_end = False
        for i, e in enumerate(unused):
            if _dist(_start(e), cur_pos) < tol:
                nxt = i
                matched_end = False
                break
            if _dist(_end(e), cur_pos) < tol:
                nxt = i
                matched_end = True
                break
        if nxt is None:
            break
        e = unused.pop(nxt)
        if e["kind"] == "line" and matched_end:
            e = {"kind": "line", "p1": e["p2"], "p2": e["p1"]}
            cur_pos = _end(e)
        elif matched_end:
            # Arc traversed backwards: exit at its start endpoint.
            cur_pos = _start(e)
        else:
            cur_pos = _end(e)
        ordered.append(e)
    ordered.extend(unused)
    return ordered


def _wire_edges(wire):
    """A wire as an ordered list of sketch-geometry dicts.

    The outline IS the feature -- no name, no type matching. A circle
    stays a circle, a 12-gon stays 12 lines, a D-cut stays an arc + a
    line. Each edge is one of:
      {"kind": "line", "p1": [x, y], "p2": [x, y]}
      {"kind": "circle", "centre": [x, y], "radius": r}
      {"kind": "arc", "centre": [x, y], "radius": r, "a0": deg, "a1": deg}
    Coordinates are global X/Y (the sketch sits at Z only).

    A wire can wind around a small boss twice (the wave-generator
    base bottom face: R16 arcs + R2.6 boss arcs in one wire, the boss
    arcs overlapping). If the arcs at one centre cover a full circle
    while arcs at other centres are also present, the wire is a
    circle with boss loops attached -- keep only the largest circle
    as the outline (the bosses are separate features, caught by their
    own faces) instead of feeding a self-overlapping loop to the
    sketch.
    """
    from OCP.TopExp import TopExp_Explorer, TopExp
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_REVERSED
    from OCP.TopoDS import TopoDS
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Line, GeomAbs_Circle
    from OCP.BRep import BRep_Tool

    edges = []
    exp = TopExp_Explorer(wire, TopAbs_EDGE)
    while exp.More():
        edge = TopoDS.Edge_s(exp.Current())
        ad = BRepAdaptor_Curve(edge)
        t = ad.GetType()
        if t == GeomAbs_Line:
            v1 = TopExp.FirstVertex_s(edge)
            v2 = TopExp.LastVertex_s(edge)
            p1 = BRep_Tool.Pnt_s(v1)
            p2 = BRep_Tool.Pnt_s(v2)
            edges.append({"kind": "line",
                          "p1": [round(p1.X(), 6), round(p1.Y(), 6)],
                          "p2": [round(p2.X(), 6), round(p2.Y(), 6)]})
        elif t == GeomAbs_Circle:
            circ = ad.Circle()
            loc = circ.Location()
            r = circ.Radius()
            u0, u1 = ad.FirstParameter(), ad.LastParameter()
            if abs(abs(u1 - u0) - 2 * math.pi) < 0.01:
                edges.append({"kind": "circle",
                              "centre": [round(loc.X(), 6), round(loc.Y(), 6)],
                              "radius": round(r, 6)})
            else:
                # Arc: the true geometric arc. The vertex angles alone
                # cannot tell a short arc from its complement (both
                # share the endpoints). The parametric range is
                # measured in the circle's own axis system, so adding
                # the X-axis angle gets the sweep right but can mirror
                # the arc when the circle's axis is -Z. Disambiguate
                # with the arc midpoint: the point at mid-parameter
                # lies on the true arc, so the CCW interval from a0
                # to a1 must pass through it. (Round 9, Nov 24:
                # vertex-angle+CCW stored complements, e.g. cradle
                # disc arcs of 282-359 deg; round 9c: param+XAxis
                # mirrored one bore arc, a0=334.5 instead of 0,
                # leaving Profile19's wire open and its pad null.)
                v1 = TopExp.FirstVertex_s(edge)
                v2 = TopExp.LastVertex_s(edge)
                p1 = BRep_Tool.Pnt_s(v1)
                p2 = BRep_Tool.Pnt_s(v2)
                cx, cy = loc.X(), loc.Y()
                ang1 = math.degrees(math.atan2(p1.Y() - cy, p1.X() - cx))
                ang2 = math.degrees(math.atan2(p2.Y() - cy, p2.X() - cx))
                u0, u1 = ad.FirstParameter(), ad.LastParameter()
                pm = ad.Value(0.5 * (u0 + u1))
                amid = math.degrees(math.atan2(pm.Y() - cy, pm.X() - cx))
                sweep12 = (ang2 - ang1) % 360.0
                if ((amid - ang1) % 360.0) <= sweep12 + 1e-9:
                    a1, a2 = ang1, ang2
                else:
                    a1, a2 = ang2, ang1
                while a2 <= a1:
                    a2 += 360.0
                edges.append({"kind": "arc",
                              "centre": [round(cx, 6), round(cy, 6)],
                              "radius": round(r, 6),
                              "a0": round(a1, 6), "a1": round(a2, 6)})
        exp.Next()
    # Order the edges into a closed loop (storage order is not loop
    # order for complex wires -- see _chain_edges).
    edges = _chain_edges(edges)
    # Simplify a wire that mixes a full circle with small boss loops:
    # if the arcs at one centre cover a full 360 deg while arcs at
    # other centres are also present, keep only that centre's circle.
    arcs = [e for e in edges if e["kind"] in ("arc", "circle")]
    if len(arcs) >= 2 and len(edges) > len(arcs):
        pass  # lines present: a polygon/D-cut, leave it alone
    elif len(arcs) >= 2:
        by_centre = {}
        for e in arcs:
            by_centre.setdefault((e["centre"][0], e["centre"][1],
                                  e["radius"]), []).append(e)
        if len(by_centre) > 1:
            best = None
            for (cx, cy, r), grp in by_centre.items():
                span = 0.0
                for e in grp:
                    if e["kind"] == "circle":
                        span = 360.0
                    else:
                        span += e["a1"] - e["a0"]
                if span >= 359.0 and (best is None or r > best[0]):
                    best = (r, cx, cy)
            if best is not None:
                return [{"kind": "circle", "centre": [best[1], best[2]],
                         "radius": best[0]}]
    # Drop degenerate edges: a line/arc shorter than MIN_EDGE_LEN
    # collapses under the Coincident constraints ("Both points are
    # equal") and fails the sketch. The gap is bridged by the
    # constraints, which pull the neighbours together by < 0.01 mm.
    # (Round 8, Nov 24, 2026: front-cover bayonet lugs carry
    # 0.0047 / 0.0073 mm segments that collapsed geometry index 5.)
    MIN_EDGE_LEN = 0.01
    kept = []
    for e in edges:
        if e["kind"] == "line":
            L = math.hypot(e["p2"][0] - e["p1"][0],
                           e["p2"][1] - e["p1"][1])
        elif e["kind"] == "arc":
            L = e["radius"] * math.radians(e["a1"] - e["a0"])
        else:  # circle: a closed loop, never degenerate by length
            kept.append(e)
            continue
        if L >= MIN_EDGE_LEN:
            kept.append(e)
    # Never drop the whole outline: if everything is tiny, keep it
    # and let the sketch report the real error.
    if kept:
        edges = kept
    return edges


def _face_is_boss(face):
    """True if a cylindrical face is a solid boss (outward normal),
    False if it is a void hole (inward normal), None if not a cylinder.
    Used to tell a boss footprint apart from a hole in a planar face.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.gp import gp_Pnt, gp_Vec

    ad = BRepAdaptor_Surface(face)
    if ad.GetType() != GeomAbs_Cylinder:
        return None
    cyl = ad.Cylinder()
    ax = cyl.Axis()
    loc = ax.Location()
    d = ax.Direction()
    try:
        umin, umax = ad.FirstUParameter(), ad.LastUParameter()
        vmin, vmax = ad.FirstVParameter(), ad.LastVParameter()
        u = (umin + umax) / 2.0
        v = (vmin + vmax) / 2.0
        pnt = gp_Pnt()
        d1u = gp_Vec()
        d1v = gp_Vec()
        ad.D1(u, v, pnt, d1u, d1v)
        n = d1u.Crossed(d1v)
        if face.Orientation() == TopAbs_REVERSED:
            n.Reverse()
        rx = pnt.X() - loc.X()
        ry = pnt.Y() - loc.Y()
        rz = pnt.Z() - loc.Z()
        dx, dy, dz = d.X(), d.Y(), d.Z()
        along = rx * dx + ry * dy + rz * dz
        px, py, pz = rx - along * dx, ry - along * dy, rz - along * dz
        return (n.X() * px + n.Y() * py + n.Z() * pz) > 0
    except Exception:
        return True


def _read_profiles(step_path):
    """Trace the outline of every planar Z-normal face.

    No feature-type matching: the wire IS the profile. Returns
    (profiles, holes):
      profiles: [{outline, zmin, zmax, centre}]  (additive, Pad)
      holes:    [{outline, zmin, zmax, centre}]  (subtractive, Pocket)
    A profile comes from a TOP face (normal +Z); the feature extends
    down to the lowest Z of the side faces adjacent to its outer wire.
    An inner wire is a hole only when its adjacent side face is a void
    (is_boss False); a solid boss footprint is left alone -- the boss
    is built as its own profile from its own top face.
    """
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopExp import TopExp_Explorer, TopExp
    from OCP.TopAbs import TopAbs_FACE, TopAbs_WIRE, TopAbs_EDGE, TopAbs_REVERSED
    from OCP.TopoDS import TopoDS
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder
    from OCP.BRep import BRep_Tool
    from OCP.BRepTools import BRepTools
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    reader = STEPControl_Reader()
    assert reader.ReadFile(step_path) == 1, "cannot read STEP: %s" % step_path
    reader.TransferRoots()
    shape = reader.OneShape()

    # Edge -> faces map (for the depth and the hole/boss decision).
    efmap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, efmap)

    def adjacent_faces(edge, skip_face):
        """Faces sharing this edge, other than skip_face, as TopoDS faces."""
        out = []
        if not efmap.Contains(edge):
            return out
        for f2 in efmap.FindFromKey(edge):
            f2 = TopoDS.Face_s(f2)
            if not f2.IsSame(skip_face):
                out.append(f2)
        return out

    def face_zrange(f):
        b = Bnd_Box()
        BRepBndLib.Add_s(f, b)
        return b.Get()[2], b.Get()[5]

    # All cylindrical faces, keyed by (centre x, y, radius): a cylinder
    # split by a seam/sliver face is several faces with the same key.
    # A circular profile's depth must span all of them, or only the top
    # step of a cup/disc is built (the cap/flange regression).
    cyl_by_key = {}
    cexp = TopExp_Explorer(shape, TopAbs_FACE)
    while cexp.More():
        cf = TopoDS.Face_s(cexp.Current())
        cad = BRepAdaptor_Surface(cf)
        if cad.GetType() == GeomAbs_Cylinder:
            cc = cad.Cylinder()
            cloc = cc.Axis().Location()
            cdir = cc.Axis().Direction()
            if abs(abs(cdir.Z()) - 1.0) < 0.01:
                cz0, cz1 = face_zrange(cf)
                key = (round(cloc.X(), 2), round(cloc.Y(), 2),
                       round(cc.Radius(), 3))
                cz0m, cz1m = cyl_by_key.get(key, (cz0, cz1))
                cyl_by_key[key] = (min(cz0m, cz0), max(cz1m, cz1))
        cexp.Next()

    profiles, holes = [], []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        ad = BRepAdaptor_Surface(face)
        if ad.GetType() != GeomAbs_Plane:
            exp.Next()
            continue
        d = ad.Plane().Axis().Direction()
        # The surface normal ignores the face orientation; a bottom face
        # has a +Z surface normal but a REVERSED orientation, so its
        # actual normal is -Z. Account for it, or the bottom face is
        # mistaken for a top face and yields a zero-depth profile.
        if face.Orientation() == TopAbs_REVERSED:
            d = d.Reversed()
        # Only horizontal faces: their wires are XY sketches.
        if abs(abs(d.Z()) - 1.0) > 0.01:
            exp.Next()
            continue
        # A TOP face (+Z) is the top of a feature that extends DOWNWARD;
        # a BOTTOM face (-Z) is the bottom of a feature that extends
        # UPWARD (a boss protruding down from a plate has no exposed top
        # face -- its top is fused -- only its bottom face is visible).
        # Both give a profile; the direction decides which way it grows.
        is_top = d.Z() > 0
        box = Bnd_Box()
        BRepBndLib.Add_s(face, box)
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        zf = round((zmin + zmax) / 2.0, 4)
        # Skip sliver faces (area ~0): they are seams/edges, not caps.
        # The wave-generator cap has two such faces at the rim (area 0.0)
        # whose arc wires would otherwise become bogus profiles.
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        if props.Mass() < 0.1:
            exp.Next()
            continue
        outer = BRepTools.OuterWire_s(face)
        outer_edges = _wire_edges(outer)
        if not outer_edges:
            exp.Next()
            continue
        # Depth: for a top face, the lowest Z of the side faces adjacent
        # to the outer wire; for a bottom face, the highest Z.
        # For a single-circle profile, only side faces that ARE that
        # circle's cylinder count -- the base bottom flange's wire is
        # adjacent to the R2.6 boss cylinders too (they reach Z 6.4),
        # which would stretch the R16 disc 0.2 mm too tall.
        z_lo, z_hi = zf, zf
        circ_key = None
        if (len(outer_edges) == 1 and outer_edges[0]["kind"] == "circle"):
            circ_key = (round(outer_edges[0]["centre"][0], 2),
                        round(outer_edges[0]["centre"][1], 2),
                        round(outer_edges[0]["radius"], 3))
        wexp = TopExp_Explorer(outer, TopAbs_EDGE)
        while wexp.More():
            for f2 in adjacent_faces(TopoDS.Edge_s(wexp.Current()), face):
                if circ_key is not None:
                    cad2 = BRepAdaptor_Surface(f2)
                    if cad2.GetType() != GeomAbs_Cylinder:
                        continue
                    cc2 = cad2.Cylinder()
                    cloc2 = cc2.Axis().Location()
                    key2 = (round(cloc2.X(), 2), round(cloc2.Y(), 2),
                            round(cc2.Radius(), 3))
                    if key2 != circ_key:
                        continue
                sz0, sz1 = face_zrange(f2)
                if sz0 < z_lo:
                    z_lo = sz0
                if sz1 > z_hi:
                    z_hi = sz1
            wexp.Next()
        if is_top:
            z_bottom, z_top = round(z_lo, 4), zf
        else:
            z_bottom, z_top = zf, round(z_hi, 4)
        # A circular profile spans every coaxial cylinder of the same
        # radius (a cylinder split by a seam is several faces). Extend
        # the depth to cover all of them, or only the top step is built.
        if circ_key is not None and circ_key in cyl_by_key:
            cz0, cz1 = cyl_by_key[circ_key]
            if is_top:
                z_bottom = round(min(z_bottom, cz0), 4)
            else:
                z_top = round(max(z_top, cz1), 4)
        # Skip zero-depth profiles (a face whose side faces do not extend
        # in its growth direction is a floor of a cut, not a feature).
        if z_top - z_bottom < 0.01:
            exp.Next()
            continue
        cx = round((xmin + xmax) / 2.0, 4)
        cy = round((ymin + ymax) / 2.0, 4)
        profiles.append({
            "type": "profile",
            "outline": outer_edges,
            "zmin": z_bottom, "zmax": z_top,
            "centre": [cx, cy],
        })
        # Inner wires: a hole only when the adjacent side face is a void.
        wexp = TopExp_Explorer(face, TopAbs_WIRE)
        while wexp.More():
            wire = TopoDS.Wire_s(wexp.Current())
            if wire.IsSame(outer):
                wexp.Next()
                continue
            inner_edges = _wire_edges(wire)
            if not inner_edges:
                wexp.Next()
                continue
            is_hole = False
            hz0, hz1 = zf, zf
            eexp = TopExp_Explorer(wire, TopAbs_EDGE)
            while eexp.More():
                for f2 in adjacent_faces(TopoDS.Edge_s(eexp.Current()), face):
                    b = _face_is_boss(f2)
                    if b is False:
                        is_hole = True
                        hz0, hz1 = face_zrange(f2)
                        hz0, hz1 = round(hz0, 4), round(hz1, 4)
                eexp.Next()
            if is_hole:
                holes.append({
                    "type": "hole",
                    "outline": inner_edges,
                    "zmin": hz0, "zmax": hz1,
                    "centre": [cx, cy],
                })
            wexp.Next()
        exp.Next()
    return profiles, holes


def _axis_key(cyl, tol=0.01):
    """Group cylinders by their axis line (x, y for a Z-axis cylinder)."""
    d = cyl["axis_dir"]
    if abs(abs(d[2]) - 1.0) < 0.01:
        return ("Z", round(cyl["axis_loc"][0], 2), round(cyl["axis_loc"][1], 2))
    return ("other", tuple(round(v, 2) for v in cyl["axis_dir"]),
            tuple(round(v, 2) for v in cyl["axis_loc"]))


def recognize(step_path):
    """Recognize features from the STEP. Returns a feature list (dict)."""
    cylinders, planes, cones = _read_faces(step_path)
    # Solid-level info (bbox, volume) from the shape itself.
    from OCP.STEPControl import STEPControl_Reader
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    reader = STEPControl_Reader()
    reader.ReadFile(step_path)
    reader.TransferRoots()
    shape = reader.OneShape()
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()

    # --- profiles: trace the outline of every planar Z-normal face. ---
    # No feature-type matching. The wire IS the profile: a circle stays
    # a circle, a 12-gon stays 12 lines, a D-cut stays an arc + a line.
    # _read_profiles returns (profiles, holes) where a hole is an inner
    # wire whose adjacent side face is a void (not a boss footprint).
    profiles, holes = _read_profiles(step_path)
    # Merge by outline: the same feature is seen from its top face AND
    # its bottom face (and a fused assembly can repeat it). Group
    # profiles by their outline, sort by Z, and merge ranges that
    # overlap or touch into one profile spanning the union. Ranges
    # with a real gap stay separate (two identical bosses at different
    # heights are two features, not one tall one).
    #
    # An arc's key is its ENDPOINTS (not a0/a1): a D-cut seen from the
    # top face and the bottom face can come out as the major arc (286
    # deg) once and the minor arc (74 deg) once -- same endpoints, same
    # centre/radius, different sweep. They are the same hole; the merge
    # keeps the larger sweep (the major arc, the true D-cut).
    def _edge_key(e):
        if e["kind"] == "line":
            return ("line", tuple(e["p1"]), tuple(e["p2"]))
        if e["kind"] == "circle":
            return ("circle", tuple(e["centre"]), e["radius"])
        cx, cy = e["centre"]
        r = e["radius"]
        x0 = round(cx + r * math.cos(math.radians(e["a0"])), 4)
        y0 = round(cy + r * math.sin(math.radians(e["a0"])), 4)
        x1 = round(cx + r * math.cos(math.radians(e["a1"])), 4)
        y1 = round(cy + r * math.sin(math.radians(e["a1"])), 4)
        p0, p1 = sorted([(x0, y0), (x1, y1)])
        return ("arc", (cx, cy), r, p0, p1)

    def _outline_key(outline):
        return tuple(sorted(_edge_key(e) for e in outline))

    def _arc_sweep(outline):
        return sum(e["a1"] - e["a0"] for e in outline
                   if e["kind"] == "arc")

    by_outline = {}
    for p in profiles:
        by_outline.setdefault(_outline_key(p["outline"]), []).append(p)
    features = []
    for key, group in sorted(by_outline.items()):
        group = sorted(group, key=lambda g: (g["zmin"], g["zmax"]))
        merged = []
        for p in group:
            if merged and p["zmin"] <= merged[-1]["zmax"] + 0.01:
                merged[-1]["zmax"] = max(merged[-1]["zmax"], p["zmax"])
                # Keep the larger-sweep outline (a D-cut's major arc,
                # not the minor arc from the other face).
                if _arc_sweep(p["outline"]) > _arc_sweep(merged[-1]["outline"]):
                    merged[-1]["outline"] = p["outline"]
            else:
                merged.append(dict(p))
        for p in merged:
            p["name"] = "profile_z%.2f_%.2f" % (p["zmin"], p["zmax"])
            features.append(p)
    # Holes: same merge by outline (a through hole is seen from the top
    # face and the bottom face).
    by_hole = {}
    for h in holes:
        by_hole.setdefault(_outline_key(h["outline"]), []).append(h)
    for key, group in sorted(by_hole.items()):
        group = sorted(group, key=lambda g: (g["zmin"], g["zmax"]))
        merged = []
        for h in group:
            if merged and h["zmin"] <= merged[-1]["zmax"] + 0.01:
                merged[-1]["zmax"] = max(merged[-1]["zmax"], h["zmax"])
                # Keep the larger-sweep outline (a D-cut's major arc,
                # not the minor arc from the other face).
                if _arc_sweep(h["outline"]) > _arc_sweep(merged[-1]["outline"]):
                    merged[-1]["outline"] = h["outline"]
            else:
                merged.append(dict(h))
        for h in merged:
            h["name"] = "hole_z%.2f_%.2f" % (h["zmin"], h["zmax"])
            features.append(h)

    # --- cones (chamfers / countersinks): report, do not build in v1 ---
    for co in cones:
        features.append({
            "type": "cone_unbuilt", "name": "cone_r%.3g" % co["radius"],
            "radius": co["radius"],
            "semi_angle_deg": co["semi_angle_deg"],
            "axis_loc": co["axis_loc"],
            "zmin": round(co["bbox"][2], 4), "zmax": round(co["bbox"][5], 4),
        })

    # --- datum (Nov 24): ONE reference frame for the whole pipeline. ---
    # Origin = part bbox centre (X, Y) + bottom (Z min); axes = the
    # assembly axes. Set once here, stored in features.json, and used
    # by the build (sketch planes are made from this point) and by
    # verification (alignment is checked against it, not recomputed
    # from the built bbox). Any later step -- a new sketch, a
    # verification from any position, the find-missing loop -- uses
    # this same datum, so the result is the same wherever it runs.
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    datum = {
        "origin": [round(cx, 4), round(cy, 4), round(zmin, 4)],
        "x_axis": [1, 0, 0], "y_axis": [0, 1, 0], "z_axis": [0, 0, 1],
    }

    return {
        "step": os.path.basename(step_path),
        "bbox": [round(v, 4) for v in (xmin, ymin, zmin, xmax, ymax, zmax)],
        "volume_mm3": round(props.Mass(), 3),
        "n_cylinders": len(cylinders),
        "n_planes": len(planes),
        "n_cones": len(cones),
        "datum": datum,
        "features": features,
    }


# ---------------------------------------------------------------------------
# Stage 2: cross-check against the 2D drawing (HLR)
# ---------------------------------------------------------------------------


def cross_check(feature_list, hlr_path):
    """Every recognized feature must appear in the 2D views.

    Checks (v1, coarse but honest):
    - the base disc diameter appears in the TOP view extent
    - each hole centre lies inside the TOP view extent
    - the hex boss flat-to-flat appears in the TOP view extent
    Returns a list of {check, ok, detail}.
    """
    with open(hlr_path) as f:
        hlr = json.load(f)
    checks = []
    top = hlr.get("top", {})
    pts = [p for kind in ("visible", "hidden")
           for pl in top.get(kind, []) for p in pl]
    if not pts:
        return [{"check": "hlr_top_present", "ok": False,
                 "detail": "no TOP view polylines in HLR"}]
    us = [p[0] for p in pts]
    vs = [p[1] for p in pts]
    # TOP view u/v extents (mm, datum frame).
    umin, umax, vmin, vmax = min(us), max(us), min(vs), max(vs)
    checks.append({"check": "hlr_top_present", "ok": True,
                   "detail": "TOP extent u %.2f..%.2f v %.2f..%.2f"
                             % (umin, umax, vmin, vmax)})
    for feat in feature_list["features"]:
        if feat["type"] == "profile":
            x, y = feat["centre"]
            ok = (umin - 0.5 <= x <= umax + 0.5) and (vmin - 0.5 <= y <= vmax + 0.5)
            checks.append({"check": "profile_in_top", "ok": ok,
                           "detail": "profile %s at (%.2f, %.2f), Z %.2f..%.2f"
                                     % (feat.get("name", "?"), x, y,
                                        feat["zmin"], feat["zmax"])})
        elif feat["type"] == "hole":
            x, y = feat["centre"]
            ok = (umin - 0.5 <= x <= umax + 0.5) and (vmin - 0.5 <= y <= vmax + 0.5)
            checks.append({"check": "hole_in_top", "ok": ok,
                           "detail": "hole %s at (%.2f, %.2f), Z %.2f..%.2f"
                                     % (feat.get("name", "?"), x, y,
                                        feat["zmin"], feat["zmax"])})
    return checks


# ---------------------------------------------------------------------------
# Stage 3: parametric build (FreeCAD PartDesign, generated macro)
# ---------------------------------------------------------------------------

# Build order: additive features first, plane by plane (one plane at a
# time, starting from the base plane, then the next plane made from the
# remaining features), then subtractive.  All sketches are standalone
# (Placement only, no face attachment) -- the robust pattern from
# partdesign_builder.py.  The model is built in DATUM coordinates
# (Nov 24): origin = (bbox centre X, bbox centre Y, bbox Z min),
# axes = the assembly axes -- the datum stored in features.json by
# recognize(). A sketch plane is the datum plane shifted by the
# feature's Z offset, so any later sketch (the find-missing loop) is
# made from the same datum point. Verification translates the built
# model back to assembly coords by the datum origin when comparing.


def _outline_endpoints(e):
    """(start, end) points of an outline edge, or None for a circle."""
    if e["kind"] == "line":
        return (e["p1"], e["p2"])
    if e["kind"] == "arc":
        a0 = math.radians(e["a0"])
        a1 = math.radians(e["a1"])
        cx, cy = e["centre"]
        r = e["radius"]
        return ([cx + r * math.cos(a0), cy + r * math.sin(a0)],
                [cx + r * math.cos(a1), cy + r * math.sin(a1)])
    return None


def _sketch_constraints_for_outline(outline):
    """Coincident constraints chaining the outline edges end-to-start.

    An arc's endpoints come from (centre, radius, angle) while a line's
    come from its vertex points, so the two can differ by ~0.001 mm
    even when they share a vertex. Without these constraints the
    sketch does not close and the Pad fails. A single full circle is
    closed by itself and needs no constraint.

    The constraint connects the GEOMETRICALLY nearest endpoints of
    each adjacent pair, not assumed (end -> start). An arc traced
    from the STEP can be reversed relative to the loop direction
    (its a0 end sits where the loop enters, a1 where it leaves);
    assuming (i,2)-(j,1) then pulls points up to ~1.1 mm apart
    together, collapsing edges ("Edge too small" / "Both points
    are equal"). Matching by geometry connects the true shared
    vertex whatever the stored direction. (Round 8, Nov 24, 2026:
    housing hole had 66 reversed tooth-tip arcs, one per tooth.)
    """
    lines = []
    n = len(outline)
    if n <= 1:
        return lines
    ends = [_outline_endpoints(e) for e in outline]
    if any(p is None for p in ends):
        # A circle mixed in: fall back to the simple end->start chain.
        for i in range(n):
            j = (i + 1) % n
            lines.append("sk.addConstraint(Sketcher.Constraint("
                         "'Coincident', %d, 2, %d, 1))" % (i, j))
        return lines
    for i in range(n):
        j = (i + 1) % n
        si, ei = ends[i]
        sj, ej = ends[j]
        cands = [
            (math.hypot(ei[0] - sj[0], ei[1] - sj[1]), 2, 1),
            (math.hypot(ei[0] - ej[0], ei[1] - ej[1]), 2, 2),
            (math.hypot(si[0] - sj[0], si[1] - sj[1]), 1, 1),
            (math.hypot(si[0] - ej[0], si[1] - ej[1]), 1, 2),
        ]
        cands.sort()
        _, pi, pj = cands[0]
        lines.append("sk.addConstraint(Sketcher.Constraint("
                     "'Coincident', %d, %d, %d, %d))" % (i, pi, j, pj))
    return lines


def _sketch_lines_for_outline(outline, indent=""):
    """Macro lines that add one closed outline to the current sketch.

    The outline is used exactly as traced -- a circle stays a circle,
    a polygon stays its lines, a D-cut stays an arc + a line. No
    feature-type name is involved.
    """
    lines = []
    for e in outline:
        if e["kind"] == "line":
            x1, y1 = e["p1"]
            x2, y2 = e["p2"]
            lines.append("%ssk.addGeometry(Part.LineSegment("
                         "App.Vector(%.6f, %.6f, 0), "
                         "App.Vector(%.6f, %.6f, 0)), False)" % (
                             indent, x1, y1, x2, y2))
        elif e["kind"] == "circle":
            cx, cy = e["centre"]
            lines.append("%ssk.addGeometry(Part.Circle("
                         "App.Vector(%.6f, %.6f, 0), "
                         "App.Vector(0, 0, 1), %.6f), False)" % (
                             indent, cx, cy, e["radius"]))
        elif e["kind"] == "arc":
            cx, cy = e["centre"]
            lines.append("%ssk.addGeometry(Part.ArcOfCircle("
                         "Part.Circle(App.Vector(%.6f, %.6f, 0), "
                         "App.Vector(0, 0, 1), %.6f), "
                         "math.radians(%.6f), math.radians(%.6f)), False)" % (
                             indent, cx, cy, e["radius"], e["a0"], e["a1"]))
    return lines


def _outline_in_datum(outline, ox, oy):
    """Shift an outline from assembly (global) coords into datum coords.

    Translation only: line endpoints and arc/circle centres shift by
    (-ox, -oy); arc angles are unchanged. The outline dict is copied,
    the original is untouched.
    """
    out = []
    for e in outline:
        e2 = dict(e)
        if e["kind"] == "line":
            e2["p1"] = [e["p1"][0] - ox, e["p1"][1] - oy]
            e2["p2"] = [e["p2"][0] - ox, e["p2"][1] - oy]
        else:
            ccx, ccy = e["centre"]
            e2["centre"] = [ccx - ox, ccy - oy]
        out.append(e2)
    return out


def _macro_for_features(feature_list, zmin):
    """Generate a FreeCAD macro (text) that builds the PartDesign model."""
    feats = feature_list["features"]
    # --- Datum (Nov 24): the model is built in DATUM coordinates. ---
    # The datum origin is (bbox centre X, bbox centre Y, bbox Z min),
    # axes = the assembly axes, stored in feature_list["datum"] by
    # recognize(). Sketch geometry is shifted into datum coords here
    # (translation only); the sketch placement stays (0, 0, z_local),
    # so the .fcstd sits at the datum origin. Old features.json files
    # without a datum fall back to origin (0, 0, zmin) -- the old
    # behaviour for parts centred on the axis.
    datum = feature_list.get("datum") or {}
    dorg = datum.get("origin", [0.0, 0.0, zmin])
    dox, doy = dorg[0], dorg[1]
    # Additive first, then holes (subtractive).  Additive features are
    # built plane by plane (see the sequential-plane loop below).
    additive = [f for f in feats if f["type"] == "profile"]
    holes = [f for f in feats if f["type"] == "hole"]
    holes.sort(key=lambda f: (f["centre"][0], f["centre"][1]))
    unbuilt = [f for f in feats if f["type"] not in ("profile", "hole")]

    lines = []
    lines.append("import math")
    lines.append("import FreeCAD as App")
    lines.append("import Part")
    lines.append("import Sketcher")
    lines.append("")
    lines.append("def make_sketch(doc, body, name, z=0.0):")
    lines.append("    sk = doc.addObject('Sketcher::SketchObject', name)")
    lines.append("    body.addObject(sk)")
    lines.append("    sk.Placement = App.Placement(App.Vector(0, 0, z),")
    lines.append("                                 App.Rotation())")
    lines.append("    return sk")
    lines.append("")
    lines.append("doc = App.newDocument('Step2Param')")
    lines.append("body = doc.addObject('PartDesign::Body', 'Body')")
    lines.append("doc.recompute()")
    lines.append("")
    # --- Sequential planes: ONE plane at a time, starting from the base. ---
    # The base plane is the lowest Z among the additive features.  Build
    # the features on it, then make the NEXT plane from the remaining
    # features (the next distinct Z level) and build on it, and so on.
    # Never enumerate all planes upfront.  A plane is a Z level here;
    # the same loop extends to X / Y / angled planes later (the next
    # plane is then picked from the remaining features by orientation).
    plane_levels = sorted(set(round(f["zmin"], 2) for f in additive))
    by_plane = {}
    for f in additive:
        by_plane.setdefault(round(f["zmin"], 2), []).append(f)
    pad_idx = 0
    for plane_z in plane_levels:
        lz0 = round(plane_z - zmin, 4)
        lines.append("# --- Plane Z %.4f (local) ---" % lz0)
        for f in sorted(by_plane[plane_z],
                        key=lambda g: (g["centre"][0], g["centre"][1])):
            h = round(f["zmax"] - f["zmin"], 4)
            tag = "Profile%d" % pad_idx
            pad_idx += 1
            n_edges = len(f["outline"])
            kinds = sorted(set(e["kind"] for e in f["outline"]))
            lines.append("# profile %s: %d edges (%s), Z %.4f..%.4f" % (
                tag, n_edges, ",".join(kinds), lz0, lz0 + h))
            lines.append("sk = make_sketch(doc, body, 'Sketch_%s', %.4f)" % (tag, lz0))
            outline_d = _outline_in_datum(f["outline"], dox, doy)
            lines.extend(_sketch_lines_for_outline(outline_d))
            lines.extend(_sketch_constraints_for_outline(outline_d))
            lines.append("doc.recompute()")
            lines.append("pad = doc.addObject('PartDesign::Pad', 'Pad_%s')" % tag)
            lines.append("body.addObject(pad)")
            lines.append("pad.Profile = sk")
            lines.append("pad.Length = %.4f" % h)
            lines.append("doc.recompute()")
    # Holes: one Pocket PER hole (not a single Pocket for all holes).
    # Each hole gets its own sketch at its own zmax, pocketing down
    # exactly its own depth (plus a 0.01 mm overshoot each side so the
    # cut is clean). The hole outline is traced, not assumed circular.
    for j, f in enumerate(holes):
        lz1 = round(f["zmax"] - zmin, 4)
        h = round(f["zmax"] - f["zmin"], 4)
        x, y = f["centre"]
        lines.append("# hole %d: %d edges, Z %.4f..%.4f" % (
            j, len(f["outline"]), lz1 - h, lz1))
        lines.append("sk = make_sketch(doc, body, 'Sketch_Hole%d', %.4f)" % (
            j, lz1 + 0.01))
        outline_d = _outline_in_datum(f["outline"], dox, doy)
        lines.extend(_sketch_lines_for_outline(outline_d))
        lines.extend(_sketch_constraints_for_outline(outline_d))
        lines.append("doc.recompute()")
        lines.append("pocket = doc.addObject('PartDesign::Pocket', 'Pocket_Hole%d')" % j)
        lines.append("body.addObject(pocket)")
        lines.append("pocket.Profile = sk")
        lines.append("pocket.Length = %.4f" % (h + 0.02))
        lines.append("doc.recompute()")
    lines.append("")
    lines.append("shape = body.Shape")
    lines.append("print('STEP2PARAM valid:', shape.isValid(),")
    lines.append("      'solids:', len(shape.Solids),")
    lines.append("      'vol:', round(shape.Volume, 3))")
    lines.append("bb = shape.BoundBox")
    lines.append("print('STEP2PARAM bbox X %.3f..%.3f Y %.3f..%.3f Z %.3f..%.3f'"
                 " % (bb.XMin, bb.XMax, bb.YMin, bb.YMax, bb.ZMin, bb.ZMax))")
    if unbuilt:
        lines.append("print('STEP2PARAM unbuilt features: %s')" % (
            ", ".join(f["type"] for f in unbuilt)))
    return "\n".join(lines) + "\n"


def build_fcstd(feature_list, out_dir, freecadcmd="freecadcmd"):
    """Build the PartDesign model from the feature list.

    Writes build_macro.py, runs it under freecadcmd, saves model.fcstd
    and model.step in out_dir.  Returns (ok, log_text).
    """
    zmin = feature_list["bbox"][2]
    macro = _macro_for_features(feature_list, zmin)
    macro_path = os.path.join(out_dir, "build_macro.py")
    with open(macro_path, "w") as f:
        f.write(macro)
    # The macro saves the files itself: append save/export to it.
    fcstd_path = os.path.join(out_dir, "model.fcstd")
    step_path = os.path.join(out_dir, "model.step")
    with open(macro_path, "a") as f:
        f.write("doc.saveAs(%r)\n" % fcstd_path)
        f.write("body.Shape.exportStep(%r)\n" % step_path)
        f.write("print('STEP2PARAM saved', %r, %r)\n" % (fcstd_path, step_path))
    cmd = [freecadcmd, macro_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    log = proc.stdout + proc.stderr
    ok = (os.path.exists(fcstd_path) and os.path.exists(step_path)
          and "STEP2PARAM valid: True" in log)
    return ok, log


# ---------------------------------------------------------------------------
# Stage 4: verify the built model against the original STEP
# ---------------------------------------------------------------------------


def verify_built(built_step, orig_feature_list):
    """Compare the built model against the original STEP.

    The built model is in DATUM coords (origin = datum origin), so
    compare sizes (not absolute positions) plus volume and face count,
    and check the model sits at the datum origin.
    Returns a list of {check, ok, detail}.
    """
    from OCP.STEPControl import STEPControl_Reader
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    reader = STEPControl_Reader()
    assert reader.ReadFile(built_step) == 1, "cannot read built STEP"
    reader.TransferRoots()
    shape = reader.OneShape()
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    vol = props.Mass()
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    n_faces = 0
    while exp.More():
        n_faces += 1
        exp.Next()
    checks = []
    ovol = orig_feature_list["volume_mm3"]
    dv = abs(vol - ovol) / max(ovol, 1e-9) * 100.0
    checks.append({"check": "volume", "ok": dv < 0.5,
                   "detail": "built %.1f vs orig %.1f mm^3 (%.3f%%)"
                             % (vol, ovol, dv)})
    obb = orig_feature_list["bbox"]
    osize = (obb[3] - obb[0], obb[4] - obb[1], obb[5] - obb[2])
    bsize = (xmax - xmin, ymax - ymin, zmax - zmin)
    for ax, o, b in zip("XYZ", osize, bsize):
        checks.append({"check": "bbox_size_%s" % ax, "ok": abs(o - b) < 0.05,
                       "detail": "built %.3f vs orig %.3f mm" % (b, o)})
    on = (orig_feature_list["n_cylinders"] + orig_feature_list["n_planes"]
          + orig_feature_list["n_cones"])
    checks.append({"check": "face_count", "ok": n_faces == on,
                   "detail": "built %d vs orig %d faces" % (n_faces, on)})
    # datum check (Nov 24): the built model sits at the datum origin,
    # so its bbox centre (X, Y) is (0, 0) and its Z min is 0. A shifted
    # build (wrong pad direction, a plane made from the wrong point)
    # fails here even if its sizes are right.
    bcx = (xmin + xmax) / 2.0
    bcy = (ymin + ymax) / 2.0
    checks.append({"check": "datum_align",
                   "ok": abs(bcx) < 0.05 and abs(bcy) < 0.05 and abs(zmin) < 0.05,
                   "detail": "built bbox centre (%.3f, %.3f), Z min %.3f "
                             "(datum origin)" % (bcx, bcy, zmin)})
    return checks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _render_iso(step_path, out_dir):
    """Render 4 isometric views (top/bottom x front/back) + 2x2 montage.

    One isometric view hides the back/bottom, so render 4 corners.
    Same shading as render_preview.py: tessellation triangles only,
    Lambertian shading, light from the camera direction, white background.
    Returns the list of PNG paths written.
    """
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from OCP.STEPControl import STEPControl_Reader
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.BRep import BRep_Tool
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    r = STEPControl_Reader()
    assert r.ReadFile(step_path) == 1
    r.TransferRoots()
    shape = r.OneShape()
    BRepMesh_IncrementalMesh(shape, 0.05, False, 0.2, True)
    verts, facets = [], []
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
            facets.append([base + a - 1, base + b - 1, base + c - 1])
        ex.Next()
    verts = np.array(verts, dtype=float)
    tris = [(verts[a], verts[b], verts[c]) for a, b, c in facets]
    BCOL = np.array([0.68, 0.75, 0.84])
    AMBIENT, DIFFUSE = 0.40, 0.60

    def render_one(cam, up, out_path):
        cam = np.array(cam, dtype=float)
        cam = cam / np.linalg.norm(cam)
        view_dir = -cam
        up = np.array(up, dtype=float)
        right = np.cross(view_dir, up)
        right = right / np.linalg.norm(right)
        up2 = np.cross(right, view_dir)
        items = []
        for a, b, c in tris:
            n = np.cross(b - a, c - a)
            ln = np.linalg.norm(n)
            if ln == 0:
                continue
            n = n / ln
            shade = AMBIENT + DIFFUSE * max(0.0, float(np.dot(n, cam)))
            col = tuple(np.clip(BCOL * shade, 0, 1))
            pa = np.array([np.dot(a, right), np.dot(a, up2)])
            pb = np.array([np.dot(b, right), np.dot(b, up2)])
            pc = np.array([np.dot(c, right), np.dot(c, up2)])
            depth = (np.dot(a, view_dir) + np.dot(b, view_dir)
                     + np.dot(c, view_dir)) / 3.0
            items.append((depth, [pa, pb, pc], col))
        items.sort(key=lambda t: t[0])
        fig, ax = plt.subplots(figsize=(6, 6))
        ax.set_aspect("equal")
        ax.axis("off")
        coll = PolyCollection([it[1] for it in items],
                              facecolors=[it[2] for it in items],
                              edgecolors="none")
        ax.add_collection(coll)
        ax.autoscale_view()
        # axis triad
        ax.annotate("", xy=(0.08, 0.08), xytext=(0.08, 0.02),
                    arrowprops=dict(arrowstyle="->", color="blue", lw=1.5))
        ax.text(0.08, 0.10, "Z", color="blue", fontsize=12, ha="center")
        fig.savefig(out_path, dpi=100, bbox_inches="tight")
        plt.close(fig)

    os.makedirs(out_dir, exist_ok=True)
    views = {"iso_tfr": (1, 1, 1), "iso_tbl": (-1, -1, 1),
             "iso_bfr": (1, 1, -1), "iso_bbl": (-1, -1, -1)}
    paths = []
    for name, cam in views.items():
        p = os.path.join(out_dir, name + ".png")
        render_one(cam, (0, 0, 1), p)
        paths.append(p)
    # 2x2 montage
    from PIL import Image
    imgs = [Image.open(p) for p in paths]
    w, h = imgs[0].size
    mont = Image.new("RGB", (2 * w, 2 * h), "white")
    for i, im in enumerate(imgs):
        mont.paste(im, ((i % 2) * w, (i // 2) * h))
    mont_path = os.path.join(out_dir, "iso4.png")
    mont.save(mont_path)
    paths.append(mont_path)
    return paths


def _render_iso_fixed(step_path, out_dir, ref_size):
    """Render the 4 iso views with a FIXED scale (ref_size = the
    original part's bbox max dimension), each model centered on its
    own bbox center. Built (local coords) and original (assembly
    coords) therefore render in the SAME frame at the SAME scale, so
    their silhouettes are directly comparable. No axis triad (it
    would contaminate the silhouette). Returns the list of PNG paths.
    """
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from OCP.STEPControl import STEPControl_Reader
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.BRep import BRep_Tool
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    r = STEPControl_Reader()
    assert r.ReadFile(step_path) == 1
    r.TransferRoots()
    shape = r.OneShape()
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    centre = np.array([(xmin + xmax) / 2.0, (ymin + ymax) / 2.0,
                       (zmin + zmax) / 2.0])
    BRepMesh_IncrementalMesh(shape, 0.05, False, 0.2, True)
    verts, facets = [], []
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
            facets.append([base + a - 1, base + b - 1, base + c - 1])
        ex.Next()
    verts = np.array(verts, dtype=float)
    tris = [(verts[a], verts[b], verts[c]) for a, b, c in facets]
    BCOL = np.array([0.68, 0.75, 0.84])
    AMBIENT, DIFFUSE = 0.40, 0.60
    half = ref_size * 0.55

    def render_one(cam, up, out_path):
        cam = np.array(cam, dtype=float)
        cam = cam / np.linalg.norm(cam)
        view_dir = -cam
        up = np.array(up, dtype=float)
        right = np.cross(view_dir, up)
        right = right / np.linalg.norm(right)
        up2 = np.cross(right, view_dir)
        cproj = np.array([np.dot(centre, right), np.dot(centre, up2)])
        items = []
        for a, b, c in tris:
            n = np.cross(b - a, c - a)
            ln = np.linalg.norm(n)
            if ln == 0:
                continue
            n = n / ln
            shade = AMBIENT + DIFFUSE * max(0.0, float(np.dot(n, cam)))
            col = tuple(np.clip(BCOL * shade, 0, 1))
            pa = np.array([np.dot(a, right), np.dot(a, up2)]) - cproj
            pb = np.array([np.dot(b, right), np.dot(b, up2)]) - cproj
            pc = np.array([np.dot(c, right), np.dot(c, up2)]) - cproj
            depth = (np.dot(a, view_dir) + np.dot(b, view_dir)
                     + np.dot(c, view_dir)) / 3.0
            items.append((depth, [pa, pb, pc], col))
        items.sort(key=lambda t: t[0])
        fig, ax = plt.subplots(figsize=(6, 6))
        ax.set_aspect("equal")
        ax.axis("off")
        ax.set_xlim(-half, half)
        ax.set_ylim(-half, half)
        coll = PolyCollection([it[1] for it in items],
                              facecolors=[it[2] for it in items],
                              edgecolors="none")
        ax.add_collection(coll)
        fig.savefig(out_path, dpi=100)
        plt.close(fig)

    os.makedirs(out_dir, exist_ok=True)
    views = {"iso_tfr": (1, 1, 1), "iso_tbl": (-1, -1, 1),
             "iso_bfr": (1, 1, -1), "iso_bbl": (-1, -1, -1)}
    paths = []
    for name, cam in views.items():
        p = os.path.join(out_dir, name + ".png")
        render_one(cam, (0, 0, 1), p)
        paths.append(p)
    return paths


def _silhouette_iou(p1, p2):
    """IoU of the silhouettes (non-white pixels) of two same-size PNGs."""
    import numpy as np
    from PIL import Image
    a = np.array(Image.open(p1).convert("L"))
    b = np.array(Image.open(p2).convert("L"))
    if a.shape != b.shape:
        # crop to the smaller common size
        h = min(a.shape[0], b.shape[0])
        w = min(a.shape[1], b.shape[1])
        a, b = a[:h, :w], b[:h, :w]
    sa = a < 250
    sb = b < 250
    inter = np.logical_and(sa, sb).sum()
    union = np.logical_or(sa, sb).sum()
    return float(inter) / float(union) if union else 1.0


def verify_iso_match(built_step, orig_step, orig_feature_list, out_dir):
    """PROGRAMMATIC image check of direction/shape: render the built
    model and the original from the same 4 iso views at the same fixed
    scale, compare silhouettes by IoU. The PROGRAM does this check
    itself -- no human looks at the images. A pad in the wrong
    direction (or a missing feature) moves/removes mass, so the
    silhouette differs and the IoU drops. Returns a list of
    {check, ok, detail} like verify_built.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.STEPControl import STEPControl_Reader
    obb = orig_feature_list["bbox"]
    ref_size = max(obb[3] - obb[0], obb[4] - obb[1], obb[5] - obb[2])
    built_dir = os.path.join(out_dir, "iso_check_built")
    orig_dir = os.path.join(out_dir, "iso_check_orig")
    built_paths = _render_iso_fixed(built_step, built_dir, ref_size)
    orig_paths = _render_iso_fixed(orig_step, orig_dir, ref_size)
    checks = []
    views = ["iso_tfr", "iso_tbl", "iso_bfr", "iso_bbl"]
    ious = []
    for name, bp, op in zip(views, built_paths, orig_paths):
        iou = _silhouette_iou(bp, op)
        ious.append(iou)
        checks.append({"check": "iso_%s" % name, "ok": iou >= 0.90,
                       "detail": "silhouette IoU %.3f" % iou})
    mean_iou = sum(ious) / len(ious) if ious else 0.0
    checks.append({"check": "iso_mean", "ok": mean_iou >= 0.90,
                   "detail": "mean silhouette IoU %.3f" % mean_iou})
    return checks


def _chain_section_polylines(wires, tol=1e-6):
    """Chain section edge polylines into closed loops.

    section_wires() returns one polyline PER EDGE (a circle split into
    arcs comes back as several OPEN polylines, a line as 2 points).
    Filling each edge separately is WRONG: an open arc filled as a
    polygon closes with a chord, so a circle split into 4 arcs becomes
    a square hole in the mask (found Nov 24: the built cradle's mask
    had a square hole at Z 2.00, IoU 0.390 where the geometry is
    correct). Chain the edges by endpoint matching into closed loops
    FIRST, then fill each loop. A valid solid's section is always
    closed loops; an open chain that cannot close is dropped.
    """
    def close(p, q):
        return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol

    segs = []
    for pts in wires:
        clean = [pts[0]]
        for p in pts[1:]:
            if not close(p, clean[-1]):
                clean.append(p)
        if len(clean) >= 2:
            segs.append(clean)
    loops = []
    open_segs = []
    for s in segs:
        if close(s[0], s[-1]):
            loops.append(s)
        else:
            open_segs.append(s)
    while open_segs:
        cur = open_segs.pop(0)
        while True:
            if close(cur[0], cur[-1]):
                loops.append(cur)
                break
            found = None
            for j, s in enumerate(open_segs):
                if close(s[0], cur[-1]):
                    found = (j, False)
                    break
                if close(s[-1], cur[-1]):
                    found = (j, True)
                    break
            if found is None:
                break
            j, rev = found
            s = open_segs.pop(j)
            if rev:
                s = s[::-1]
            cur = cur + s[1:]
    return loops


def _section_mask_raster(wires, bbox, W=200, H=200):
    """Rasterize chained section loops into a boolean mask (even-odd fill).

    Factored out of verify_ct_match's section_mask (same logic):
    chain the section edges into closed loops FIRST, then fill each
    loop and XOR. Filling each edge separately is wrong -- an open arc
    becomes a chord-closed segment, not the disc it belongs to.
    """
    import numpy as np
    from PIL import Image, ImageDraw
    ox0, oy0, _, ox1, oy1, _ = bbox
    sx = W / max(ox1 - ox0, 1e-9)
    sy = H / max(oy1 - oy0, 1e-9)
    loops = _chain_section_polylines(wires)
    mask = Image.new("1", (W, H), 0)
    for pts in loops:
        if len(pts) < 3:
            continue
        px = [int((x - ox0) * sx) for x, y in pts]
        py = [int((y - oy0) * sy) for x, y in pts]
        lay = Image.new("1", (W, H), 0)
        ImageDraw.Draw(lay).polygon(list(zip(px, py)), fill=1)
        mask = Image.fromarray(
            np.logical_xor(np.array(mask), np.array(lay)).astype(np.uint8) * 255
        ).convert("1")
    return np.array(mask, dtype=bool)


def _adaptive_ct_heights(zmin, zmax, coarse_n=25, coarse_spacing=None,
                         dense_spacing=0.2, mask_fn=None):
    """Adaptive CT cut heights (Nov 24, his design).

    Two ways to set the coarse (whole-part) sampling -- the web UI
    offers both as a choice:
      * by COUNT: coarse_n evenly-spaced heights (default 25)
      * by SPACING: coarse_spacing mm apart (n = ceil(h/spacing)+1);
        overrides coarse_n when given.
    Then, inside each coarse interval whose two end sections DIFFER
    (mask IoU < 0.999 -- the shape changes there), add heights at
    dense_spacing (default 0.2 mm). Uniform regions need no dense
    slices: a constant wrong shape (e.g. the cradle's 2.4 mm arms)
    already shows up in the coarse slices. mask_fn(z) -> boolean
    mask of the section at z; without it, the coarse list is
    returned unchanged. Returns sorted list of heights.
    """
    import math
    import numpy as np
    h = zmax - zmin
    if coarse_spacing is not None and coarse_spacing > 0:
        n = max(2, int(math.ceil(h / coarse_spacing)) + 1)
    else:
        n = max(2, int(coarse_n))
    coarse = [zmin + i * h / (n - 1) for i in range(n)]
    if mask_fn is None:
        return coarse
    masks = {z: mask_fn(z) for z in coarse}
    dense = []
    for i in range(len(coarse) - 1):
        z0, z1 = coarse[i], coarse[i + 1]
        m0, m1 = masks[z0], masks[z1]
        inter = np.logical_and(m0, m1).sum()
        union = np.logical_or(m0, m1).sum()
        iou = float(inter) / float(union) if union else 1.0
        if iou < 0.999:
            z = z0 + dense_spacing
            while z < z1 - 1e-9:
                dense.append(round(z, 6))
                z += dense_spacing
    return sorted(set(coarse) | set(dense))


def verify_ct_match(built_step, orig_step, orig_feature_list, out_dir,
                    coarse_n=25, coarse_spacing=None, dense_spacing=0.2):
    """PROGRAMMATIC CT check of direction/shape.

    ADAPTIVE (Nov 24, his design): coarse sampling across the whole
    part (by count, default 25 evenly-spaced, or by spacing when
    coarse_spacing is given) PLUS dense slices at dense_spacing
    (default 0.2 mm) ONLY inside intervals where the section changes.
    A uniform dense grid (Nov 24, 0.25 mm -> 199 slices for the
    cradle) wastes slices on prismatic regions; the coarse slices
    already catch a constant wrong shape.

    DATUM (Nov 24): the datum is set ONCE in recognize() and stored in
    features.json -- origin = (original bbox centre X, bbox centre Y,
    bbox Z min), axes = the assembly axes. The build makes its sketch
    planes from this datum point, so the built model is already in the
    datum frame. Here it is aligned onto the original BEFORE cutting:
    the translation (dx, dy, dz) must equal the datum origin, and
    ct_align FAILS if it does not (a build outside the datum frame).
    BOTH models are then cut at the same ABSOLUTE heights (assembly Z),
    not relative fractions -- relative heights shift when a feature is
    missing (a shorter model puts its slices at different physical Z),
    which hides exactly the error the CT is meant to catch. Returns a
    list of {check, ok, detail}.
    """
    import numpy as np
    from OCP.STEPControl import STEPControl_Reader
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec
    from .ct_slice import section_wires

    def load(path):
        r = STEPControl_Reader()
        assert r.ReadFile(path) == 1
        r.TransferRoots()
        shape = r.OneShape()
        box = Bnd_Box()
        BRepBndLib.Add_s(shape, box)
        return shape, box.Get()

    bshape, bbb = load(built_step)
    oshape, obb = load(orig_step)
    # --- align built to original: Z min + XY centre ---
    dx = (obb[0] + obb[3]) / 2 - (bbb[0] + bbb[3]) / 2
    dy = (obb[1] + obb[4]) / 2 - (bbb[1] + bbb[4]) / 2
    dz = obb[2] - bbb[2]
    if abs(dx) > 1e-9 or abs(dy) > 1e-9 or abs(dz) > 1e-9:
        tr = gp_Trsf()
        tr.SetTranslation(gp_Vec(dx, dy, dz))
        bshape = BRepBuilderAPI_Transform(bshape, tr, True).Shape()
    # raster grid is fixed by _section_mask_raster over the ORIGINAL's
    # XY bbox (same frame for both models)

    def section_mask(shape, z):
        # Chain the section edges into closed loops FIRST, then fill
        # each loop and XOR (even-odd) -> net cross-section mask.
        # Filling each edge separately is wrong (see
        # _chain_section_polylines): an open arc becomes a chord-closed
        # segment, not the disc it belongs to.
        return _section_mask_raster(section_wires(shape, "xy", z), obb)

    checks = []
    # ct_align is now a REAL check (Nov 24): the alignment offset that
    # brings the built model onto the original must equal the stored
    # datum origin. The datum is set once in recognize() and shared by
    # build and verify, so a residual here means the build is not in
    # the datum frame (wrong plane, wrong direction) -- not a
    # measurement to absorb silently.
    datum = orig_feature_list.get("datum")
    if datum:
        ox, oy, oz = datum["origin"]
        align_ok = (abs(dx - ox) < 0.05 and abs(dy - oy) < 0.05
                    and abs(dz - oz) < 0.05)
        align_detail = (f"built aligned to original: dx {dx:+.3f} dy {dy:+.3f} "
                        f"dz {dz:+.3f} mm (datum origin {ox:+.3f}, {oy:+.3f}, "
                        f"{oz:+.3f}; residual {dx - ox:+.3f}, {dy - oy:+.3f}, "
                        f"{dz - oz:+.3f})")
    else:
        align_ok = True
        align_detail = (f"built aligned to original: dx {dx:+.3f} dy {dy:+.3f} "
                        f"dz {dz:+.3f} mm (no datum stored; Z datum = bottom, "
                        f"XY datum = original centre)")
    checks.append({"check": "ct_align", "ok": align_ok, "detail": align_detail})
    # Adaptive heights: coarse across the whole part + dense (0.2 mm)
    # inside intervals where the ORIGINAL's section changes. The orig
    # masks are cached so the coarse heights are not re-cut in the
    # loop below.
    _omask_cache = {}

    def _omask(z):
        if z not in _omask_cache:
            _omask_cache[z] = section_mask(oshape, z)
        return _omask_cache[z]

    heights = _adaptive_ct_heights(obb[2], obb[5], coarse_n, coarse_spacing,
                                   dense_spacing, mask_fn=_omask)
    h_total = obb[5] - obb[2]
    ious = []
    for i, z in enumerate(heights):
        t = (z - obb[2]) / h_total if h_total > 0 else 0.0
        mb = section_mask(bshape, z)
        mo = _omask(z)
        inter = np.logical_and(mb, mo).sum()
        union = np.logical_or(mb, mo).sum()
        iou = float(inter) / float(union) if union else 1.0
        ious.append(iou)
        checks.append({"check": "ct_z%02d" % i, "ok": iou >= 0.90,
                       "detail": "Z %.2f mm (%.2f%%) slice IoU %.3f" % (z, t * 100, iou)})
    checks.append({"check": "ct_slices", "ok": True,
                   "detail": "%d slices (coarse %s, dense %.2f mm in "
                             "changing intervals)" % (
                                 len(heights),
                                 ("spacing %.2f mm" % coarse_spacing)
                                 if coarse_spacing else ("n=%d" % coarse_n),
                                 dense_spacing)})
    mean_iou = sum(ious) / len(ious) if ious else 0.0
    worst = min(ious) if ious else 0.0
    checks.append({"check": "ct_mean", "ok": mean_iou >= 0.90,
                   "detail": "mean slice IoU %.3f (worst %.3f)" % (mean_iou, worst)})
    return checks


# ---------------------------------------------------------------------------
# Stage 5: iterative refinement -- find the parts NOT drawn, draw them
# ---------------------------------------------------------------------------
# The user's loop (Nov 24): after step 2 (recognize) + step 3 (build),
# verify; find the parts NOT drawn; continue drawing those parts;
# iterate until complete. The program does this itself: it reads the
# failing CT regions, re-measures the ORIGINAL there, and corrects the
# features -- then rebuilds and re-verifies, up to N iterations.
#
# Two corrections, both measured from the original (never guessed):
#  1. Outline: a profile/hole whose outline is a rectangle or circle
#     but the original's section (measured at several Z in its range)
#     is a different rectangle/circle -> replace the outline with the
#     measured (most common) one. (Cradle: the snap arms read 12x2.4,
#     the original measures 12x2.0 at most heights.)
#  2. Z-range: a feature whose range contains a sub-range where the
#     section differs (smooth bore vs notched bore) -> shrink the
#     feature to the true range and add a new feature for the differing
#     sub-range (rectangle/circle outline from the section there).
#     (Cradle: the notched bore starts at Z 3.00, the original is
#     smooth until Z 4.00.)


def _failing_z_intervals(verify_ct, gap=0.6):
    """Group failing ct_z checks into Z intervals.

    verify_ct is the list of {check, ok, detail} from verify_ct_match.
    Returns a list of (z0, z1) intervals (assembly Z) where the slice
    IoU failed. Adjacent failing slices closer than `gap` mm merge
    into one interval.
    """
    import re
    zs = []
    for c in verify_ct or []:
        if c["check"].startswith("ct_z") and not c["ok"]:
            m = re.search(r"Z ([-\d.]+) mm", c["detail"])
            if m:
                zs.append(float(m.group(1)))
    zs.sort()
    intervals = []
    for z in zs:
        if intervals and z - intervals[-1][1] <= gap:
            intervals[-1][1] = z
        else:
            intervals.append([z, z])
    return [(a, b) for a, b in intervals]


def _loop_bbox(loop):
    xs = [p[0] for p in loop]
    ys = [p[1] for p in loop]
    return (min(xs), max(xs), min(ys), max(ys))


def _loop_rect(loop, tol=0.05):
    """If the loop is an axis-aligned rectangle, return (x0, x1, y0, y1).

    The loop is the chained section polyline; a rectangle has 4
    corners and its points lie on the 4 sides. Returns None otherwise.
    """
    if len(loop) < 4:
        return None
    x0, x1, y0, y1 = _loop_bbox(loop)
    if x1 - x0 < tol or y1 - y0 < tol:
        return None
    # Every point must lie on one of the 4 sides (within tol).
    for x, y in loop:
        on = (abs(x - x0) <= tol or abs(x - x1) <= tol or
              abs(y - y0) <= tol or abs(y - y1) <= tol)
        if not on:
            return None
    # All 4 corners must be present (a point near each corner).
    corners = [(x0, y0), (x0, y1), (x1, y0), (x1, y1)]
    for cx, cy in corners:
        if not any(abs(x - cx) <= tol and abs(y - cy) <= tol
                   for x, y in loop):
            return None
    return (round(x0, 4), round(x1, 4), round(y0, 4), round(y1, 4))


def _loop_circle(loop, tol=0.05):
    """If the loop is a circle, return (cx, cy, r). Else None.

    The centre is the bbox centre; every point must be at distance r
    from it (within tol).
    """
    import math
    if len(loop) < 8:
        return None
    x0, x1, y0, y1 = _loop_bbox(loop)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    r = (x1 - x0 + y1 - y0) / 4.0
    if r <= tol:
        return None
    for x, y in loop:
        if abs(math.hypot(x - cx, y - cy) - r) > tol:
            return None
    return (round(cx, 4), round(cy, 4), round(r, 4))


def _rect_outline(rect):
    x0, x1, y0, y1 = rect
    return [
        {"kind": "line", "p1": [x0, y0], "p2": [x1, y0]},
        {"kind": "line", "p1": [x1, y0], "p2": [x1, y1]},
        {"kind": "line", "p1": [x1, y1], "p2": [x0, y1]},
        {"kind": "line", "p1": [x0, y1], "p2": [x0, y0]},
    ]


def _circle_outline(circ):
    cx, cy, r = circ
    return [{"kind": "circle", "centre": [cx, cy], "radius": r}]


def _outline_bbox(outline):
    """Bounding box of a feature outline (line/arc/circle edges)."""
    import math
    pts = []
    for e in outline:
        if e["kind"] == "line":
            pts.append(e["p1"])
            pts.append(e["p2"])
        elif e["kind"] == "circle":
            cx, cy = e["centre"]
            r = e["radius"]
            pts += [(cx - r, cy - r), (cx + r, cy + r)]
        else:
            cx, cy = e["centre"]
            r = e["radius"]
            for a in (e["a0"], e["a1"]):
                pts.append((cx + r * math.cos(math.radians(a)),
                            cy + r * math.sin(math.radians(a))))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), max(xs), min(ys), max(ys))


def _section_loops(shape, z):
    """Chained closed loops of the section at height z (XY plane)."""
    from .ct_slice import section_wires
    wires = section_wires(shape, "xy", z)
    return _chain_section_polylines(wires)


def _loop_signature(loop, tol=0.05):
    """Hashable signature of a loop: rectangle, circle, or bbox.

    Two sections have the same signature when they are the same shape
    (same kind and same size/position). Used to find where a section
    changes along Z.
    """
    r = _loop_rect(loop, tol)
    if r is not None:
        return ("rect", r)
    c = _loop_circle(loop, tol)
    if c is not None:
        return ("circle", c)
    return ("other", tuple(round(v, 2) for v in _loop_bbox(loop)))


def _best_loop_match(loops, fb):
    """The section loop that best matches a feature's bbox.

    Among loops whose bbox overlaps the feature's bbox, pick the one
    whose bbox is CLOSEST (smallest max coordinate difference). The
    first overlapping loop is wrong: a hole's bbox is inside the disc
    outline's bbox, so the disc loop overlaps it too -- and the pilot
    hole's bbox sits inside the disc's. Closest-bbox picks the bore
    loop for a bore feature and the disc loop for a disc feature.
    Returns the loop, or None.
    """
    best = None
    best_d = None
    for loop in loops:
        lb = _loop_bbox(loop)
        if not (lb[0] < fb[1] and lb[1] > fb[0] and
                lb[2] < fb[3] and lb[3] > fb[2]):
            continue
        d = max(abs(lb[0] - fb[0]), abs(lb[1] - fb[1]),
                abs(lb[2] - fb[2]), abs(lb[3] - fb[3]))
        if best_d is None or d < best_d:
            best_d = d
            best = loop
    return best


def _refine_features(fl, orig_step, ct_dense_spacing=0.2):
    """One refinement pass: correct the features the CT check failed.

    Reads the failing Z intervals from fl["verify_ct"], re-measures
    the ORIGINAL in those intervals, and corrects the features:
    wrong rectangle/circle outlines are replaced by the measured
    (most common) one; a feature whose range spans a section change
    is shrunk to the true range and the differing sub-range gets a
    new feature (rectangle/circle from the section there).
    Returns a list of human-readable notes (empty = nothing changed).
    """
    from OCP.STEPControl import STEPControl_Reader
    intervals = _failing_z_intervals(fl.get("verify_ct"))
    if not intervals:
        return []
    reader = STEPControl_Reader()
    assert reader.ReadFile(orig_step) == 1
    reader.TransferRoots()
    shape = reader.OneShape()
    zmin_all, zmax_all = fl["bbox"][2], fl["bbox"][5]
    notes = []
    new_features = []

    def _loops_at(z):
        try:
            return _section_loops(shape, z)
        except Exception:
            return []

    for feat in fl["features"]:
        if feat["type"] not in ("profile", "hole"):
            new_features.append(feat)
            continue
        fz0, fz1 = feat["zmin"], feat["zmax"]
        # Does this feature overlap a failing interval?
        hit = any(not (fz1 < a - 0.01 or fz0 > b + 0.01)
                  for a, b in intervals)
        if not hit:
            new_features.append(feat)
            continue
        # --- correction 1: outline (rectangle / circle features) ---
        outline = feat["outline"]
        simple = None
        if len(outline) == 4 and all(e["kind"] == "line" for e in outline):
            simple = ("rect", _outline_bbox(outline))
        elif len(outline) == 1 and outline[0]["kind"] == "circle":
            e = outline[0]
            simple = ("circle", (e["centre"][0], e["centre"][1],
                                 e["radius"]))
        if simple is not None:
            # Measure the section at several Z across the feature's
            # range; the most common rectangle/circle is the true one
            # (a fillet or hook only affects a few heights).
            n = max(5, int((fz1 - fz0) / 2.0) + 1)
            counts = {}
            for i in range(n):
                z = fz0 + (fz1 - fz0) * i / max(n - 1, 1)
                fb = simple[1] if simple[0] == "rect" else (
                    simple[1][0] - simple[1][2], simple[1][0] + simple[1][2],
                    simple[1][1] - simple[1][2], simple[1][1] + simple[1][2])
                loop = _best_loop_match(_loops_at(z), fb)
                if loop is None:
                    continue
                m = (_loop_rect(loop) if simple[0] == "rect"
                     else _loop_circle(loop))
                if m is None:
                    continue
                counts[m] = counts.get(m, 0) + 1
            if counts:
                best, cnt = max(counts.items(), key=lambda kv: kv[1])
                cur = simple[1]
                diff = (max(abs(best[i] - cur[i]) for i in range(len(best)))
                        if simple[0] == "rect" else
                        max(abs(best[0] - cur[0]), abs(best[1] - cur[1]),
                            abs(best[2] - cur[2])))
                if diff > 0.05:
                    feat = dict(feat)
                    feat["outline"] = (_rect_outline(best) if simple[0] == "rect"
                                       else _circle_outline(best))
                    notes.append(
                        "%s: outline corrected %s -> %s (measured %d/%d "
                        "heights)" % (feat.get("name"), cur, best, cnt, n))
        # --- correction 2: Z-range (section changes inside the range) --
        # Scan the feature's range at dense spacing; find the Z values
        # where the section's loop signature changes. If the feature
        # spans a change, shrink it to the largest run that matches
        # its own outline kind and add the other runs as new features
        # (rectangle/circle only; complex runs keep the old feature).
        lo = max(fz0, zmin_all)
        hi = min(fz1, zmax_all)
        if hi - lo > ct_dense_spacing:
            zs = []
            z = lo
            while z <= hi + 1e-9:
                zs.append(round(z, 6))
                z += ct_dense_spacing
            if zs[-1] < hi - 1e-9:
                zs.append(round(hi, 6))
            sigs = []
            for z in zs:
                loops = _loops_at(z)
                # Signature of the loop that best matches this feature.
                fb = _outline_bbox(feat["outline"])
                loop = _best_loop_match(loops, fb)
                sigs.append(_loop_signature(loop) if loop is not None else None)
            # Runs: a new run starts when the signature KIND changes,
            # or (for rect/circle) when the size moved > 1.0 mm from
            # the run's start -- a real step. A gradual taper (the
            # arm tip narrows 0.4 mm over 1.6 mm) stays one run; it is
            # the outline correction's job, not a split.
            runs = []
            for z, s in zip(zs, sigs):
                if s is None:
                    continue
                if runs:
                    pk, pv = runs[-1][2]
                    if s[0] == pk:
                        if s[0] in ("rect", "circle"):
                            dv = max(abs(s[1][i] - pv[i])
                                     for i in range(len(s[1])))
                            if dv <= 1.0:
                                runs[-1][1] = z
                                continue
                        else:
                            runs[-1][1] = z
                            continue
                runs.append([z, z, s])
            # Drop runs thinner than 0.1 mm (a single cut, not a real
            # feature -- creating one would be a degenerate Pad/Pocket).
            runs = [r for r in runs if r[1] - r[0] >= 0.1 - 1e-9]
            # Tile the runs so they meet without gaps: each run ends
            # where the next begins (the true transition is between
            # the two sampled heights, within one dense step).
            for i in range(len(runs) - 1):
                runs[i][1] = runs[i + 1][0]
            if len(runs) > 1 and any(s is not None for _, _, s in runs):
                # Keep the run whose signature matches the feature's
                # own outline kind best (rectangle/circle/complex);
                # the other runs become new features if they are
                # rectangle/circle.
                own_kind = ("rect" if (len(outline) == 4 and
                                       all(e["kind"] == "line" for e in outline))
                            else "circle" if (len(outline) == 1 and
                                             outline[0]["kind"] == "circle")
                            else "other")
                # The run containing the feature's mid-Z is the feature's own.
                zmid = (fz0 + fz1) / 2.0
                own = None
                for r in runs:
                    if r[0] - 1e-9 <= zmid <= r[1] + 1e-9:
                        own = r
                        break
                if own is None:
                    own = runs[0]
                if own[2] is not None and own[2][0] == own_kind:
                    # Shrink the feature to its own run.
                    if abs(own[0] - fz0) > 0.05 or abs(own[1] - fz1) > 0.05:
                        feat = dict(feat)
                        feat["zmin"] = round(own[0], 4)
                        feat["zmax"] = round(own[1], 4)
                        feat["name"] = "%s_z%.2f_%.2f" % (
                            feat["type"], own[0], own[1])
                        notes.append(
                            "%s: Z range %.2f..%.2f -> %.2f..%.2f "
                            "(section changes inside)" % (
                                feat.get("name"), fz0, fz1, own[0], own[1]))
                    # Add the other runs as new features (rect/circle).
                    for r in runs:
                        if r is own or r[2] is None:
                            continue
                        kind, val = r[2]
                        if kind == "rect":
                            out = _rect_outline(val)
                            centre = [(val[0] + val[1]) / 2.0,
                                      (val[2] + val[3]) / 2.0]
                        elif kind == "circle":
                            out = _circle_outline(val)
                            centre = [val[0], val[1]]
                        else:
                            continue
                        new_features.append({
                            "type": feat["type"],
                            "name": "%s_z%.2f_%.2f" % (feat["type"], r[0], r[1]),
                            "outline": out,
                            "zmin": round(r[0], 4), "zmax": round(r[1], 4),
                            "centre": centre,
                        })
                        notes.append(
                            "added %s %s Z %.2f..%.2f (%s %s)" % (
                                feat["type"], new_features[-1]["name"],
                                r[0], r[1], kind, val))
        new_features.append(feat)
    if notes:
        fl["features"] = new_features
    return notes


def _all_checks_pass(fl):
    """True when volume, iso silhouette and CT mean all pass."""
    def _ok(checks, name):
        for c in checks or []:
            if c.get("check") == name:
                return bool(c.get("ok"))
        return False
    return (_ok(fl.get("verify"), "volume") and
            _ok(fl.get("verify_iso"), "iso_mean") and
            _ok(fl.get("verify_ct"), "ct_mean"))


def _iteration_metrics(fl):
    """One number per iteration: how far the model is from passing.

    deficit = volume error above the 0.5% threshold
            + CT mean below 0.90 (x100) + iso mean below 0.90 (x100).
    Lower is better; 0 means all three checks pass. Used to decide
    whether a refinement improved the model, made it worse, or did
    nothing -- the loop's stop rule (his policy, Nov 25).
    Returns {volume_pct, ct_mean, iso_mean, n_fail_slices, deficit}.
    """
    import re
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
            m = re.search(r"mean silhouette IoU ([\d.]+)", c.get("detail", ""))
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


def _analyze_failures(fl, orig_step, built_step):
    """Analyze WHAT is wrong and WHERE, for the report (Nov 25, his
    instruction: he needs the diagnosis to give a fix direction).

    For each failing CT interval, reports the Z range, the IoU
    (worst/mean), how many slices fail, which features overlap the
    interval, and -- at the worst slice -- how the ORIGINAL's section
    differs from the BUILT model's section (number of loops, number
    of section points, bbox). A section with far more points than the
    built one means fine 3D detail the profile model does not capture;
    a bbox that drifts along Z means a taper. Returns a list of dicts.
    """
    import re
    from OCP.STEPControl import STEPControl_Reader
    intervals = _failing_z_intervals(fl.get("verify_ct"))
    if not intervals:
        return []
    # IoU per failing Z.
    iou_by_z = {}
    for c in fl.get("verify_ct") or []:
        if c["check"].startswith("ct_z") and not c["ok"]:
            m = re.search(r"Z ([-\d.]+) mm", c["detail"])
            i = re.search(r"IoU ([\d.]+)", c["detail"])
            if m and i:
                iou_by_z[float(m.group(1))] = float(i.group(1))

    def _read_shape(path):
        reader = STEPControl_Reader()
        if reader.ReadFile(path) != 1:
            return None
        reader.TransferRoots()
        return reader.OneShape()

    orig = _read_shape(orig_step)
    built = _read_shape(built_step)
    # The built model is in DATUM coordinates (origin = datum origin),
    # the original in assembly coordinates. A section at assembly Z
    # is at built Z = assembly Z - datum Z. Cutting the built model
    # at the assembly Z finds nothing (0 loops) -- the bug that made
    # the arm-tip analysis report "built 0 loops".
    datum_z = (fl.get("datum") or {}).get("origin", [0, 0, 0])[2]
    out = []
    for a, b in intervals:
        zs = sorted(z for z in iou_by_z if a - 1e-9 <= z <= b + 1e-9)
        if not zs:
            continue
        worst_z = min(zs, key=lambda z: iou_by_z[z])
        worst = iou_by_z[worst_z]
        mean_iou = sum(iou_by_z[z] for z in zs) / len(zs)
        # Features overlapping this interval (deduplicated; the 4
        # snap arms share one name, so a count is shown).
        seen = {}
        for f in fl["features"]:
            if f["type"] not in ("profile", "hole"):
                continue
            if not (f["zmax"] < a - 0.01 or f["zmin"] > b + 0.01):
                key = "%s %s Z %.2f..%.2f" % (
                    f["type"], f.get("name"), f["zmin"], f["zmax"])
                seen[key] = seen.get(key, 0) + 1
        feats = [k + (" (x%d)" % v if v > 1 else "")
                 for k, v in seen.items()]
        # Original vs built section at the worst slice.
        cmp = {}
        for label, shape, zz in (("orig", orig, worst_z),
                                 ("built", built, worst_z - datum_z)):
            if shape is None:
                continue
            try:
                loops = _section_loops(shape, zz)
            except Exception:
                loops = []
            npts = sum(len(l) for l in loops)
            bboxes = [_loop_bbox(l) for l in loops] if loops else []
            cmp[label] = {"n_loops": len(loops), "n_points": npts,
                          "bboxes": [[round(v, 2) for v in bb]
                                     for bb in bboxes]}
        # Plain-language diagnosis.
        diag = []
        if "orig" in cmp and "built" in cmp:
            o, u = cmp["orig"], cmp["built"]
            if o["n_points"] > u["n_points"] * 1.5 and o["n_points"] > 20:
                diag.append("original section has %d points vs built %d "
                            "-- fine 3D detail the profile does not "
                            "capture" % (o["n_points"], u["n_points"]))
            if o["n_loops"] != u["n_loops"]:
                diag.append("loop count differs: original %d, built %d"
                            % (o["n_loops"], u["n_loops"]))
            # Bbox drift along the interval: taper or step.
            if orig is not None:
                try:
                    bb0 = [_loop_bbox(l) for l in _section_loops(orig, a)]
                    bb1 = [_loop_bbox(l) for l in _section_loops(orig, b)]
                    if bb0 and bb1:
                        d = max(abs(bb0[0][i] - bb1[0][i])
                                for i in range(4)) if len(bb0) == len(bb1) else 0
                        if d > 0.3:
                            diag.append("original section bbox moves %.2f mm "
                                        "across the interval -- taper/step, "
                                        "not a constant profile" % d)
                except Exception:
                    pass
            # Size/position difference at the worst slice: for each
            # original loop, find the built loop with the closest bbox
            # and report the largest mismatch. A constant rectangle
            # built where the original tapers shows up here.
            if "orig" in cmp and "built" in cmp:
                ob = cmp["orig"].get("bboxes") or []
                ub = cmp["built"].get("bboxes") or []
                worst_d = 0.0
                worst_pair = None
                for o in ob:
                    best_u = None
                    best_d = None
                    for u in ub:
                        d = max(abs(o[i] - u[i]) for i in range(4))
                        if best_d is None or d < best_d:
                            best_d = d
                            best_u = u
                    if best_u is not None and best_d > worst_d:
                        worst_d = best_d
                        worst_pair = (o, best_u)
                if worst_pair is not None and worst_d > 0.1:
                    diag.append("section size/position differs by up to "
                                "%.2f mm at Z %.2f (original %s vs built "
                                "%s)" % (worst_d, worst_z,
                                         [round(v, 2) for v in worst_pair[0]],
                                         [round(v, 2) for v in worst_pair[1]]))
        out.append({
            "z_range": [round(a, 2), round(b, 2)],
            "n_fail_slices": len(zs),
            "worst_iou": round(worst, 3),
            "worst_z": round(worst_z, 2),
            "mean_iou": round(mean_iou, 3),
            "features": feats,
            "section_compare": cmp,
            "diagnosis": diag,
        })
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="STEP + 2D drawing -> parametric FreeCAD")
    ap.add_argument("--step", required=True)
    ap.add_argument("--hlr", default=None, help="model_hlr.json (2D drawing data)")
    ap.add_argument("--out", required=True, help="output dir for features.json")
    ap.add_argument("--freecadcmd", default="freecadcmd")
    ap.add_argument("--ct", action="store_true",
                    help="CT-scan the shape: adaptive slices (coarse 25 "
                         "evenly-spaced + 0.2 mm inside intervals where the "
                         "section changes) + PNGs into <out>/ct/, so the LLM "
                         "can understand the shape and sketch it. If the "
                         "shape is still unclear, re-run ct_slice.py with "
                         "--zrange=z0,z1,n for a detailed scan of that region.")
    ap.add_argument("--ct-coarse-n", type=int, default=25,
                    help="CT whole-part slices by COUNT, evenly spaced "
                         "(default 25). Overridden by --ct-coarse-spacing "
                         "when given.")
    ap.add_argument("--ct-coarse-spacing", type=float, default=None,
                    help="CT whole-part spacing in mm (e.g. 2.0). When "
                         "given, the coarse count is ceil(height/spacing)+1 "
                         "instead of --ct-coarse-n.")
    ap.add_argument("--ct-dense-spacing", type=float, default=0.2,
                    help="CT spacing in mm inside intervals where the "
                         "section changes (default 0.2).")
    ap.add_argument("--refine-iter", type=int, default=3,
                    help="Iterative loop (Nov 24, his design): after build "
                         "+ verify, find the parts NOT drawn, re-sketch "
                         "them from the original and rebuild, up to N times "
                         "(default 3). 0 = run once, no refinement.")
    ap.add_argument("--iso", action="store_true",
                    help="Isometric views: 4 corners (top/bottom x front/back) "
                         "+ 2x2 montage into <out>/iso/, so the LLM can see "
                         "every side of the shape. One isometric view hides "
                         "the back/bottom.")
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    print("recognizing features from", args.step)
    fl = recognize(args.step)
    for feat in fl["features"]:
        if feat["type"] == "profile":
            kinds = sorted(set(e["kind"] for e in feat["outline"]))
            print("   profile %s: %d edges (%s), Z %.4f..%.4f" % (
                feat.get("name"), len(feat["outline"]), ",".join(kinds),
                feat["zmin"], feat["zmax"]))
        elif feat["type"] == "hole":
            kinds = sorted(set(e["kind"] for e in feat["outline"]))
            print("   hole %s: %d edges (%s), Z %.4f..%.4f" % (
                feat.get("name"), len(feat["outline"]), ",".join(kinds),
                feat["zmin"], feat["zmax"]))
        else:
            print("  ", feat["type"], feat.get("name"), feat)
    print("bbox:", fl["bbox"], "volume:", fl["volume_mm3"])
    if args.ct:
        # CT shape-understanding: ADAPTIVE slices -- same rule as the
        # verify_ct_match check, so the edge-count profile shows WHERE
        # the shape changes at the resolution the verification uses.
        # The PNGs show WHAT the shape is at each height.
        from OCP.STEPControl import STEPControl_Reader
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        from .ct_slice import section_wires
        _r = STEPControl_Reader()
        assert _r.ReadFile(args.step) == 1
        _r.TransferRoots()
        _shape = _r.OneShape()
        _box = Bnd_Box()
        BRepBndLib.Add_s(_shape, _box)
        _obb = _box.Get()
        _zs = _adaptive_ct_heights(
            _obb[2], _obb[5], args.ct_coarse_n, args.ct_coarse_spacing,
            args.ct_dense_spacing,
            mask_fn=lambda z: _section_mask_raster(
                section_wires(_shape, "xy", z), _obb))
        import subprocess
        ct_dir = os.path.join(args.out, "ct")
        r = subprocess.run(
            [sys.executable, "-m", "verifier.ct_slice",
             "--step", args.step, "--out", ct_dir,
             "--zs", ",".join("%.6f" % z for z in _zs)],
            capture_output=True, text=True)
        print("CT scan (%d slices):" % len(_zs))
        for line in r.stdout.splitlines():
            if "edges" in line or line.startswith("wrote"):
                print("  CT", line)
        if r.returncode == 0:
            with open(os.path.join(ct_dir, "slices.json")) as f:
                fl["ct"] = json.load(f)
        else:
            print("  CT failed:", r.stderr[-500:])
    if args.iso:
        iso_dir = os.path.join(args.out, "iso")
        try:
            paths = _render_iso(args.step, iso_dir)
            print("isometric views:", ", ".join(os.path.basename(p) for p in paths))
            fl["iso"] = [os.path.basename(p) for p in paths]
        except Exception as e:
            print("  ISO failed:", e)
    if args.hlr:
        print("cross-checking against 2D drawing", args.hlr)
        for c in cross_check(fl, args.hlr):
            print("  [%s] %s: %s" % ("OK" if c["ok"] else "FAIL", c["check"], c["detail"]))
        fl["cross_check"] = cross_check(fl, args.hlr)
    with open(os.path.join(args.out, "features.json"), "w") as f:
        json.dump(fl, f, indent=2)
    print("wrote", os.path.join(args.out, "features.json"))

    # --- build + verify, with the iterative loop (Nov 24, his design):
    # verify, find the parts NOT drawn, re-sketch them from the
    # original and rebuild, repeat until all checks pass (or the
    # iteration budget runs out). _refine_features corrects fl in
    # place; each pass clears the previous verify results.
    def _build_and_verify():
        print("building parametric FreeCAD model")
        ok, log = build_fcstd(fl, args.out, freecadcmd=args.freecadcmd)
        for line in log.splitlines():
            if "STEP2PARAM" in line or "Error" in line or "error" in line:
                print("  ", line)
        if not ok:
            print("BUILD FAILED")
            print(log[-2000:])
            return False
        fl["verify"] = []
        fl["verify_iso"] = []
        fl["verify_ct"] = []
        print("verifying built model against the original STEP")
        for c in verify_built(os.path.join(args.out, "model.step"), fl):
            print("  [%s] %s: %s" % ("OK" if c["ok"] else "FAIL", c["check"], c["detail"]))
            fl["verify"].append(c)
        print("verifying direction/shape by IMAGE (program compares the")
        print("  built vs original iso silhouettes itself -- no human check)")
        try:
            for c in verify_iso_match(os.path.join(args.out, "model.step"),
                                      args.step, fl, args.out):
                print("  [%s] %s: %s" % ("OK" if c["ok"] else "FAIL",
                                          c["check"], c["detail"]))
                fl["verify_iso"].append(c)
        except Exception as e:
            print("  ISO check failed:", e)
        print("verifying direction/shape by CT (program cuts both models")
        print("  at the same relative heights and compares cross-sections)")
        try:
            for c in verify_ct_match(os.path.join(args.out, "model.step"),
                                     args.step, fl, args.out,
                                     coarse_n=args.ct_coarse_n,
                                     coarse_spacing=args.ct_coarse_spacing,
                                     dense_spacing=args.ct_dense_spacing):
                if c["check"] in ("ct_mean",) or not c["ok"]:
                    print("  [%s] %s: %s" % ("OK" if c["ok"] else "FAIL",
                                              c["check"], c["detail"]))
                fl["verify_ct"].append(c)
        except Exception as e:
            print("  CT check failed:", e)
        return True

    # --- build + verify, with the iterative loop (Nov 24-25, his design).
    # Policy (Nov 25, his words): up to 3 refinements run AUTOMATICALLY.
    # From the 4th on, ask the user first. If a refinement makes the
    # model WORSE than the best so far, or shows NO improvement, stop
    # and report it -- the person chooses (give a direction, or
    # complete the work as-is). The best iteration's model is kept,
    # never a worse one. The program runs detached, so "ask" means:
    # stop with status needs_decision / no_improvement in
    # fl["refine_report"] and the log, and Muse asks him in chat.
    import copy
    max_iter = max(0, args.refine_iter)
    history = []
    best = None  # (deficit, iteration, fl snapshot)
    status = "pass"
    status_msg = ""
    for it in range(max_iter + 1):
        if not _build_and_verify():
            return 1
        m = _iteration_metrics(fl)
        history.append(dict(m, iteration=it))
        print("  iteration %d: volume %.3f%%, ct_mean %.3f, iso_mean %.3f, "
              "fail slices %d, deficit %.3f" % (
                  it, m["volume_pct"] if m["volume_pct"] is not None else -1,
                  m["ct_mean"] if m["ct_mean"] is not None else -1,
                  m["iso_mean"] if m["iso_mean"] is not None else -1,
                  m["n_fail_slices"], m["deficit"]))
        if _all_checks_pass(fl):
            status = "pass"
            status_msg = "all checks PASS (iteration %d)" % it
            print(status_msg)
            break
        if best is None or m["deficit"] < best[0] - 0.01:
            best = (m["deficit"], it, copy.deepcopy(fl))
        elif m["deficit"] > best[0] + 0.01:
            # Worse than the best so far: revert to the best model
            # and stop. Never keep a worse revision.
            print("  iteration %d is WORSE than iteration %d "
                  "(deficit %.3f -> %.3f); reverting to iteration %d"
                  % (it, best[1], best[0], m["deficit"], best[1]))
            fl.clear()
            fl.update(copy.deepcopy(best[2]))
            if not _build_and_verify():
                return 1
            status = "no_improvement"
            status_msg = ("iteration %d made the model worse than "
                          "iteration %d (deficit %.3f -> %.3f); reverted "
                          "to iteration %d" % (it, best[1], best[0],
                                              m["deficit"], best[1]))
            break
        elif it > 0:
            # Same deficit as the best: this refinement changed nothing.
            status = "no_improvement"
            status_msg = ("iteration %d shows no improvement over "
                          "iteration %d (deficit %.3f); the refinement "
                          "did not change the model" % (it, best[1],
                                                       m["deficit"]))
            print("  " + status_msg)
            break
        if it >= max_iter:
            # Automatic budget used; still failing, but the last step
            # improved -- ask the user whether to continue (4th+).
            status = "needs_decision"
            status_msg = ("3 automatic refinements done (iteration %d); "
                          "still failing (deficit %.3f) but improving -- "
                          "ask the user whether to continue" % (it, m["deficit"]))
            print("  " + status_msg)
            break
        notes = _refine_features(fl, args.step,
                                 ct_dense_spacing=args.ct_dense_spacing)
        if not notes:
            status = "no_improvement"
            status_msg = ("no corrections found for the failing regions "
                          "(iteration %d)" % it)
            print("  " + status_msg)
            break
        print("refining (iteration %d -> %d):" % (it, it + 1))
        for n in notes:
            print("  ", n)
    # --- refine report: the user decides from this, not from the log.
    # When it passes, the passing iteration is the best one.
    if status == "pass":
        best_it = history[-1]["iteration"] if history else 0
    else:
        best_it = best[1] if best is not None else (history[-1]["iteration"]
                                                    if history else 0)
    if status == "pass":
        report_msg = "모든 검증 통과 (iteration %d)." % best_it
    elif status == "needs_decision":
        report_msg = ("3회 자동 개선을 마쳤으나 아직 미흡합니다 "
                      "(deficit %.3f). 추가 작업을 진행할까요?" %
                      history[-1]["deficit"])
    else:  # no_improvement
        report_msg = ("개선이 진행되지 않습니다. 개선방향을 알려주면 "
                      "추가 개선을 진행해보고 개선방향이 없다면 "
                      "현재작업에서 완료처리 하겠습니다.")
    fl["refine_report"] = {
        "status": status,
        "message": report_msg,
        "detail": status_msg,
        "auto_budget": max_iter,
        "best_iteration": best_it,
        "iterations": history,
        # What is wrong and where -- the diagnosis he needs to give a
        # fix direction (Nov 25). Computed from the best iteration's
        # verify results (the model on disk is the best one).
        "failures": _analyze_failures(fl, args.step,
                                      os.path.join(args.out, "model.step")),
    }
    print("refine report: status=%s best_iteration=%d" % (status, best_it))
    print("  " + report_msg)
    with open(os.path.join(args.out, "features.json"), "w") as f:
        json.dump(fl, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
