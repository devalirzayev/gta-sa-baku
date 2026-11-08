"""Baku landmarks as custom models (the SA buildings that buildings.py put on these footprints
are dropped by the packager):

  * Heydər Əliyev Mərkəzi - white shell rising from the plaza in three waves, lifted on the
    south-west side over a glass wall;
  * Alov qüllələri (Flame Towers) - three lofted flame-shaped glass towers on their podium; the
    night vertex colours make them glow orange-red like their LED flames;
  * Heydər məscidi - prayer hall with arched windows, drum + dome, four minarets with balconies;
  * Qız qalası (Maiden Tower) - limestone tower from its OSM footprint.

Footprints come from the Overture buildings (ids below).  Heights use the map scale 0.675 with a
small boost so the landmarks keep their dominance in the skyline (LANDMARK_SCALE).
Each landmark = HD model (dd 299, full collision) + LOD model (dd 1800, BIG building; inherits
the HD collision model because it has exactly one child).
"""
from __future__ import annotations

import math
import os

import numpy as np
import pyarrow.parquet as pq
import shapely

from tools.build import ground
from tools.build.mesh import MeshBuilder, triangulate_area
from tools.geo.proj import lonlat_to_game
from tools.rw import col as COL, dff as DFF, txd as TXD
from tools.rw.col import SURF
from tools.tex import landmark_tex as LT

LANDMARK_SCALE = 0.75
COARSE = False            # True while building the low-detail collision version of a landmark
LM_TXD = "bk_lmtx"
LM_BASE = 15100
SUN = np.array([-0.45, -0.55, 0.70]) / np.linalg.norm([-0.45, -0.55, 0.70])

FOOTPRINTS = {
    "hac": "993c5240-163",
    "flame1": "c3674177-fed", "flame2": "5c00964e-f12", "flame3": "43ec16b3-5bd", "podium": "2cc49c7e-480",
    "mosque": "77b6f5a6-476", "mosque_c": "9a122faa-4f3",
    "maiden": "b540e6e6-fe1",
}


def load_footprints():
    t = pq.read_table("data/raw/building.parquet", columns=["id", "geometry"]).to_pylist()
    out = {}
    for k, pre in FOOTPRINTS.items():
        for r in t:
            if r["id"].startswith(pre):
                g = shapely.from_wkb(r["geometry"])
                g = shapely.transform(g, lambda c: np.c_[lonlat_to_game(c[:, 0], c[:, 1])])
                if g.geom_type == "MultiPolygon":
                    g = max(g.geoms, key=lambda p: p.area)
                out[k] = shapely.Polygon(g.exterior.coords)
                break
    return out


def minaret_points():
    out = []
    for r in pq.read_table("data/raw/infrastructure.parquet", columns=["class", "geometry"]).to_pylist():
        if r["class"] == "minaret":
            p = shapely.from_wkb(r["geometry"])
            out.append(lonlat_to_game(p.x, p.y))
    return np.array(out, float).reshape(-1, 2)


# ------------------------------------------------------------------------------------ helpers
def _shade_from_normals(V, T):
    n = np.zeros_like(V)
    fn = np.cross(V[T[:, 1]] - V[T[:, 0]], V[T[:, 2]] - V[T[:, 0]])
    for k in range(3):
        np.add.at(n, T[:, k], fn)
    n /= np.maximum(np.linalg.norm(n, axis=1), 1e-9)[:, None]
    return np.clip(0.80 + 0.30 * (n @ SUN), 0.58, 1.10), n


def _orient_out(V, T, centre):
    """Flip triangles so their normals point away from `centre` (closed convex-ish shells)."""
    c = V[T].mean(1)
    fn = np.cross(V[T[:, 1]] - V[T[:, 0]], V[T[:, 2]] - V[T[:, 0]])
    flip = ((c - centre) * fn).sum(1) < 0
    T = T.copy()
    T[flip] = T[flip][:, [0, 2, 1]]
    return T


def _resample_ring(P, n):
    P = np.asarray(P, float)
    if np.allclose(P[0], P[-1]):
        P = P[:-1]
    ring = np.vstack([P, P[:1]])
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(ring, axis=0).T))]
    q = np.linspace(0, s[-1], n, endpoint=False)
    return np.c_[np.interp(q, s, ring[:, 0]), np.interp(q, s, ring[:, 1])]


def _loft(mb, rings, zs, mat, uscale, vscale, night_rgb=None, surface=SURF["DEFAULT"], cap=True, day_rgb=None,
          centre_xy=None):
    """Quads between consecutive rings (each (n,2) at height zs[k]); optional top cap."""
    rings = [np.asarray(r, float) for r in rings]
    n = len(rings[0])
    V, UV, NR = [], [], []
    for k, (r, z) in enumerate(zip(rings, zs)):
        rr = np.vstack([r, r[:1]])
        s = np.r_[0, np.cumsum(np.hypot(*np.diff(rr, axis=0).T))]
        V.append(np.c_[rr, np.full(n + 1, z)])
        UV.append(np.c_[s / uscale, -np.full(n + 1, z) / vscale])
        if night_rgb is not None:
            NR.append(np.broadcast_to(night_rgb(k, z, rr), (n + 1, 3)))
    V = np.vstack(V)
    UV = np.vstack(UV)
    T = []
    w = n + 1
    for k in range(len(rings) - 1):
        for i in range(n):
            a, b, c, d = k * w + i, k * w + i + 1, (k + 1) * w + i + 1, (k + 1) * w + i
            T += [[a, b, c], [a, c, d]]
    T = np.array(T)
    # orient each quad outward from the axis through the ring centres (each ring has its own
    # centre: lofts may lean, like the flame towers)
    ring_c = np.array([r.mean(0) for r in rings])
    if centre_xy is not None and len(rings) == 2:
        ring_c[:] = np.asarray(centre_xy)
    quad_ring = np.repeat(np.arange(len(rings) - 1), 2 * n)
    cxy = (ring_c[quad_ring] + ring_c[quad_ring + 1]) / 2
    cen = V[T].mean(1)
    fn = np.cross(V[T[:, 1]] - V[T[:, 0]], V[T[:, 2]] - V[T[:, 0]])
    radial = cen[:, :2] - cxy
    flip = (radial * fn[:, :2]).sum(1) < 0
    T[flip] = T[flip][:, [0, 2, 1]]
    sh, _ = _shade_from_normals(V, T)
    nr = np.vstack(NR) if NR else None
    mb.add(mat, V, UV, T, shade=sh, night_rgb=nr, day_rgb=day_rgb, surface=surface)
    if cap:
        top = rings[-1]
        c = top.mean(0)
        Vc = np.vstack([np.c_[top, np.full(n, zs[-1])], [*c, zs[-1] + 0.01]])
        Tc = np.array([[n, i, (i + 1) % n] for i in range(n)])
        Tc = _orient_out(Vc, Tc, np.array([*c, zs[-1] - 10.0]))
        cap_night = None if nr is None else np.vstack([nr[-(n + 1):-1], nr[-(n + 1):-1].mean(0, keepdims=True)])
        mb.add(mat, Vc, Vc[:, :2] / uscale, Tc, shade=1.0, surface=surface, night_rgb=cap_night)


def _extrude(mb, poly, z0, z1, wall_mat, roof_mat, uscale=8.0, vscale=8.0, night=None, roof=True, wall_night_rgb=None):
    ring = np.asarray(poly.exterior.coords)[:-1]
    if not shapely.LinearRing(ring).is_ccw:
        ring = ring[::-1]
    n = len(ring)
    rr = np.vstack([ring, ring[:1]])
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(rr, axis=0).T))]
    V = np.vstack([np.c_[rr, np.full(n + 1, z0)], np.c_[rr, np.full(n + 1, z1)]])
    UV = np.vstack([np.c_[s / uscale, np.full(n + 1, -z0 / vscale)], np.c_[s / uscale, np.full(n + 1, -z1 / vscale)]])
    T = []
    for i in range(n):
        a, b, c, d = i, i + 1, n + 1 + i + 1, n + 1 + i
        T += [[a, b, c], [a, c, d]]          # ccw ring -> outward normals with this winding
    T = np.array(T)
    sh, _ = _shade_from_normals(V, T)
    mb.add(wall_mat, V, UV, T, shade=sh, night=night, surface=SURF["DEFAULT"], night_rgb=wall_night_rgb)
    if roof:
        P, Tr = triangulate_area(poly, 50.0)
        if len(Tr):
            Vr = np.c_[P, np.full(len(P), z1)]
            fn = np.cross(Vr[Tr[:, 1]] - Vr[Tr[:, 0]], Vr[Tr[:, 2]] - Vr[Tr[:, 0]])
            Tr = Tr.copy()
            Tr[fn[:, 2] < 0] = Tr[fn[:, 2] < 0][:, [0, 2, 1]]
            mb.add(roof_mat, Vr, P / 10.0, Tr, shade=1.02, surface=SURF["DEFAULT"])


def _dome(mb, c, r, z0, h, mat, n=32, m=10, pointed=0.0, night_rgb=None):
    """Dome over a circle (c, r) at z0, height h; pointed > 0 gives an onion/ogee tip."""
    if COARSE:
        n, m = max(8, n // 4), max(3, m // 3)
    rings, zs = [], []
    for k in range(m + 1):
        t = k / m
        a = t * math.pi / 2
        rad = r * math.cos(a) * (1 - pointed * t * t)
        rad = max(rad, 0.02)
        rings.append(np.c_[c[0] + rad * np.cos(np.linspace(0, 2 * math.pi, n, endpoint=False)),
                           c[1] + rad * np.sin(np.linspace(0, 2 * math.pi, n, endpoint=False))])
        zs.append(z0 + h * math.sin(a) + pointed * h * 0.25 * t ** 3)
    _loft(mb, rings, zs, mat, 4.0, 4.0, cap=True, centre_xy=c,
          night_rgb=(lambda k, z, rr: night_rgb) if night_rgb is not None else None)


def _cylinder(mb, c, r, z0, z1, mat, n=20, cap=True, vscale=6.0, uscale=4.0):
    if COARSE:
        n = max(6, n // 3)
    ring = np.c_[c[0] + r * np.cos(np.linspace(0, 2 * math.pi, n, endpoint=False)),
                 c[1] + r * np.sin(np.linspace(0, 2 * math.pi, n, endpoint=False))]
    _loft(mb, [ring, ring], [z0, z1], mat, uscale, vscale, cap=cap, centre_xy=c)


def _ground_z(W, poly, pct=5):
    P, _ = triangulate_area(poly, 6.0)
    if len(P) == 0:
        c = poly.centroid
        P = np.array([[c.x, c.y]])
    return float(np.percentile(ground.H_at(W, P[:, 0], P[:, 1]), pct))


# ------------------------------------------------------------------------------------ builders
def build_hac(W, poly):
    """Heydar Aliyev Centre: a white shell in three waves along the building's long axis.  At the
    west end it sweeps down to the plaza; elsewhere its edge is lifted over glazed facades."""
    from scipy.spatial import cKDTree
    c = np.array(poly.centroid.coords[0])
    z0 = _ground_z(W, poly) + ground.KERB
    loc = shapely.transform(poly, lambda p: p - c)
    mb = MeshBuilder()
    P, Tr = triangulate_area(loc.simplify(1.0) if COARSE else loc, 8.0 if COARSE else 2.0)
    ext = loc.exterior
    d = shapely.distance(shapely.points(P), ext)
    E = np.asarray(ext.coords)
    w, v = np.linalg.eigh(np.cov((E - E.mean(0)).T))
    ax = v[:, np.argmax(w)]
    if ax[0] < 0:
        ax = -ax                                     # long axis pointing east
    a = P @ ax
    a0, a1 = a.min(), a.max()
    t = (a - a0) / max(a1 - a0, 1e-9)

    def g(x, m, s_):
        return np.exp(-((x - m) / s_) ** 2)
    hmax = 74.0 * LANDMARK_SCALE * 0.9               # 74 m to the highest point
    # boundary segments with outward normals; edge height of the shell above the glass facades
    Ec = E if np.allclose(E[0], E[-1]) else np.vstack([E, E[:1]])
    ccw = shapely.LinearRing(Ec).is_ccw
    mids, nrms = [], []
    for i in range(len(Ec) - 1):
        p, q = Ec[i], Ec[i + 1]
        tv = (q - p) / max(np.hypot(*(q - p)), 1e-9)
        nrms.append(np.array([tv[1], -tv[0]]) if ccw else np.array([-tv[1], tv[0]]))
        mids.append((p + q) / 2)
    mids, nrms = np.array(mids), np.array(nrms)
    perp = np.array([-ax[1], ax[0]])
    t_mid = ((mids @ ax) - a0) / max(a1 - a0, 1e-9)
    south = (nrms @ perp) < 0
    lift_seg = np.clip((t_mid - 0.10) / 0.30, 0, 1) * np.where(south, 15.0, 10.0)
    _, idx = cKDTree(mids).query(P)
    edge = lift_seg[idx]
    wave = 0.50 * g(t, 0.16, 0.12) + 0.80 * g(t, 0.47, 0.14) + 1.0 * g(t, 0.83, 0.13)
    H_t = 16.0 + (hmax - 16.0) * wave / wave.max()
    prof = np.sin(np.pi / 2 * np.clip(d / 32.0, 0, 1)) ** 0.85
    z = z0 + edge + np.maximum(H_t - edge, 0) * prof
    V = np.c_[P, z]
    fn = np.cross(V[Tr[:, 1]] - V[Tr[:, 0]], V[Tr[:, 2]] - V[Tr[:, 0]])
    Tr = Tr.copy()
    Tr[fn[:, 2] < 0] = Tr[fn[:, 2] < 0][:, [0, 2, 1]]
    sh, _ = _shade_from_normals(V, Tr)
    sh = np.clip(sh * 1.12, 0.6, 1.2)
    mb.add("lm_white", V, P / 22.0, Tr, shade=sh, night_rgb=np.full((len(V), 3), 150.0), surface=SURF["DEFAULT"])
    # glass facades under the lifted shell edge
    for i in range(len(Ec) - 1):
        hgt = lift_seg[i]
        if hgt < 0.8:
            continue
        p, q = Ec[i], Ec[i + 1]
        Vw = np.array([[*p, z0 - 0.5], [*q, z0 - 0.5], [*q, z0 + hgt + 0.3], [*p, z0 + hgt + 0.3]])
        L = float(np.hypot(*(q - p)))
        UVw = np.array([[0, 0], [L / 8, 0], [L / 8, -hgt / 8], [0, -hgt / 8]])
        Tw = np.array([[0, 1, 2], [0, 2, 3]])
        if np.cross(Vw[1] - Vw[0], Vw[2] - Vw[0])[:2] @ nrms[i] < 0:
            Tw = Tw[:, [0, 2, 1]]
        mb.add("lm_glass", Vw, UVw, Tw, shade=0.95, night_rgb=(230, 205, 150), surface=SURF["GLASS"])
    # skirt below the shell edge (the terrain is not perfectly flat under the footprint)
    n = len(Ec) - 1
    Vs = np.vstack([np.c_[Ec, np.full(len(Ec), z0 - 3.0)], np.c_[Ec, np.full(len(Ec), z0 + 0.3)]])
    Ts = []
    for i in range(n):
        Ts += [[i, i + 1, len(Ec) + i + 1], [i, len(Ec) + i + 1, len(Ec) + i]]
    Ts = _orient_out(Vs, np.array(Ts), np.array([0, 0, z0]))
    mb.add("lm_white", Vs, np.c_[np.arange(len(Vs)) % len(Ec), Vs[:, 2] / 8], Ts, shade=0.9, surface=SURF["DEFAULT"])
    return mb, (c[0], c[1], z0)


def build_flame(W, polys, podium):
    """Three flame towers + podium, one model (origin at the trio centre)."""
    cents = [np.array(p.centroid.coords[0]) for p in polys]
    trio = np.mean(cents, axis=0)
    z0 = min(_ground_z(W, p) for p in polys) + ground.KERB
    mb = MeshBuilder()
    heights = [182.0, 165.0, 160.0]
    for poly, H_real, cen in zip(polys, heights, cents):
        H = H_real * LANDMARK_SCALE
        ring0 = _resample_ring(np.asarray(poly.exterior.coords) - trio, 16 if COARSE else 56)
        if not shapely.LinearRing(ring0).is_ccw:
            ring0 = ring0[::-1]
        c0 = cen - trio
        inward = -c0 / max(np.hypot(*c0), 1e-9)
        # distance from centroid to the footprint edge towards the trio centre
        reach = float(np.max(((ring0 - c0) @ inward)))
        rings, zs = [], []
        K = 12 if COARSE else 44
        for k in range(K + 1):
            t = k / K
            s = 1.0 if t < 0.18 else max(0.0, 1 - ((t - 0.18) / 0.82) ** 2.0) ** 0.75
            cc = c0 + inward * (1 - s) * reach * 0.9
            rings.append(cc + (ring0 - c0) * s)        # last ring collapses to the tip
            zs.append(z0 + H * t)

        def night(k, z, rr, H=H):
            t = (z - z0) / H
            ang = np.arctan2(rr[:, 1] - rr[:, 1].mean(), rr[:, 0] - rr[:, 0].mean())
            wave = 0.5 + 0.5 * np.sin(ang * 3 + t * 9.0)
            r = 255.0
            g = 35 + 105 * t + 40 * wave            # red at the foot, orange-yellow tongues higher up
            b = 8 + 28 * t * t
            return np.c_[np.full(len(rr), r), g, np.full(len(rr), b)]
        _loft(mb, rings, zs, "lm_flame", 16.0, 25.6, night_rgb=night, surface=SURF["GLASS"], centre_xy=c0, cap=False)
    # podium
    if podium is not None:
        loc = shapely.transform(podium, lambda p: p - trio)
        _extrude(mb, loc, z0 - 4.0, z0 + 9.0, "lm_glass", "lm_stone", uscale=8.0, vscale=8.0,
                 wall_night_rgb=(210, 190, 140))
    return mb, (trio[0], trio[1], z0)


def build_mosque(W, hall, centre_part, minarets):
    c = np.array(hall.centroid.coords[0])
    z0 = _ground_z(W, hall) + ground.KERB
    mb = MeshBuilder()
    loc = shapely.transform(hall, lambda p: p - c)
    _extrude(mb, loc, z0 - 3.0, z0 + 13.0, "lm_mosque", "lm_stone", uscale=12.0, vscale=16.0,
             wall_night_rgb=(190, 170, 130))
    if centre_part is not None:
        cp = np.array(centre_part.centroid.coords[0]) - c
        r = math.sqrt(centre_part.area / math.pi)
    else:
        cp = np.zeros(2)
        r = 7.0
    _cylinder(mb, cp, r * 0.92, z0 + 13.0, z0 + 18.0, "lm_mosque", n=28, cap=False, vscale=16.0, uscale=12.0)
    _dome(mb, cp, r * 0.95, z0 + 18.0, r * 1.05, "lm_dome", n=32, m=12, pointed=0.25, night_rgb=(70, 96, 130))
    _cylinder(mb, cp, 0.18, z0 + 18.0 + r * 1.05, z0 + 18.0 + r * 1.05 + 3.0, "lm_gold", n=8)
    for m in minarets:
        mp = m - c
        H = 95.0 * LANDMARK_SCALE * 0.9
        _cylinder(mb, mp, 1.45, z0 - 2.0, z0 + H * 0.62, "lm_stone", n=16)
        _cylinder(mb, mp, 2.3, z0 + H * 0.62, z0 + H * 0.62 + 0.9, "lm_stone", n=16)        # balcony 1
        _cylinder(mb, mp, 1.25, z0 + H * 0.62 + 0.9, z0 + H * 0.84, "lm_stone", n=16)
        _cylinder(mb, mp, 2.0, z0 + H * 0.84, z0 + H * 0.84 + 0.8, "lm_stone", n=16)         # balcony 2
        _cylinder(mb, mp, 1.1, z0 + H * 0.84 + 0.8, z0 + H * 0.92, "lm_stone", n=16)
        _dome(mb, mp, 1.25, z0 + H * 0.92, H * 0.08, "lm_dome", n=16, m=6, pointed=0.6)
        _cylinder(mb, mp, 0.1, z0 + H * 1.0, z0 + H * 1.0 + 1.6, "lm_gold", n=6)
    return mb, (c[0], c[1], z0)


def build_maiden(W, poly):
    c = np.array(poly.centroid.coords[0])
    z0 = _ground_z(W, poly) + ground.KERB
    mb = MeshBuilder()
    loc = shapely.transform(poly, lambda p: p - c)
    H = 29.5 * LANDMARK_SCALE
    _extrude(mb, loc, z0 - 3.0, z0 + H, "lm_stone", "lm_stone", uscale=8.0, vscale=8.0, night=1.4)
    # crown: slightly projecting parapet
    crown = loc.buffer(0.35, join_style="mitre")
    _extrude(mb, crown, z0 + H - 0.2, z0 + H + 1.2, "lm_stone", "lm_stone", uscale=8.0, vscale=8.0, night=1.4)
    return mb, (c[0], c[1], z0)


# ------------------------------------------------------------------------------------ packaging
SPECS = [  # key, model name, lod name
    ("hac", "bk_lm_hac", "bk_lm_hac_l"),
    ("flame", "bk_lm_flame", "bk_lm_flame_l"),
    ("mosque", "bk_lm_mosque", "bk_lm_mosque_l"),
    ("maiden", "bk_lm_maiden", "bk_lm_maiden_l"),
]


def build_all(W, out_dir, verbose=True):
    global COARSE
    fp = load_footprints()
    mins = minaret_points()
    near = mins[np.hypot(*(mins - np.array(fp["mosque"].centroid.coords[0])).T) < 60] if len(mins) else mins

    def build_set():
        return {"hac": build_hac(W, fp["hac"]),
                "flame": build_flame(W, [fp["flame1"], fp["flame2"], fp["flame3"]], fp.get("podium")),
                "mosque": build_mosque(W, fp["mosque"], fp.get("mosque_c"), near),
                "maiden": build_maiden(W, fp["maiden"])}
    built = build_set()
    COARSE = True                 # low-detail twins for the collision models (600-triangle limit)
    try:
        coarse = build_set()
    finally:
        COARSE = False
    os.makedirs(out_dir, exist_ok=True)
    meta = []
    for i, (key, name, lname) in enumerate(SPECS):
        mb, origin = built[key]
        # builders work in xy relative to the instance position, z absolute
        geo, _ = mb.build((0.0, 0.0, origin[2]))
        _, (cV, cT, cS, cL) = coarse[key][0].build((0.0, 0.0, origin[2]))
        V = np.asarray(geo.vertices, float)
        hd_id, lod_id = LM_BASE + 2 * i, LM_BASE + 2 * i + 1
        open(os.path.join(out_dir, name + ".dff"), "wb").write(DFF.write_dff(name, geo))
        open(os.path.join(out_dir, lname + ".dff"), "wb").write(DFF.write_dff(lname, geo))
        cm = COL.ColModel(name, hd_id, cV, cT, cS, cL)
        open(os.path.join(out_dir, name + ".col"), "wb").write(COL.write_col3(cm))
        cl = COL.ColModel(lname, lod_id, bounds=(V.min(0), V.max(0)))
        open(os.path.join(out_dir, lname + ".col"), "wb").write(COL.write_col3(cl))
        meta.append({"key": key, "name": name, "id": hd_id, "lod_name": lname, "lod_id": lod_id, "txd": LM_TXD,
                     "origin": list(map(float, origin)), "verts": int(len(V)), "tris": int(len(geo.triangles)),
                     "col_faces": int(len(cT)) if cT is not None else 0,
                     "height": float(V[:, 2].max() - V[:, 2].min())})
        if verbose:
            print(f"{name}: {len(V)} verts, {len(geo.triangles)} tris, height {meta[-1]['height']:.0f}")
    tex = [TXD.texture_native(k, f(), encoding="DXT1") for k, f in LT.TEXTURES.items()]
    open(os.path.join(out_dir, LM_TXD + ".txd"), "wb").write(TXD.write_txd(tex))
    footprints = {k: shapely.to_wkt(v) for k, v in fp.items()}
    return {"models": meta, "footprints": footprints}


if __name__ == "__main__":
    import json
    W = ground.prepare()
    m = build_all(W, "build/out/landmarks")
    json.dump(m, open("build/out/landmarks/landmarks.json", "w"), indent=1)
