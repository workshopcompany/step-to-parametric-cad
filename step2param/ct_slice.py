"""CT-style cross-section slicer.

Like a CT scan: cut the STEP solid with horizontal planes at many Z
heights and report/render each 2D profile, so an LLM can understand
the shape without guessing from face counts.

Usage:
  python ct_slice.py --step <file.step> --out <dir> [--n N] [--zs 0,5,10,...]
                     [--zrange z0,z1,n]

  Default: 25 slices across the whole height (a few seconds; cheap).
  If the shape is still unclear, run a DETAILED scan of just the region
  that needs it: --zrange z0,z1,n slices densely inside [z0, z1].

Output per slice: a PNG (top view of the section wires) + a line in
slices.json describing each closed loop (bbox, area, n_vertices).
"""
import argparse, json, math, os, sys

from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_SOLID, TopAbs_FACE, TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.gp import gp_Pln, gp_Pnt, gp_Dir
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRep import BRep_Tool
from OCP.TopTools import TopTools_ListOfShape
from OCP.TopoDS import TopoDS
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GeomAbs import GeomAbs_Line, GeomAbs_Circle
from OCP.GProp import GProp_GProps
from OCP.BRepGProp import BRepGProp


def load_solids(path):
    r = STEPControl_Reader()
    assert r.ReadFile(path) == 1, f"cannot read {path}"
    r.TransferRoots()
    shape = r.OneShape()
    solids = []
    ex = TopExp_Explorer(shape, TopAbs_SOLID)
    while ex.More():
        solids.append(ex.Current())
        ex.Next()
    return shape, solids


def section_wires(shape, plane, offset):
    """Intersect shape with a plane.

    plane: 'xy' (top view, cut at Z=offset, plot X vs Y),
           'xz' (front view, cut at Y=offset, plot X vs Z),
           'yz' (side view, cut at X=offset, plot Y vs Z).
    Returns list of polylines [(u,v),...] in the plane's 2D coordinates.
    """
    if plane == "xy":
        pln = gp_Pln(gp_Pnt(0, 0, offset), gp_Dir(0, 0, 1))
        uv = lambda p: (p.X(), p.Y())
    elif plane == "xz":
        pln = gp_Pln(gp_Pnt(0, offset, 0), gp_Dir(0, 1, 0))
        uv = lambda p: (p.X(), p.Z())
    elif plane == "yz":
        pln = gp_Pln(gp_Pnt(offset, 0, 0), gp_Dir(1, 0, 0))
        uv = lambda p: (p.Y(), p.Z())
    else:
        raise ValueError(f"unknown plane {plane}")
    sec = BRepAlgoAPI_Section(shape, pln)
    sec.Build()
    assert sec.IsDone(), "section failed"
    ex = TopExp_Explorer(sec.Shape(), TopAbs_EDGE)
    edges = []
    while ex.More():
        edge = TopoDS.Edge_s(ex.Current())
        ad = BRepAdaptor_Curve(edge)
        typ = ad.GetType()
        pts = []
        if typ == GeomAbs_Line:
            p0 = ad.Value(ad.FirstParameter())
            p1 = ad.Value(ad.LastParameter())
            pts = [uv(p0), uv(p1)]
        elif typ == GeomAbs_Circle:
            # discretise arc
            n = max(8, int(abs(ad.LastParameter() - ad.FirstParameter()) / 0.1))
            for k in range(n + 1):
                t = ad.FirstParameter() + (ad.LastParameter() - ad.FirstParameter()) * k / n
                pts.append(uv(ad.Value(t)))
        else:
            n = 32
            for k in range(n + 1):
                t = ad.FirstParameter() + (ad.LastParameter() - ad.FirstParameter()) * k / n
                pts.append(uv(ad.Value(t)))
        edges.append(pts)
        ex.Next()
    return edges


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=25, help="number of slices (default 25)")
    ap.add_argument("--plane", default="xy", choices=["xy", "xz", "yz"],
                    help="cut plane: xy=top (cut at Z), xz=front (cut at Y), "
                         "yz=side (cut at X). Default xy.")
    ap.add_argument("--zs", default="", help="explicit offsets list, comma separated")
    ap.add_argument("--zrange", default="",
                    help="detailed scan: a0,a1,n -> n slices densely inside [a0,a1] "
                         "along the cut axis")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    shape, solids = load_solids(args.step)
    print(f"solids: {len(solids)}")
    # bbox
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    print(f"bbox X {xmin:.2f}..{xmax:.2f} Y {ymin:.2f}..{ymax:.2f} Z {zmin:.2f}..{zmax:.2f}")

    # cut axis + plot ranges per plane
    if args.plane == "xy":
        ax_lo, ax_hi = zmin, zmax
        u_lo, u_hi, v_lo, v_hi = xmin, xmax, ymin, ymax
        ax_name, u_name, v_name = "Z", "X", "Y"
    elif args.plane == "xz":
        ax_lo, ax_hi = ymin, ymax
        u_lo, u_hi, v_lo, v_hi = xmin, xmax, zmin, zmax
        ax_name, u_name, v_name = "Y", "X", "Z"
    else:  # yz
        ax_lo, ax_hi = xmin, xmax
        u_lo, u_hi, v_lo, v_hi = ymin, ymax, zmin, zmax
        ax_name, u_name, v_name = "X", "Y", "Z"

    if args.zrange:
        # detailed scan of one region: a0,a1,n
        a0s, a1s, ns = args.zrange.split(",")
        a0, a1, n = float(a0s), float(a1s), max(2, int(ns))
        offs = [a0 + (a1 - a0) * i / (n - 1) for i in range(n)]
    elif args.zs:
        offs = [float(v) for v in args.zs.split(",")]
    else:
        n = max(2, args.n)
        offs = [ax_lo + (ax_hi - ax_lo) * i / (n - 1) for i in range(n)]
        # nudge off exact faces to avoid degenerate sections
        offs = [o + 0.01 if i == 0 else (o - 0.01 if i == len(offs) - 1 else o)
                for i, o in enumerate(offs)]

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    summary = {"bbox": [xmin, ymin, zmin, xmax, ymax, zmax],
               "plane": args.plane, "slices": []}
    for o in offs:
        edges = section_wires(shape, args.plane, o)
        # render
        fig, ax = plt.subplots(figsize=(5, 5))
        ax.set_aspect("equal")
        ax.set_xlim(u_lo - 2, u_hi + 2)
        ax.set_ylim(v_lo - 2, v_hi + 2)
        ax.set_title(f"{args.plane} {ax_name} = {o:.2f}  ({len(edges)} edges)")
        for pts in edges:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            ax.plot(xs, ys, "b-", linewidth=1.2)
        ax.grid(True, alpha=0.3)
        png = os.path.join(args.out, f"slice_{args.plane}_{o:07.2f}.png")
        fig.savefig(png, dpi=100)
        plt.close(fig)
        # text summary
        allpts = [p for e in edges for p in e]
        if allpts:
            sx = [p[0] for p in allpts]; sy = [p[1] for p in allpts]
            info = {"offset": round(o, 3), "n_edges": len(edges),
                    "uv_bbox": [round(min(sx), 2), round(min(sy), 2),
                                round(max(sx), 2), round(max(sy), 2)],
                    "png": os.path.basename(png)}
        else:
            info = {"offset": round(o, 3), "n_edges": 0, "uv_bbox": None,
                    "png": os.path.basename(png)}
        summary["slices"].append(info)
        print(f"{args.plane} {ax_name} {o:7.2f}: {len(edges):3d} edges  bbox={info['uv_bbox']}")
    with open(os.path.join(args.out, "slices.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print("wrote", args.out)


if __name__ == "__main__":
    main()
