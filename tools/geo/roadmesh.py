"""2D road surface layout: carriageway ribbons, junction polygons, sidewalks.

Works on a displaced RoadGraph (tools.geo.roads). Produces shapely geometry plus per-edge
"ribbon" descriptions (centerline, trimmed arc range, widths) that the mesher turns into
UV-mapped strips.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.ops import unary_union

from .roads import RoadGraph, Edge, densify, junction_clusters

CORNER_R = 4.0          # kerb corner radius at junctions (game units)


def polyline_normals(p: np.ndarray) -> np.ndarray:
    """Unit left normals at each vertex (miter-averaged)."""
    d = np.diff(p, axis=0)
    L = np.hypot(d[:, 0], d[:, 1])[:, None]
    t = d / np.maximum(L, 1e-9)
    n_seg = np.c_[-t[:, 1], t[:, 0]]
    n = np.zeros_like(p)
    n[0] = n_seg[0]
    n[-1] = n_seg[-1]
    if len(p) > 2:
        m = n_seg[:-1] + n_seg[1:]
        ml = np.hypot(m[:, 0], m[:, 1])[:, None]
        m = m / np.maximum(ml, 1e-9)
        # miter length correction, clamped
        cos_half = np.clip((m * n_seg[:-1]).sum(1), 0.35, 1.0)[:, None]
        n[1:-1] = m / cos_half
    return n


def arclen(p):
    return np.r_[0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]


def cut_polyline(p, a, b):
    """Sub-polyline between arc lengths a < b (interpolated ends)."""
    s = arclen(p)
    a = max(0.0, min(a, s[-1]))
    b = max(a, min(b, s[-1]))
    def at(t):
        i = int(np.searchsorted(s, t, side="right") - 1)
        i = min(max(i, 0), len(p) - 2)
        f = (t - s[i]) / max(s[i + 1] - s[i], 1e-9)
        return p[i] + (p[i + 1] - p[i]) * f
    inner = p[(s > a + 1e-6) & (s < b - 1e-6)]
    return np.vstack([at(a), inner, at(b)])


@dataclass
class Ribbon:
    edge: Edge
    center: np.ndarray          # trimmed centerline (densified)
    s0: float                   # arc position of center[0] on the full edge
    left: float                 # carriageway half width to the left (+normal)
    right: float
    sw_left: float
    sw_right: float

    def side_lines(self, extra_l=0.0, extra_r=0.0):
        n = polyline_normals(self.center)
        return self.center + n * (self.left + extra_l), self.center - n * (self.right + extra_r)

    def polygon(self, extra_l=0.0, extra_r=0.0):
        a, b = self.side_lines(extra_l, extra_r)
        poly = shapely.Polygon(np.vstack([a, b[::-1]]))
        if not poly.is_valid:
            poly = shapely.make_valid(poly)
            poly = unary_union([g for g in getattr(poly, "geoms", [poly]) if g.geom_type == "Polygon"])
        return poly

    def strip_polygon(self, side, w0, w1):
        """Polygon between offsets w0..w1 on one side (+1 left, -1 right)."""
        n = polyline_normals(self.center) * side
        a = self.center + n * w0
        b = self.center + n * w1
        poly = shapely.Polygon(np.vstack([a, b[::-1]]))
        if not poly.is_valid:
            poly = shapely.make_valid(poly)
            poly = unary_union([g for g in getattr(poly, "geoms", [poly]) if g.geom_type == "Polygon"])
        return poly


@dataclass
class Junction:
    id: int
    nodes: set
    arms: list                  # (edge id, end 'u'|'v')
    internal: list              # internal edge ids
    polygon: object = None
    sidewalk: float = 0.0
    height_node: int = -1


@dataclass
class RoadLayout:
    ribbons: dict = field(default_factory=dict)       # edge id -> Ribbon
    junctions: dict = field(default_factory=dict)     # junction id -> Junction
    node_junction: dict = field(default_factory=dict)  # node -> junction id
    cuts: dict = field(default_factory=dict)          # (edge id, 'u'|'v') -> cut distance
    road_area: object = None
    sidewalk_area: object = None


def _half_widths(e: Edge):
    return e.width / 2, e.width / 2


def build_layout(g: RoadGraph, level=0, verbose=True) -> RoadLayout:
    """Layout of major roads on one level (0 = ground)."""
    lay = RoadLayout()
    edges = {k: e for k, e in g.edges.items() if e.major and e.level == level}
    for e in edges.values():
        e.pts = densify(e.pts, 3.0)
    deg = defaultdict(list)
    for e in edges.values():
        deg[e.u].append((e.id, "u"))
        deg[e.v].append((e.id, "v"))
    clusters = junction_clusters(g)
    # group junction nodes (degree >= 3, or cluster members) into junctions
    groups = defaultdict(set)
    for n, lst in deg.items():
        c = clusters.get(n, n)
        groups[c].add(n)
    jid = 0
    for c, nodes in groups.items():
        arms, internal = [], []
        for n in nodes:
            for eid, end in deg[n]:
                e = edges[eid]
                other = e.v if end == "u" else e.u
                if other in nodes and e.length < 28.0 and e.attrs.get("ring") is None:
                    if eid not in internal:
                        internal.append(eid)
                else:
                    arms.append((eid, end))
        if len(arms) + 2 * len(internal) <= 2 and not internal:
            continue  # simple continuation or dead end: no junction polygon
        j = Junction(jid, nodes, arms, internal)
        lay.junctions[jid] = j
        for n in nodes:
            lay.node_junction[n] = jid
        jid += 1

    # --- cut distances per arm ---
    full_poly = {}
    for e in edges.values():
        r = Ribbon(e, e.pts, 0.0, e.width / 2, e.width / 2, e.attrs.get("sw_left", e.sidewalk),
                   e.attrs.get("sw_right", e.sidewalk))
        full_poly[e.id] = r.polygon()
    for j in lay.junctions.values():
        others_all = [full_poly[a] for a, _ in j.arms] + [full_poly[i] for i in j.internal]
        for idx, (eid, end) in enumerate(j.arms):
            e = edges[eid]
            p = e.pts if end == "u" else e.pts[::-1]
            s = arclen(p)
            n = polyline_normals(p)
            d0 = _dir_at(p, 12.0)
            blockers = []
            for k2, (a, end2) in enumerate(j.arms):
                if k2 == idx:
                    continue
                e2 = edges[a]
                p2 = e2.pts if end2 == "u" else e2.pts[::-1]
                if abs(float(np.dot(d0, _dir_at(p2, 12.0)))) > 0.9 and e2.oneway and e.oneway:
                    continue  # parallel carriageway of the same corridor: runs alongside
                blockers.append(full_poly[a])
            others = unary_union(blockers + [full_poly[i] for i in j.internal])
            others = others.buffer(0.3)
            # walk outwards until the cross-section clears all other ribbons
            cut = 0.0
            hw = e.width / 2 + 0.5
            for k in range(len(p)):
                seg = shapely.LineString([p[k] + n[k] * hw, p[k] - n[k] * hw])
                if not seg.intersects(others):
                    cut = s[k]
                    break
            else:
                cut = s[-1]
            cut = min(cut + CORNER_R * 0.8, s[-1] * 0.5 if _other_end_junction(lay, e, end) else s[-1] * 0.9)
            lay.cuts[(eid, end)] = cut
    # --- ribbons ---
    for e in edges.values():
        a = lay.cuts.get((e.id, "u"), 0.0)
        b = e.length - lay.cuts.get((e.id, "v"), 0.0)
        internal = any(e.id in j.internal for j in lay.junctions.values())
        if internal or b - a < 0.5:
            continue
        c = cut_polyline(e.pts, a, b)
        lay.ribbons[e.id] = Ribbon(e, c, a, e.width / 2, e.width / 2,
                                   e.attrs.get("sw_left", e.sidewalk), e.attrs.get("sw_right", e.sidewalk))
    # --- junction polygons ---
    for j in lay.junctions.values():
        parts = []
        sws = []
        for eid, end in j.arms:
            e = edges[eid]
            cut = lay.cuts[(eid, end)]
            p = e.pts if end == "u" else e.pts[::-1]
            sub = cut_polyline(p, 0.0, cut + 0.05)
            r = Ribbon(e, sub, 0.0, e.width / 2, e.width / 2, 0, 0)
            parts.append(r.polygon())
            sws.append(max(e.attrs.get("sw_left", e.sidewalk), e.attrs.get("sw_right", e.sidewalk)))
        for i in j.internal:
            parts.append(full_poly[i])
        u = unary_union(parts)
        poly = u.buffer(CORNER_R, join_style="round", quad_segs=6).buffer(-CORNER_R, join_style="round", quad_segs=6)
        poly = poly.union(u)
        j.polygon = poly
        j.sidewalk = max(sws) if sws else 0.0
    # --- areas ---
    road_parts = [r.polygon() for r in lay.ribbons.values()] + [j.polygon for j in lay.junctions.values()]
    lay.road_area = unary_union(road_parts).buffer(0)
    sw_parts = []
    for r in lay.ribbons.values():
        if r.sw_left > 0:
            sw_parts.append(r.strip_polygon(+1, r.left - 0.05, r.left + r.sw_left))
        if r.sw_right > 0:
            sw_parts.append(r.strip_polygon(-1, r.right - 0.05, r.right + r.sw_right))
    for j in lay.junctions.values():
        if j.sidewalk > 0:
            sw_parts.append(j.polygon.buffer(j.sidewalk, join_style="round", quad_segs=6))
    lay.sidewalk_area = unary_union(sw_parts).difference(lay.road_area).buffer(0)
    if verbose:
        print(f"layout level {level}: {len(lay.ribbons)} ribbons, {len(lay.junctions)} junctions, "
              f"road area {lay.road_area.area:.0f}, sidewalk area {lay.sidewalk_area.area:.0f}")
    return lay


def _other_end_junction(lay, e, end):
    other = e.v if end == "u" else e.u
    return other in lay.node_junction


def _dir_at(p, dist):
    """Unit direction from p[0] towards the point `dist` along the polyline."""
    s = arclen(p)
    t = min(dist, s[-1])
    i = int(np.searchsorted(s, t, side="right") - 1)
    i = min(max(i, 0), len(p) - 2)
    f = (t - s[i]) / max(s[i + 1] - s[i], 1e-9)
    q = p[i] + (p[i + 1] - p[i]) * f
    v = q - p[0]
    return v / max(float(np.hypot(*v)), 1e-9)
