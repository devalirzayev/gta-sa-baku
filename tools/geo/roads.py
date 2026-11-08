"""Road network: load Overture segments, build a topological graph in game units,
assign game lane layouts, and displace roads so that their (wider-than-real) game
widths do not overlap.

Game-specific conventions:
  * SA AI lane width is 5.4 units (CCarPathLink::OneWayLaneOffset uses 5.4).
  * Visual carriageway width = lanes * LANE_VIS + 2 * GUTTER.
  * "major" roads get kerbs, sidewalks, markings and AI paths; "minor" roads (service,
    living_street, track, pedestrian, paths) are flush surfaces inside blocks.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pyarrow.parquet as pq
import shapely
from scipy.spatial import cKDTree

from .proj import lonlat_to_game, WORLD

LANE_AI = 5.4
LANE_VIS = 5.4
GUTTER = 0.5

MAJOR = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified"}
MINOR = {"living_street", "service", "track", "pedestrian", "unknown"}
PATHS = {"footway", "path", "cycleway", "steps", "bridleway"}

CLASS_RANK = {"motorway": 7, "trunk": 6, "primary": 5, "secondary": 4, "tertiary": 3,
              "unclassified": 2, "residential": 2, "living_street": 1, "service": 1, "track": 0,
              "pedestrian": 0, "unknown": 0, "footway": -1, "path": -1, "cycleway": -1,
              "steps": -1, "bridleway": -1}


@dataclass
class Edge:
    id: int
    u: int
    v: int
    pts: np.ndarray
    cls: str
    oneway: int = 0
    lanes_f: int = 1
    lanes_b: int = 1
    level: int = 0
    bridge: bool = False
    tunnel: bool = False
    link: bool = False
    name: str | None = None
    subclass: str | None = None
    surface: str | None = None
    seg_id: str = ""
    width: float = 0.0
    sidewalk: float = 0.0
    speed: int | None = None
    attrs: dict = field(default_factory=dict)

    @property
    def major(self):
        return self.cls in MAJOR

    @property
    def length(self):
        d = np.diff(self.pts, axis=0)
        return float(np.hypot(d[:, 0], d[:, 1]).sum())


@dataclass
class RoadGraph:
    nodes: dict            # node id -> np.array([x, y])
    edges: dict            # edge id -> Edge

    def adjacency(self):
        adj = defaultdict(list)
        for e in self.edges.values():
            adj[e.u].append(e.id)
            adj[e.v].append(e.id)
        return adj


# ---------------------------------------------------------------------------------------------
def _ranges(rules, key_values=None):
    """Convert Overture rule lists [{'values'|'value', 'between'}] to [(a, b, value)]."""
    out = []
    for r in rules or []:
        b = r.get("between") or [0.0, 1.0]
        if key_values is not None:
            for v in r.get("values") or []:
                out.append((b[0], b[1], v))
        else:
            out.append((b[0], b[1], r.get("value")))
    return out


def _oneway(access):
    fwd_denied = bwd_denied = False
    for a in access or []:
        w = a.get("when") or {}
        if a.get("access_type") != "denied" or w.get("mode") or w.get("using") or w.get("during"):
            continue
        if w.get("heading") == "backward":
            bwd_denied = True
        elif w.get("heading") == "forward":
            fwd_denied = True
    if bwd_denied and not fwd_denied:
        return 1
    if fwd_denied and not bwd_denied:
        return -1
    return 0


def load_graph(seg_path="data/raw/segment.parquet", conn_path="data/raw/connector.parquet",
               bbox=(-WORLD - 50, -WORLD - 50, WORLD + 50, WORLD + 50)) -> RoadGraph:
    seg = pq.read_table(seg_path).to_pylist()
    con = pq.read_table(conn_path, columns=["id", "geometry"]).to_pylist()
    con_xy = {}
    for c in con:
        p = shapely.from_wkb(c["geometry"])
        x, y = lonlat_to_game(p.x, p.y)
        con_xy[c["id"]] = np.array([float(x), float(y)])

    nodes = {}
    node_index = {}

    def node_for(cid, xy):
        if cid not in node_index:
            node_index[cid] = len(node_index)
            nodes[node_index[cid]] = np.asarray(xy, dtype=np.float64)
        return node_index[cid]

    edges = {}
    eid = 0
    for r in seg:
        if r["subtype"] != "road":
            continue
        cls = r["class"] or "unknown"
        if cls not in MAJOR and cls not in MINOR and cls not in PATHS:
            continue
        g = shapely.from_wkb(r["geometry"])
        lon, lat = np.array(g.coords).T
        x, y = lonlat_to_game(lon, lat)
        pts = np.c_[x, y]
        # drop duplicate consecutive points
        keep = np.r_[True, np.hypot(*np.diff(pts, axis=0).T) > 1e-6]
        pts = pts[keep]
        if len(pts) < 2:
            continue
        seglen = np.r_[0, np.cumsum(np.hypot(*np.diff(pts, axis=0).T))]
        total = seglen[-1]
        if total <= 0:
            continue
        frac = seglen / total
        # cut positions: connectors (snap to nearest vertex) + flag/level boundaries
        cuts = {}
        for c in r["connectors"] or []:
            cxy = con_xy.get(c["connector_id"])
            if cxy is None:
                continue
            i = int(np.argmin(np.hypot(*(pts - cxy).T)))
            cuts[i] = c["connector_id"]
        flags = _ranges(r["road_flags"], key_values=True)
        levels = _ranges(r["level_rules"])
        bounds = set()
        for a, b, _ in flags + levels:
            for t in (a, b):
                if 0 < t < 1:
                    bounds.add(int(np.argmin(np.abs(frac - t))))
        for i in bounds:
            if i not in cuts and 0 < i < len(pts) - 1:
                cuts[i] = f"{r['id']}#{i}"
        # ensure ends are nodes
        if 0 not in cuts:
            cuts[0] = f"{r['id']}#0"
        if len(pts) - 1 not in cuts:
            cuts[len(pts) - 1] = f"{r['id']}#{len(pts) - 1}"
        idx = sorted(cuts)
        oneway = _oneway(r["access_restrictions"])
        name = (r["names"] or {}).get("primary")
        surf = None
        if r["road_surface"]:
            surf = r["road_surface"][0]["value"]
        speed = None
        for s in r["speed_limits"] or []:
            if s.get("max_speed"):
                speed = s["max_speed"]["value"]
        for a_i, b_i in zip(idx[:-1], idx[1:]):
            if b_i <= a_i:
                continue
            sub = pts[a_i:b_i + 1]
            mid = (frac[a_i] + frac[b_i]) / 2
            fl = {v for a, b, v in flags if a <= mid <= b}
            lv = [v for a, b, v in levels if a <= mid <= b]
            level = int(lv[0]) if lv else 0
            u = node_for(cuts[a_i], sub[0])
            v = node_for(cuts[b_i], sub[-1])
            if u == v and len(sub) < 3:
                continue
            e = Edge(eid, u, v, sub.copy(), cls, oneway=oneway, level=level,
                     bridge="is_bridge" in fl, tunnel="is_tunnel" in fl or "is_covered" in fl,
                     link="is_link" in fl or r["subclass"] == "link", name=name,
                     subclass=r["subclass"], surface=surf, seg_id=r["id"], speed=speed)
            if e.bridge and e.level == 0:
                e.level = 1
            if e.tunnel and e.level == 0:
                e.level = -1
            edges[eid] = e
            eid += 1
    # make node coordinates consistent with edge endpoints
    for e in edges.values():
        e.pts[0] = nodes[e.u]
        e.pts[-1] = nodes[e.v]
    g = RoadGraph(nodes, edges)
    clip_graph(g, bbox)
    return g


def clip_graph(g: RoadGraph, bbox):
    x0, y0, x1, y1 = bbox
    drop = [k for k, e in g.edges.items()
            if not ((e.pts[:, 0] > x0).any() and (e.pts[:, 0] < x1).any() and
                    (e.pts[:, 1] > y0).any() and (e.pts[:, 1] < y1).any())]
    for k in drop:
        del g.edges[k]
    used = {e.u for e in g.edges.values()} | {e.v for e in g.edges.values()}
    for n in list(g.nodes):
        if n not in used:
            del g.nodes[n]


# ---------------------------------------------------------------------------------------------
def assign_layout(g: RoadGraph):
    """Decide game lanes, widths and sidewalks per edge."""
    for e in g.edges.values():
        c = e.cls
        if c in PATHS:
            e.lanes_f = e.lanes_b = 0
            e.width = 2.4 if c != "steps" else 2.0
            e.sidewalk = 0
            continue
        if c in MINOR:
            e.lanes_f = e.lanes_b = 0
            e.width = {"pedestrian": 6.0, "track": 3.8}.get(c, 4.4)
            if e.subclass in ("driveway", "parking_aisle", "alley"):
                e.width = 3.8
            e.sidewalk = 0
            continue
        if e.oneway:
            n = {"motorway": 3, "trunk": 3, "primary": 2, "secondary": 2, "tertiary": 1}.get(c, 1)
            if e.link:
                n = 1
            e.lanes_f, e.lanes_b = n, 0
        else:
            n = {"motorway": 3, "trunk": 2, "primary": 2}.get(c, 1)
            if e.link:
                n = 1
            e.lanes_f = e.lanes_b = n
        lanes = e.lanes_f + e.lanes_b
        e.width = lanes * LANE_VIS + 2 * GUTTER
        if c in ("motorway", "trunk") or e.link or e.level != 0 or e.tunnel:
            e.sidewalk = 0.0
        elif c in ("primary", "secondary"):
            e.sidewalk = 2.6
        else:
            e.sidewalk = 2.0


# ---------------------------------------------------------------------------------------------
def densify(pts: np.ndarray, step: float) -> np.ndarray:
    out = [pts[0]]
    for a, b in zip(pts[:-1], pts[1:]):
        d = float(np.hypot(*(b - a)))
        n = max(1, int(math.ceil(d / step)))
        for k in range(1, n + 1):
            out.append(a + (b - a) * (k / n))
    return np.array(out)


def simplify_degree2(g: RoadGraph):
    """Merge chains of edges through degree-2 nodes when attributes are identical."""
    adj = g.adjacency()
    keyf = lambda e: (e.cls, e.oneway != 0, e.lanes_f + e.lanes_b, e.level, e.bridge, e.tunnel,
                      e.link, e.name, e.subclass)
    changed = True
    while changed:
        changed = False
        adj = g.adjacency()
        for n, es in list(adj.items()):
            if len(es) != 2 or n not in g.nodes:
                continue
            a, b = g.edges.get(es[0]), g.edges.get(es[1])
            if a is None or b is None or a.id == b.id or keyf(a) != keyf(b):
                continue
            # orient a: ... -> n, b: n -> ...
            pa = a.pts if a.v == n else a.pts[::-1]
            pb = b.pts if b.u == n else b.pts[::-1]
            ua = a.u if a.v == n else a.v
            vb = b.v if b.u == n else b.u
            if ua == vb:
                continue
            # one-way direction must agree
            if a.oneway:
                fa = (a.v == n) == (a.oneway == 1)
                fb = (b.u == n) == (b.oneway == 1)
                if fa != fb:
                    continue
                ow = 1 if fa else -1
            else:
                ow = 0
            a.pts = np.vstack([pa, pb[1:]])
            a.u, a.v = ua, vb
            a.oneway = ow
            if ow == -1:
                a.pts = a.pts[::-1]
                a.u, a.v = a.v, a.u
                a.oneway = 1
            del g.edges[b.id]
            del g.nodes[n]
            changed = True
    # normalise one-way = -1 into forward orientation
    for e in g.edges.values():
        if e.oneway == -1:
            e.pts = e.pts[::-1].copy()
            e.u, e.v = e.v, e.u
            e.oneway = 1
            e.lanes_f, e.lanes_b = e.lanes_b, e.lanes_f


# ---------------------------------------------------------------------------------------------
def _dirs(p):
    d = np.gradient(p, axis=0) if len(p) > 2 else np.repeat(np.diff(p, axis=0), len(p), axis=0)[:len(p)]
    n = np.hypot(d[:, 0], d[:, 1])[:, None]
    return d / np.maximum(n, 1e-9)


def junction_clusters(g: RoadGraph, short=28.0):
    """Union-find over nodes joined by short major edges (junction complexes) and roundabout rings."""
    parent = {n: n for n in g.nodes}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for e in g.edges.values():
        if e.major and e.length < short and e.attrs.get("ring") is None and e.u in parent and e.v in parent:
            ra, rb = find(e.u), find(e.v)
            if ra != rb:
                parent[ra] = rb
    return {n: find(n) for n in g.nodes}


def detect_roundabouts(g: RoadGraph, max_len=420.0):
    """Find small rings of one-way major edges; inflate tiny ones so the ring carriageway fits.
    Marks edges with attrs['ring'] = ring id. Returns list of (center, radius, edge ids)."""
    out_edges = defaultdict(list)
    for e in g.edges.values():
        if e.major and e.oneway == 1:
            out_edges[e.u].append(e)
    rings = []
    seen = set()
    for start in list(out_edges):
        # follow the unique "keep turning" path: DFS limited depth to find a cycle back to start
        stack = [(start, [], 0.0)]
        found = None
        while stack and found is None:
            n, path, L = stack.pop()
            for e in out_edges.get(n, []):
                if e.id in path or L + e.length > max_len:
                    continue
                if e.v == start and len(path) >= 1:
                    found = path + [e.id]
                    break
                if len(path) < 8:
                    stack.append((e.v, path + [e.id], L + e.length))
        if not found:
            continue
        key = frozenset(found)
        if key in seen:
            continue
        pts = np.vstack([g.edges[i].pts for i in found])
        poly = shapely.Polygon(pts)
        if not poly.is_valid or poly.area <= 0:
            continue
        per = sum(g.edges[i].length for i in found)
        circ = 4 * math.pi * poly.area / per ** 2
        if circ < 0.75:
            continue
        seen.add(key)
        c = np.array(poly.centroid.coords[0])
        r0 = float(np.hypot(*(pts - c).T).mean())
        rid = len(rings)
        w = max(g.edges[i].width for i in found)
        r = max(r0, w / 2 + 5.5)
        for i in found:
            e = g.edges[i]
            e.attrs["ring"] = rid
            v = e.pts - c
            rr = np.hypot(v[:, 0], v[:, 1])[:, None]
            e.pts = c + v / np.maximum(rr, 1e-6) * r
        for i in found:
            e = g.edges[i]
            g.nodes[e.u] = e.pts[0].copy()
            g.nodes[e.v] = e.pts[-1].copy()
        rings.append((c, r, found))
    for e in g.edges.values():
        e.pts[0] = g.nodes[e.u]
        e.pts[-1] = g.nodes[e.v]
    return rings


def side_analysis(g: RoadGraph, step=4.0, probe=8.0):
    """Decide per edge whether its left/right side faces another parallel major road
    (-> median/separator, no sidewalk) or open space (-> sidewalk)."""
    maj = [e for e in g.edges.values() if e.major]
    P, D, own = [], [], []
    for k, e in enumerate(maj):
        p = densify(e.pts, step)
        P.append(p)
        D.append(_dirs(p))
        own += [k] * len(p)
    P = np.vstack(P)
    D = np.vstack(D)
    own = np.array(own)
    LV = np.array([maj[k].level for k in own])
    tree = cKDTree(P)
    hw = np.array([maj[k].width / 2 for k in own])
    left_hits = np.zeros(len(maj))
    right_hits = np.zeros(len(maj))
    counts = np.bincount(own, minlength=len(maj)).astype(float)
    pairs = tree.query_pairs(probe + 16, output_type="ndarray")
    i, j = pairs[:, 0], pairs[:, 1]
    m = (own[i] != own[j]) & (LV[i] == LV[j])
    i, j = i[m], j[m]
    cosang = np.abs((D[i] * D[j]).sum(1))
    m = cosang > 0.9
    i, j = i[m], j[m]
    v = P[j] - P[i]
    dist = np.hypot(v[:, 0], v[:, 1])
    m = dist < hw[i] + hw[j] + probe
    i, j, v = i[m], j[m], v[m]
    # side of j relative to i (and vice versa); only count neighbours that are beside, not ahead
    cr_ij = D[i][:, 0] * v[:, 1] - D[i][:, 1] * v[:, 0]
    cr_ji = -(D[j][:, 0] * v[:, 1] - D[j][:, 1] * v[:, 0])
    al_ij = np.abs((D[i] * v).sum(1))
    al_ji = np.abs((D[j] * v).sum(1))
    for a, cr, al in ((i, cr_ij, al_ij), (j, cr_ji, al_ji)):
        ok = (np.abs(cr) > 1.5) & (al < np.abs(cr))
        a, cr = a[ok], cr[ok]
        # unique per vertex: a vertex hit on a side counts once
        lv = np.unique(a[cr > 0])
        rv = np.unique(a[cr < 0])
        np.add.at(left_hits, own[lv], 1)
        np.add.at(right_hits, own[rv], 1)
    for k, e in enumerate(maj):
        fl = left_hits[k] / counts[k]
        fr = right_hits[k] / counts[k]
        e.attrs["inner_left"] = fl > 0.45
        e.attrs["inner_right"] = fr > 0.45
        sw = e.sidewalk
        e.attrs["sw_left"] = 0.0 if e.attrs["inner_left"] else sw
        e.attrs["sw_right"] = 0.0 if e.attrs["inner_right"] else sw


def displace(g: RoadGraph, step=4.0, iters=150, gap=1.0, verbose=True):
    """Push apart major road edges whose game widths overlap (same level only).

    Each vertex has a left and right extent (half width + sidewalk on that side). The required
    distance between two vertices of different edges uses the extents of the sides facing each
    other. Pairs near a common junction cluster are ignored (the junction polygon merges them).
    The displacement field (not the positions) is smoothed along each polyline, so curve shapes
    are preserved.
    """
    clusters = junction_clusters(g)
    maj = [e for e in g.edges.values() if e.major]
    for e in maj:
        e.pts = densify(e.pts, step)
    owner, P, HL, HR, LV, RING = [], [], [], [], [], []
    for k, e in enumerate(maj):
        n = len(e.pts)
        owner += [k] * n
        P.append(e.pts)
        HL += [e.width / 2 + e.attrs.get("sw_left", e.sidewalk)] * n
        HR += [e.width / 2 + e.attrs.get("sw_right", e.sidewalk)] * n
        LV += [e.level] * n
        RING += [e.attrs.get("ring", -1)] * n
    owner = np.array(owner)
    P = np.vstack(P).astype(np.float64)
    P0 = P.copy()
    HL, HR, LV, RING = np.array(HL), np.array(HR), np.array(LV), np.array(RING)
    lens = np.array([len(e.pts) for e in maj])
    starts = np.r_[0, np.cumsum(lens)[:-1]]
    ends = starts + lens - 1
    node_of = np.full(len(P), -1)
    for k, e in enumerate(maj):
        node_of[starts[k]] = e.u
        node_of[ends[k]] = e.v
    arc = np.zeros(len(P))
    for k in range(len(maj)):
        s, t = starts[k], ends[k]
        arc[s:t + 1] = np.r_[0, np.cumsum(np.hypot(*np.diff(P[s:t + 1], axis=0).T))]
    length = arc[ends[owner]]
    cu = np.array([clusters.get(maj[k].u, -1) for k in owner])
    cv = np.array([clusters.get(maj[k].v, -1) for k in owner])
    hmax = float(max(HL.max(), HR.max()))
    maxr = 2 * hmax + gap
    inner = np.ones(len(P), bool)
    inner[starts] = False
    inner[ends] = False
    idx_in = np.nonzero(inner)[0]
    for it in range(iters):
        Dir = np.zeros_like(P)
        for k in range(len(maj)):
            s, t = starts[k], ends[k] + 1
            Dir[s:t] = _dirs(P[s:t])
        tree = cKDTree(P)
        pairs = tree.query_pairs(maxr, output_type="ndarray")
        i, j = pairs[:, 0], pairs[:, 1]
        m = (owner[i] != owner[j]) & (LV[i] == LV[j]) & ~((RING[i] >= 0) & (RING[i] == RING[j]))
        i, j = i[m], j[m]
        dv = P[j] - P[i]
        dist = np.hypot(dv[:, 0], dv[:, 1])
        cr_i = Dir[i][:, 0] * dv[:, 1] - Dir[i][:, 1] * dv[:, 0]   # >0: j left of i
        cr_j = -(Dir[j][:, 0] * dv[:, 1] - Dir[j][:, 1] * dv[:, 0])  # >0: i left of j
        need = np.where(cr_i > 0, HL[i], HR[i]) + np.where(cr_j > 0, HL[j], HR[j]) + gap
        m = dist < need
        i, j, need, dv, dist = i[m], j[m], need[m], dv[m], dist[m]
        # skip pairs near a junction cluster shared by both edges
        lim = need * 1.5 + 8.0
        di_u, di_v = arc[i], length[i] - arc[i]
        dj_u, dj_v = arc[j], length[j] - arc[j]
        skip = np.zeros(len(i), bool)
        for ci, di in ((cu[i], di_u), (cv[i], di_v)):
            for cj, dj in ((cu[j], dj_u), (cv[j], dj_v)):
                skip |= (ci == cj) & (ci >= 0) & (di < lim) & (dj < lim)
        i, j, need, dv, dist = i[~skip], j[~skip], need[~skip], dv[~skip], dist[~skip]
        if verbose and (it % 25 == 0 or it == iters - 1):
            print(f"  displace iter {it}: {len(i)} conflicts, max overlap {float((need - dist).max()) if len(i) else 0:.2f}")
        g.attrs_conflicts = np.c_[(P[i] + P[j]) / 2, need - dist] if len(i) else np.zeros((0, 3))
        if len(i) == 0:
            break
        dist = np.maximum(dist, 1e-3)
        push = ((need - dist) / dist * 0.25)[:, None] * dv
        F = np.zeros_like(P)
        np.add.at(F, i, -push)
        np.add.at(F, j, push)
        mag = np.hypot(F[:, 0], F[:, 1])
        F *= (np.minimum(mag, 1.5) / np.maximum(mag, 1e-9))[:, None]
        D = (P - P0) + F
        nd = node_of >= 0
        uniq, inv = np.unique(node_of[nd], return_inverse=True)
        acc = np.zeros((len(uniq), 2))
        cnt = np.zeros(len(uniq))
        np.add.at(acc, inv, D[nd])
        np.add.at(cnt, inv, 1)
        D[nd] = acc[inv] / cnt[inv, None]
        for _ in range(2):
            D2 = D.copy()
            D2[idx_in] = 0.5 * D[idx_in] + 0.25 * (D[idx_in - 1] + D[idx_in + 1])
            D = D2
        P = P0 + D * 0.995
    for k, e in enumerate(maj):
        e.pts = P[starts[k]:ends[k] + 1].copy()
    moved = defaultdict(list)
    for e in maj:
        moved[e.u].append(e.pts[0])
        moved[e.v].append(e.pts[-1])
    minor_at = defaultdict(list)
    for e in g.edges.values():
        if not e.major:
            minor_at[e.u].append(e)
            minor_at[e.v].append(e)
    for n, lst in moved.items():
        old = g.nodes[n].copy()
        new = np.mean(lst, axis=0)
        g.nodes[n] = new
        delta = new - old
        dl = float(np.hypot(*delta))
        if dl > 1e-6:
            for e in minor_at.get(n, []):
                p = e.pts.copy()
                d = np.r_[0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]
                if e.v == n and e.u != n:
                    d = d[-1] - d
                w = np.clip(1 - d / max(30.0, 3 * dl), 0, 1)
                e.pts = p + w[:, None] * delta
    for e in g.edges.values():
        e.pts[0] = g.nodes[e.u]
        e.pts[-1] = g.nodes[e.v]
    return P - P0
