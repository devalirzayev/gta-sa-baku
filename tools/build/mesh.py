"""Mesh accumulation helpers: per-tile geometry buffers with materials, prelight and collision."""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import shapely

from tools.rw import dff, col

DAY = np.array([205, 205, 200, 255], np.float64)
NIGHT = np.array([78, 80, 96, 255], np.float64)


class MeshBuilder:
    def __init__(self):
        self.parts = defaultdict(list)   # material -> list of (V, UV, T, day, night)
        self.col = []                    # list of (V, T, surface, light)

    def add(self, mat, V, UV, T, shade=None, night=None, surface=None, light=None, collide=True,
            day_rgb=None, night_rgb=None):
        """shade/night: brightness factors for the default day/night prelight colours;
        day_rgb/night_rgb: explicit colours (3,) or (N,3) overriding them (e.g. lit windows)."""
        V = np.asarray(V, np.float64)
        T = np.asarray(T, np.int64)
        if len(T) == 0:
            return
        UV = np.asarray(UV, np.float64)
        # keep only referenced vertices
        used, inv = np.unique(T.ravel(), return_inverse=True)
        if len(used) < len(V):
            V = V[used]
            UV = UV[used]
            if shade is not None and np.ndim(shade) > 0:
                shade = np.asarray(shade)[used]
            if night is not None and np.ndim(night) > 0:
                night = np.asarray(night)[used]
            if day_rgb is not None and np.ndim(day_rgb) > 1:
                day_rgb = np.asarray(day_rgb)[used]
            if night_rgb is not None and np.ndim(night_rgb) > 1:
                night_rgb = np.asarray(night_rgb)[used]
            T = inv.reshape(-1, 3)
        n = len(V)
        sh = np.ones(n) if shade is None else np.broadcast_to(np.asarray(shade, np.float64), (n,))
        day = np.clip(DAY[None, :3] * sh[:, None], 0, 255)
        if day_rgb is not None:
            day = np.clip(np.broadcast_to(np.asarray(day_rgb, np.float64), (n, 3)) * sh[:, None], 0, 255)
        nshade = sh if night is None else np.broadcast_to(np.asarray(night, np.float64), (n,))
        ngt = np.clip(NIGHT[None, :3] * nshade[:, None], 0, 255)
        if night_rgb is not None:
            ngt = np.clip(np.broadcast_to(np.asarray(night_rgb, np.float64), (n, 3)), 0, 255)
        self.parts[mat].append((V, np.asarray(UV, np.float64), T, day, ngt))
        if collide and surface is not None:
            lt = col.light_byte(15, 7) if light is None else light
            self.col.append((V, T, surface, lt))

    def empty(self):
        return not self.parts

    def build(self, origin, materials_order=None):
        """Merge everything into one dff.Geometry (relative to origin) + collision arrays."""
        o = np.asarray(origin, np.float64)
        mats = sorted(self.parts) if materials_order is None else [m for m in materials_order if m in self.parts]
        Vs, UVs, Ts, Ms, Ds, Ns = [], [], [], [], [], []
        base = 0
        for mi, m in enumerate(mats):
            for V, UV, T, day, ngt in self.parts[m]:
                Vs.append(V - o)
                UVs.append(UV)
                Ts.append(T + base)
                Ms.append(np.full(len(T), mi))
                Ds.append(day)
                Ns.append(ngt)
                base += len(V)
        V = np.vstack(Vs)
        UV = np.vstack(UVs)
        T = np.vstack(Ts)
        M = np.concatenate(Ms)
        D = np.vstack(Ds)
        Nn = np.vstack(Ns)
        V, UV, T, D, Nn, ok = weld(V, UV, T, D, Nn)
        M = M[ok]
        prelit = np.c_[np.round(D).astype(np.uint8), np.full(len(D), 255, np.uint8)]
        night = np.c_[np.round(Nn).astype(np.uint8), np.full(len(Nn), 255, np.uint8)]
        materials = [dff.Material(m) for m in mats]
        geo = dff.Geometry(V.astype(np.float32), UV.astype(np.float32), T, M, materials, prelit, night)
        # collision
        cV, cT, cS, cL = [], [], [], []
        cb = 0
        for Vc, Tc, s, lt in self.col:
            cV.append(Vc - o)
            cT.append(Tc + cb)
            cS.append(np.full(len(Tc), s))
            cL.append(np.full(len(Tc), lt))
            cb += len(Vc)
        if cV:
            cV = np.vstack(cV)
            cT = np.vstack(cT)
            cS = np.concatenate(cS)
            cL = np.concatenate(cL)
            cV, cT, keep = weld_col(cV, cT)
            cS, cL = cS[keep], cL[keep]
        else:
            cV = cT = cS = cL = None
        return geo, (cV, cT, cS, cL)


def weld(V, UV, T, D, Nn, tol=1e-3):
    """Merge vertices with identical position+uv (per tile)."""
    key = np.c_[np.round(V / tol), np.round(UV / 1e-4), np.round(D), np.round(Nn)]
    _, idx, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    T2 = inv[T]
    ok = (T2[:, 0] != T2[:, 1]) & (T2[:, 1] != T2[:, 2]) & (T2[:, 0] != T2[:, 2])
    return V[idx], UV[idx], T2[ok], D[idx], Nn[idx], ok


def weld_col(V, T, tol=1.0 / 128):
    key = np.round(V / tol)
    _, idx, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    T2 = inv[T]
    ok = (T2[:, 0] != T2[:, 1]) & (T2[:, 1] != T2[:, 2]) & (T2[:, 0] != T2[:, 2])
    return V[idx], T2[ok], ok


# ---------------------------------------------------------------------------------------------
def triangulate_area(geom, grid=8.0, origin=(0.0, 0.0)):
    """Triangulate polygonal area, split on a regular grid so the result can follow a height
    field. Returns (P (n,2), T (m,3)) with CCW triangles (seen from +Z)."""
    if geom is None or geom.is_empty:
        return np.zeros((0, 2)), np.zeros((0, 3), np.int64)
    x0, y0, x1, y1 = geom.bounds
    gx0 = np.floor((x0 - origin[0]) / grid) * grid + origin[0]
    gy0 = np.floor((y0 - origin[1]) / grid) * grid + origin[1]
    xs = np.arange(gx0, x1 + grid, grid)
    ys = np.arange(gy0, y1 + grid, grid)
    boxes = shapely.box(*np.meshgrid(xs[:-1], ys[:-1]), *np.meshgrid(xs[1:], ys[1:]))
    boxes = np.asarray(boxes).ravel()
    tree = shapely.STRtree(boxes)
    hit = tree.query(geom, predicate="intersects")
    pieces = shapely.intersection(boxes[hit], geom)
    pieces = pieces[~shapely.is_empty(pieces)]
    tris = shapely.constrained_delaunay_triangles(pieces)
    allt = shapely.get_parts(shapely.get_parts(tris))
    allt = allt[shapely.get_type_id(allt) == 3]
    if len(allt) == 0:
        return np.zeros((0, 2)), np.zeros((0, 3), np.int64)
    coords = shapely.get_coordinates(shapely.get_exterior_ring(allt)).reshape(len(allt), 4, 2)[:, :3]
    # orient CCW
    a, b, c = coords[:, 0], coords[:, 1], coords[:, 2]
    cr = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    keep = np.abs(cr) > 1e-6
    coords = coords[keep]
    cr = cr[keep]
    cw = cr < 0
    coords[cw] = coords[cw][:, [0, 2, 1]]
    P = coords.reshape(-1, 2)
    T = np.arange(len(P)).reshape(-1, 3)
    return P, T


def strip(left, right, zl, zr, v_coord, u_left=0.0, u_right=1.0):
    """Quad strip between two polylines (same length). left is on the +normal side.
    Returns V (2n,3), UV (2n,2), T (2(n-1),3) CCW from above."""
    n = len(left)
    V = np.vstack([np.c_[left, zl], np.c_[right, zr]])
    UV = np.vstack([np.c_[np.full(n, u_left), v_coord], np.c_[np.full(n, u_right), v_coord]])
    k = np.arange(n - 1)
    Lk, Lk1, Rk, Rk1 = k, k + 1, n + k, n + k + 1
    T = np.vstack([np.c_[Rk, Rk1, Lk1], np.c_[Rk, Lk1, Lk]])
    return V, UV, T


def wall(a, b, z_bot_a, z_bot_b, z_top_a, z_top_b, facing_left=True, u_scale=4.0, v_scale=4.0):
    """Vertical quad between points a->b. facing_left: front face normal points to the left
    of the a->b direction."""
    L = float(np.hypot(*(np.asarray(b) - np.asarray(a))))
    V = np.array([[a[0], a[1], z_bot_a], [b[0], b[1], z_bot_b], [b[0], b[1], z_top_b], [a[0], a[1], z_top_a]])
    UV = np.array([[0, z_bot_a / v_scale], [L / u_scale, z_bot_b / v_scale], [L / u_scale, z_top_b / v_scale],
                   [0, z_top_a / v_scale]])
    UV[:, 1] = -UV[:, 1]
    if facing_left:
        T = np.array([[0, 2, 1], [0, 3, 2]])
    else:
        T = np.array([[0, 1, 2], [0, 2, 3]])
    return V, UV, T
