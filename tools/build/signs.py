"""Baku signage.

1. Road signs (Azerbaijani, Vienna convention): one small model per sign type sharing the TXD
   bk_rsign.  They can be knocked over like the vanilla street signs: object.dat gets lines with
   the exact parameters of vanilla 'noparkingsign1' (identical lines share one CObjectData slot),
   so they become CDummyObjects and count against the same budgets as the other dynamic props.
   Model axes follow the vanilla convention measured for traffic lights: the plate faces -y, i.e.
   a sign with heading h controls traffic driving along its +y axis.  Pivot at mid height (as
   vanilla bussign1), so instance z = ground + H/2.

2. Shop / market boards, street-name plates and zebra crossings: merged into one static model per
   250 x 250 ground tile (bk_sgXXYY, texture atlas bk_sgtXXYY), see tiles().
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pyarrow.parquet as pq
import shapely

from tools.build import ground
from tools.build.mesh import MeshBuilder
from tools.geo import profiles
from tools.geo.proj import lonlat_to_game, WORLD
from tools.geo.roadmesh import polyline_normals
from tools.rw import col as COL, dff as DFF, txd as TXD
from tools.rw.col import SURF
from tools.tex import signs_tex as ST

RSIGN_TXD = "bk_rsign"
RSIGN_BASE = 15065            # ids.PROP_BASE


@dataclass
class RoadSign:
    name: str
    id: int
    face: str
    shape: str                # octagon, tri_down, circle, square, diamond, rect
    size: float               # plate width (units = metres)
    height: float             # total height (ground -> top of plate)
    aspect: float = 1.0       # plate height / width (rect)


_SPECS = [
    ("stop", "stop", "octagon", 0.75, 2.75),
    ("giveway", "giveway", "tri_down", 0.9, 2.8),
    ("ped", "ped", "square", 0.7, 2.75),
    ("noentry", "noentry", "circle", 0.7, 2.7),
    ("speed40", "speed40", "circle", 0.7, 2.7),
    ("speed60", "speed60", "circle", 0.7, 2.7),
    ("speed80", "speed80", "circle", 0.7, 2.7),
    ("priority", "priority", "diamond", 0.75, 2.8),
    ("roundabout", "roundabout", "circle", 0.7, 2.7),
    ("nostop", "nostop", "circle", 0.7, 2.7),
    ("bus", "bus", "square", 0.6, 2.9),
    ("oneway", "oneway", "square", 0.6, 2.7),
    ("metro", "metro", "circle", 0.9, 3.8),
    ("parking", "parking", "square", 0.6, 2.7),
]
ROAD_SIGNS = {k: RoadSign(f"bk_rs_{k}", RSIGN_BASE + i, face, shape, size, h)
              for i, (k, face, shape, size, h) in enumerate(_SPECS)}
SIGN_IDS = {s.id: s for s in ROAD_SIGNS.values()}

# object.dat: identical to vanilla 'noparkingsign1' (damage effect 0, lamp-post collision response):
# the sign is knocked over when hit.  Effect 200 (breakable) would need a Breakable plugin in the
# DFF; without it BreakObject_c::Init fails and the object just disappears (Object.cpp).
# Identical parameter lines share one CObjectData slot.
OBJECT_DAT_PARAMS = ("30.0,\t\t50.0   \t\t0.99,\t\t0.05,\t\t50.0,\t\t100.0,  1.0,\t0,\t1,\t1,\t0,\t0,\t0.0, 0.0, 0.0,"
                     "\t\tnone")


def object_dat_lines():
    return [f"{s.name},\t\t{OBJECT_DAT_PARAMS}" for s in ROAD_SIGNS.values()]


# ------------------------------------------------------------------------------------ geometry
def _outline(shape, w, aspect=1.0):
    """Plate outline in (x, z) relative to the plate centre + matching UV (texture square)."""
    if shape == "octagon":
        r = w / 2 / math.cos(math.pi / 8)
        pts = [(r * math.cos(math.pi / 8 + k * math.pi / 4), r * math.sin(math.pi / 8 + k * math.pi / 4)) for k in range(8)]
        ext = w / 2
    elif shape == "circle":
        r = w / 2
        pts = [(r * math.cos(2 * math.pi * k / 24), r * math.sin(2 * math.pi * k / 24)) for k in range(24)]
        ext = r
    elif shape == "tri_down":
        h = w * math.sqrt(3) / 2
        pts = [(-w / 2, h / 2), (0.0, -h / 2), (w / 2, h / 2)]
        uv = [(0.0, 0.0), (0.5, 1.0), (1.0, 0.0)]
        return np.array(pts), np.array(uv)
    elif shape == "diamond":
        r = w / 2
        pts = [(0, r), (r, 0), (0, -r), (-r, 0)]
        ext = r
    else:  # square / rect
        hw, hh = w / 2, w * aspect / 2
        pts = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
        P = np.array(pts)
        uv = np.c_[(P[:, 0] + hw) / (2 * hw), (hh - P[:, 1]) / (2 * hh)]
        return P, uv
    P = np.array(pts)
    uv = np.c_[(P[:, 0] + ext) / (2 * ext), (ext - P[:, 1]) / (2 * ext)]
    return P, uv


def _fan(n):
    return np.array([[0, k, k + 1] for k in range(1, n - 1)])


def _orient(V, T, normal):
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    nrm = np.cross(b - a, c - a)
    flip = (nrm @ np.asarray(normal, float)) < 0
    T = T.copy()
    T[flip] = T[flip][:, [0, 2, 1]]
    return T


def road_sign_mesh(s: RoadSign):
    """MeshBuilder + collision boxes for one sign type (pivot at mid height)."""
    mb = MeshBuilder()
    H = s.height
    z0 = -H / 2
    P, UV = _outline(s.shape, s.size, s.aspect)
    plate_h = P[:, 1].max() - P[:, 1].min()
    zc = z0 + H - plate_h / 2 - 0.02
    face = np.c_[P[:, 0], np.full(len(P), -0.055), P[:, 1] + zc]
    back = np.c_[P[:, 0], np.full(len(P), -0.045), P[:, 1] + zc]
    T = _fan(len(P))
    mb.add("bk_rs_" + s.face, face, UV, _orient(face, T, (0, -1, 0)), shade=1.22, night=1.0)
    mb.add("bk_rs_back", back, np.c_[P[:, 0] * 2, -P[:, 1] * 2], _orient(back, T, (0, 1, 0)), shade=1.0, night=0.8)
    # pole: square tube 0.07, from the ground to just under the plate top
    r = 0.035
    top = zc + plate_h / 2 - 0.05
    corners = [(-r, -r), (r, -r), (r, r), (-r, r)]
    for k in range(4):
        (x0, y0), (x1, y1) = corners[k], corners[(k + 1) % 4]
        V = np.array([[x0, y0, z0], [x1, y1, z0], [x1, y1, top], [x0, y0, top]])
        UVp = np.array([[0, H], [1, H], [1, 0], [0, 0]], float)
        Tq = np.array([[0, 1, 2], [0, 2, 3]])
        out = np.array([(x0 + x1) / 2, (y0 + y1) / 2, 0.0])
        mb.add("bk_rs_pole", V, UVp, _orient(V, Tq, out), shade=1.0, night=0.8)
    boxes = [((-0.05, -0.05, z0), (0.05, 0.05, top), SURF["LAMP_POST"]),
             ((P[:, 0].min(), -0.07, zc - plate_h / 2), (P[:, 0].max(), -0.03, zc + plate_h / 2), SURF["LAMP_POST"])]
    return mb, boxes


def build_road_sign_models():
    """-> list of (name, id, dff bytes, col bytes), txd bytes."""
    out = []
    for s in ROAD_SIGNS.values():
        mb, boxes = road_sign_mesh(s)
        geo, _ = mb.build((0.0, 0.0, 0.0))
        data = DFF.write_dff(s.name, geo)
        cm = COL.ColModel(s.name, s.id, boxes=boxes)
        out.append((s.name, s.id, data, COL.write_col3(cm)))
    tex = [TXD.texture_native("bk_rs_" + k, f(), encoding="DXT1") for k, f in ST.FACES.items()]
    tex.append(TXD.texture_native("bk_rs_back", ST.back_metal(), encoding="DXT1"))
    tex.append(TXD.texture_native("bk_rs_pole", ST.pole_metal(), encoding="DXT1"))
    return out, TXD.write_txd(tex)


# ------------------------------------------------------------------------------------ placement
def _sign(ctx, key, P, d, prio, rank=0):
    """Prop record for a road sign at P whose plate faces traffic travelling along d."""
    s = ROAD_SIGNS[key]
    z = ctx.ground_z(float(P[0]), float(P[1])) + s.height / 2
    h = math.atan2(-d[0], d[1])
    return {"id": s.id, "name": s.name, "x": float(P[0]), "y": float(P[1]), "z": float(z),
            "heading": h % (2 * math.pi), "kind": "sign", "dyn": True, "prio": prio, "rank": rank,
            "r": float(ctx.rng.random())}


def _free_spot(ctx, P, min_gap=1.6):
    xy = np.asarray(P, float)[None]
    return (not ctx.on_road(xy)[0] and not ctx.in_building(xy, 0.3)[0] and not ctx.in_water(xy)[0]
            and not ctx.near_placed(float(P[0]), float(P[1]), min_gap))


def _place(ctx, out, key, P, d, prio, rank=0, alts=()):
    for Q in (P, *alts):
        if _free_spot(ctx, Q):
            out.append(_sign(ctx, key, Q, d, prio, rank))
            ctx.mark(float(Q[0]), float(Q[1]), "sign")
            return True
    return False


MAJOR = {"motorway", "trunk", "primary", "secondary", "tertiary"}
MINOR = {"residential", "unclassified", "living_street", "service"}


def road_sign_candidates(ctx, lights):
    """Dynamic sign props: crossings, give way / stop, no entry, roundabouts, speed limits, metro."""
    W = ctx.W
    g = W.g
    lay = W.lay
    out = []
    # signalised junction nodes (no give-way/crossing signs there)
    sig_xy = np.array([[L["jx"], L["jy"]] for L in lights]) if lights else np.zeros((0, 2))
    from scipy.spatial import cKDTree
    sig_tree = cKDTree(sig_xy) if len(sig_xy) else None

    def near_signal(p, r):
        return sig_tree is not None and sig_tree.query_ball_point(p, r)

    def arm_frame(e, end):
        """(point at the ribbon end, travel dir towards the junction, right unit, half width right)."""
        r = lay.ribbons.get(e.id)
        if r is None:
            return None
        n = polyline_normals(r.center)
        if end == "v":
            c, t = r.center[-1], r.center[-1] - r.center[-2]
            t = t / max(np.hypot(*t), 1e-9)
            return c, t, -n[-1] / max(np.hypot(*n[-1]), 1e-9), r.right
        c, t = r.center[0], r.center[0] - r.center[1]
        t = t / max(np.hypot(*t), 1e-9)
        return c, t, n[0] / max(np.hypot(*n[0]), 1e-9), r.left

    # 1. junction control signs
    for j in lay.junctions.values():
        jc = np.array(j.polygon.centroid.coords[0])
        if near_signal(jc, 25.0):
            continue
        arms = [(lay.ribbons[eid].edge if eid in lay.ribbons else g.edges.get(eid), end) for eid, end in j.arms]
        arms = [(e, end) for e, end in arms if e is not None]
        classes = {e.cls for e, _ in arms}
        has_major = bool(classes & {"trunk", "primary", "secondary"})
        ring = any(e.attrs.get("ring") is not None for e, _ in arms) or any(
            g.edges[i].attrs.get("ring") is not None for i in j.internal if i in g.edges)
        for e, end in arms:
            if e.oneway and end == "u":
                # traffic leaves the junction into a one-way street; at its far end it is "no entry"
                continue
            fr = arm_frame(e, end)
            if fr is None:
                continue
            c, t, right, half = fr
            P = c + right * (half + 0.7) - t * 1.2
            if ring and e.attrs.get("ring") is None:
                _place(ctx, out, "roundabout", P, t, 3, alts=(P + right * 0.8,))
            elif has_major and e.cls in MINOR | {"tertiary"} and e.cls not in {"trunk", "primary", "secondary"}:
                key = "stop" if (e.cls in MINOR and ({"trunk", "primary"} & classes) and ctx.rng.random() < 0.35) else "giveway"
                _place(ctx, out, key, P, t, 3, alts=(P + right * 0.8,))
        # no entry at the exit end of one-way streets
        for e, end in arms:
            if not e.oneway or end != "v":
                continue
            fr = arm_frame(e, end)
            if fr is None:
                continue
            c, t, right, half = fr
            f = -t                                   # forbidden direction (into the street)
            rf = np.array([f[1], -f[0]])
            P = c + rf * (half + 0.7) + f * 1.0
            _place(ctx, out, "noentry", P, f, 3, alts=(P + rf * 0.8,))
    # 2. pedestrian crossings (OSM crossing nodes on major roads, away from signals)
    rs = [r for r in lay.ribbons.values() if r.edge.cls in MAJOR and len(r.center) >= 2]
    lines = [shapely.LineString(r.center) for r in rs]
    tree = shapely.STRtree(lines)
    pts = []
    for row in pq.read_table("data/raw/infrastructure.parquet", columns=["class", "geometry"]).to_pylist():
        if row["class"] == "crossing":
            p = shapely.from_wkb(row["geometry"])
            if p.geom_type == "Point":
                pts.append(lonlat_to_game(p.x, p.y))
    seen = []
    crossings = []
    for x, y in pts:
        if abs(x) > WORLD - 10 or abs(y) > WORLD - 10:
            continue
        p = shapely.Point(x, y)
        i = tree.nearest(p)
        if i is None or lines[int(i)].distance(p) > 3.0:
            continue
        r = rs[int(i)]
        s = lines[int(i)].project(p)
        L = lines[int(i)].length
        if s < 4 or s > L - 4:
            continue
        if any((x - a) ** 2 + (y - b) ** 2 < 15 ** 2 for a, b in seen):
            continue
        seen.append((x, y))
        e = r.edge
        s_full = profiles.arclen(e.pts)
        lf = abs(float(np.interp(r.s0 + s, s_full, e.lift)))
        if lf > 0.3:
            continue
        crossings.append((r, s))
        if near_signal((x, y), 22.0):
            continue
        (c,), (t,) = _sample(r.center, [s])
        n = np.array([-t[1], t[0]])
        dirs = [(t, -n, r.right)] + ([] if e.oneway else [(-t, n, r.left)])
        for d, right, half in dirs:
            P = c + right * (half + 0.7) - d * 1.8
            _place(ctx, out, "ped", P, d, 4, alts=(P - d * 1.2,))
    # 3. speed limits along major roads (after junctions, once per long ribbon and direction)
    for r in lay.ribbons.values():
        e = r.edge
        if e.cls not in ("motorway", "trunk", "primary", "secondary"):
            continue
        L = float(profiles.arclen(r.center)[-1])
        if L < 160:
            continue
        val = "speed80" if e.cls in ("motorway", "trunk") else "speed60"
        s_full = profiles.arclen(e.pts)
        for fwd in ((True,) if e.oneway else (True, False)):
            s = 30.0 if fwd else L - 30.0
            if abs(float(np.interp(r.s0 + s, s_full, e.lift))) > 0.3:
                continue
            (c,), (t,) = _sample(r.center, [s])
            n = np.array([-t[1], t[0]])
            d, right, half = (t, -n, r.right) if fwd else (-t, n, r.left)
            _place(ctx, out, val, c + right * (half + 0.7), d, 5)
    # 4. metro stations
    for row in pq.read_table("data/raw/infrastructure.parquet", columns=["class", "geometry"]).to_pylist():
        if row["class"] != "subway_station":
            continue
        p = shapely.from_wkb(row["geometry"])
        if p.geom_type != "Point":
            continue
        x, y = lonlat_to_game(p.x, p.y)
        if abs(x) > WORLD - 10 or abs(y) > WORLD - 10:
            continue
        best = None
        for a in np.linspace(0, 2 * math.pi, 16, endpoint=False):
            for rr in (0.0, 4.0, 8.0, 12.0, 18.0, 26.0):
                Q = np.array([x + rr * math.cos(a), y + rr * math.sin(a)])
                if _free_spot(ctx, Q, 3.0):
                    best = Q
                    break
            if best is not None:
                break
        if best is not None:
            out.append(_sign(ctx, "metro", best, np.array([0.0, 1.0]), 1))
            ctx.mark(float(best[0]), float(best[1]), "sign")
    ctx.crossings = crossings
    return out


def bus_stop_signs(ctx, bus_props):
    out = []
    for b in bus_props:
        if b["kind"] != "bus":
            continue
        h = b["heading"]
        # shelter: road along local -x, shelter long axis along y; sign 3.2 units along +y, at the kerb
        fwd = np.array([-math.sin(h), math.cos(h)])
        to_road = -np.array([math.cos(h), math.sin(h)])
        P = np.array([b["x"], b["y"]]) + fwd * 3.3 + to_road * 0.75
        # right-hand traffic: the lane next to the shelter has the shelter on its right,
        # right(d) = (d.y, -d.x) = -to_road  ->  d = (to_road.y, -to_road.x)
        d = np.array([to_road[1], -to_road[0]])
        _place(ctx, out, "bus", P, d, 1, alts=(P + fwd * 1.0,))
    return out


def _sample(p, s_query):
    s = profiles.arclen(p)
    s_query = np.clip(np.asarray(s_query, float), 0, s[-1])
    i = np.clip(np.searchsorted(s, s_query, side="right") - 1, 0, len(p) - 2)
    f = (s_query - s[i]) / np.maximum(s[i + 1] - s[i], 1e-9)
    pts = p[i] + (p[i + 1] - p[i]) * f[:, None]
    t = p[i + 1] - p[i]
    t = t / np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
    return pts, t
