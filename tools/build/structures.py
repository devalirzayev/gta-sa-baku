"""Road structures: bridge decks / viaducts with parapets and piers, ramps on retaining walls,
open trenches and covered tunnels.  Built from edges whose surface lift |lift| > 0.3."""
from __future__ import annotations

import math

import numpy as np
import shapely

from tools.geo import profiles
from tools.geo.roadmesh import polyline_normals
from tools.rw.col import SURF, light_byte
from tools.build.mesh import strip, wall

LIFT_EPS = 0.3
DECK_T = 1.2          # deck thickness
PARAPET_H = 1.0
PARAPET_W = 0.45
PIER_STEP = 22.0
VIADUCT_MIN = 3.6     # above this lift use piers instead of solid walls
TUNNEL_H = 6.2        # clear height inside covered tunnels


def spans(e):
    """Contiguous runs of structure vertices: list of (i0, i1) inclusive, padded by one."""
    lf = np.abs(e.lift)
    m = lf > LIFT_EPS
    out = []
    i = 0
    n = len(m)
    while i < n:
        if m[i]:
            j = i
            while j + 1 < n and m[j + 1]:
                j += 1
            out.append((max(0, i - 1), min(n - 1, j + 1)))
            i = j + 1
        else:
            i += 1
    return out


def span_polygon(e, i0, i1, extra=0.0):
    c = e.pts[i0:i1 + 1]
    if len(c) < 2:
        return shapely.Polygon()
    n = polyline_normals(c)
    hw = e.width / 2 + extra
    a, b = c + n * hw, c - n * hw
    p = shapely.Polygon(np.vstack([a, b[::-1]]))
    return p if p.is_valid else p.buffer(0)


def trench_footprint(g):
    """Polygons of open (uncovered) depressed road sections -> carved out of the ground."""
    polys = []
    for e in g.edges.values():
        if not e.major:
            continue
        for i0, i1 in spans(e):
            lf = e.lift[i0:i1 + 1]
            if lf.min() >= -LIFT_EPS:
                continue
            covered = e.tunnel
            if covered:
                # only the portal ramps (shallow parts) are open
                idx = np.nonzero(np.abs(lf) < TUNNEL_H + 0.5)[0]
                if len(idx) == 0:
                    continue
            polys.append(span_polygon(e, i0, i1, extra=PARAPET_W + 0.2))
    return polys


def build_structures_tile(W, box, mb, road_tex):
    g = W.g
    x0, y0, x1, y1 = box.bounds
    for e in g.edges.values():
        if not e.major:
            continue
        for i0, i1 in spans(e):
            c = e.pts[i0:i1 + 1]
            if len(c) < 2:
                continue
            mid = (c[:-1] + c[1:]) / 2
            inside = (mid[:, 0] >= x0) & (mid[:, 0] < x1) & (mid[:, 1] >= y0) & (mid[:, 1] < y1)
            if not inside.any():
                continue
            zg = e.z[i0:i1 + 1]
            lf = e.lift[i0:i1 + 1]
            ztop = zg + lf
            s = profiles.arclen(e.pts)[i0:i1 + 1]
            n = polyline_normals(c)
            hw = e.width / 2
            L = c + n * hw
            R = c - n * hw
            tex = road_tex.get((e.lanes_f, e.lanes_b), "bk_asph")
            width = 2 * hw
            # --- road surface ---
            V, UV, T = strip(L, R, ztop, ztop, s / max(width, 6.0))
            sel = np.r_[inside, inside]
            dark = 0.55 if lf.min() < -LIFT_EPS and e.tunnel else 1.0
            mb.add(tex, V, UV, T[sel], shade=dark, night=dark, surface=SURF["TARMAC"])
            elevated = lf.max() > LIFT_EPS
            if elevated:
                _deck(mb, c, n, hw, zg, ztop, lf, inside)
            else:
                _trench(mb, c, n, hw, zg, ztop, lf, inside, e.tunnel)


def _quads_between(mb, mat, P, z_bot, z_top, sel, face_left, shade, surface, uscale=4.0, vscale=4.0):
    """Vertical wall along polyline P (segments k->k+1 where sel[k]) from z_bot to z_top."""
    for k in np.nonzero(sel)[0]:
        V, UV, Tw = wall(P[k], P[k + 1], z_bot[k], z_bot[k + 1], z_top[k], z_top[k + 1],
                         facing_left=face_left, u_scale=uscale, v_scale=vscale)
        mb.add(mat, V, UV, Tw, shade=shade, surface=surface)


def _parapet_top(mb, c, n, hw, side, z, sel):
    inner = c + n * hw * side
    outer = c + n * (hw + PARAPET_W) * side
    left, right = (outer, inner) if side > 0 else (inner, outer)
    Vt, UVt, Tt = strip(left, right, z, z, profiles.arclen(c) / 4.0)
    mb.add("bk_conc", Vt, UVt, Tt[np.r_[sel, sel]], shade=1.0, surface=SURF["PAVEMENT"])


def _deck(mb, c, n, hw, zg, ztop, lf, inside):
    conc = SURF["PAVEMENT"]
    bottom = np.where(lf > VIADUCT_MIN, ztop - DECK_T, zg - 0.6)
    for side in (+1, -1):
        inner = c + n * hw * side
        outer = c + n * (hw + PARAPET_W) * side
        _quads_between(mb, "bk_conc", inner, ztop, ztop + PARAPET_H, inside, side < 0, 0.9, conc)
        _parapet_top(mb, c, n, hw, side, ztop + PARAPET_H, inside)
        _quads_between(mb, "bk_conc", outer, bottom, ztop + PARAPET_H, inside, side > 0, 0.82, conc)
    via = lf > VIADUCT_MIN
    if via.any():
        Lo = c + n * (hw + PARAPET_W)
        Ro = c - n * (hw + PARAPET_W)
        zb = ztop - DECK_T
        V, UV, T = strip(Ro, Lo, zb, zb, profiles.arclen(c) / 6.0)   # reversed -> faces down
        selq = inside & via[:-1] & via[1:]
        mb.add("bk_conc", V, UV, T[np.r_[selq, selq]], shade=0.55, night=0.6, surface=conc)
        s = profiles.arclen(c)
        next_s = s[0] + PIER_STEP / 2
        for k in range(len(c) - 1):
            if s[k] >= next_s and via[k] and inside[k]:
                _pier(mb, c[k], n[k], hw, zg[k], ztop[k] - DECK_T)
                next_s = s[k] + PIER_STEP


def _pier(mb, p, nrm, hw, z0, z1):
    t = np.array([nrm[1], -nrm[0]])   # along-road direction
    half_w = min(hw * 0.55, 6.0)
    half_t = 1.0
    corners = [p + nrm * half_w + t * half_t, p - nrm * half_w + t * half_t,
               p - nrm * half_w - t * half_t, p + nrm * half_w - t * half_t]
    # walls facing outwards: corners are clockwise when seen from above? ensure CCW order
    cc = np.array(corners)
    area = 0.5 * np.sum(cc[:, 0] * np.roll(cc[:, 1], -1) - np.roll(cc[:, 0], -1) * cc[:, 1])
    if area < 0:
        cc = cc[::-1]
    for i in range(4):
        a, b = cc[i], cc[(i + 1) % 4]
        V, UV, T = wall(a, b, z0 - 0.5, z0 - 0.5, z1, z1, facing_left=False, u_scale=3.0, v_scale=3.0)
        mb.add("bk_conc", V, UV, T, shade=0.8, surface=SURF["PAVEMENT"])


def _trench(mb, c, n, hw, zg, ztop, lf, inside, covered):
    conc = SURF["PAVEMENT"]
    cov = (np.abs(lf) >= TUNNEL_H - 0.2) & covered
    opn = ~cov
    for side in (+1, -1):
        inner = c + n * hw * side
        outer = c + n * (hw + PARAPET_W) * side
        top = np.where(cov, ztop + TUNNEL_H, zg + PARAPET_H)
        _quads_between(mb, "bk_conc", inner, ztop, top, inside, side < 0, 0.7, conc)
        sel = inside & opn[:-1] & opn[1:]
        if sel.any():
            _quads_between(mb, "bk_conc", outer, zg - 0.2, zg + PARAPET_H, sel, side > 0, 0.85, conc)
            _parapet_top(mb, c, n, hw, side, zg + PARAPET_H, sel)
    sel = inside & cov[:-1] & cov[1:]
    if sel.any():
        Lw = c + n * hw
        Rw = c - n * hw
        zc = ztop + TUNNEL_H
        V, UV, T = strip(Rw, Lw, zc, zc, profiles.arclen(c) / 6.0)   # faces down
        mb.add("bk_conc", V, UV, T[np.r_[sel, sel]], shade=0.45, night=0.5, surface=conc)
