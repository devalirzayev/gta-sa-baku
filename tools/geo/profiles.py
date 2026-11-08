"""Vertical road profiles: node heights, per-edge height profiles, bridge/tunnel lift with ramps.

Every major/minor edge gets `e.z` (ground height of the centerline per vertex) and `e.lift`
(height of the road surface above/below that ground; >0 elevated, <0 depressed).
Surface height = z + lift.
"""
from __future__ import annotations

from collections import defaultdict, deque

import numpy as np
from scipy import ndimage

from .roads import RoadGraph, densify

CLEAR_LEVEL = 7.0        # deck height per level above ground (surface), game units
MAX_GRADE = 0.10         # max road grade for ramps / profiles
RAMP_GRADE = 0.065


def arclen(p):
    return np.r_[0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]


def smooth1d(v, s, sigma):
    """Gaussian smoothing of samples v at arc positions s (approx. uniform spacing)."""
    if len(v) < 3:
        return v.copy()
    step = max((s[-1] - s[0]) / (len(s) - 1), 1e-6)
    return ndimage.gaussian_filter1d(v, sigma / step, mode="nearest")


def compute_profiles(g: RoadGraph, T, step=3.0):
    adj = defaultdict(list)
    for e in g.edges.values():
        e.pts = densify(e.pts, step)
        adj[e.u].append(e.id)
        adj[e.v].append(e.id)
    # node ground heights: smoothed terrain, junction clusters share height
    hz = {n: float(T.sample(*p)[0]) for n, p in g.nodes.items()}
    # relax node heights along edges so neighbouring nodes don't exceed MAX_GRADE
    for _ in range(30):
        changed = 0
        for e in g.edges.values():
            if e.level != 0 and not (e.bridge or e.tunnel):
                pass
            L = max(e.length, 1.0)
            a, b = hz[e.u], hz[e.v]
            lim = MAX_GRADE * L
            if abs(a - b) > lim:
                m = (a + b) / 2
                hz[e.u] = m + np.sign(a - b) * lim / 2
                hz[e.v] = m - np.sign(a - b) * lim / 2
                changed += 1
        if not changed:
            break
    g.node_z = hz
    # edge ground profiles
    for e in g.edges.values():
        s = arclen(e.pts)
        raw = T.sample(e.pts[:, 0], e.pts[:, 1])
        sm = smooth1d(raw, s, 18.0)
        # pin ends to node heights with a linear correction
        t = s / max(s[-1], 1e-6)
        corr = (hz[e.u] - sm[0]) * (1 - t) + (hz[e.v] - sm[-1]) * t
        e.z = sm + corr
        e.lift = np.zeros(len(e.pts))
    _lifts(g, adj)


def _lifts(g: RoadGraph, adj):
    """Assign lift for bridges (+) and tunnels (-), then propagate ramps into neighbours."""
    target = {}
    for e in g.edges.values():
        lv = e.level
        if e.bridge and lv <= 0:
            lv = 1
        if e.tunnel and lv >= 0:
            lv = -1
        target[e.id] = lv * CLEAR_LEVEL if lv != 0 else 0.0
    # node lift = max |target| of incident grade-separated edges, only if node is not attached
    # to any level-0 edge that is part of a junction with other ground roads
    node_lift = {}
    for n, es in adj.items():
        vals = [target[i] for i in es if target[i] != 0]
        ground = [i for i in es if target[i] == 0]
        if vals and len(ground) <= 1:
            node_lift[n] = max(vals, key=abs)
        elif vals:
            node_lift[n] = 0.0  # bridge meets a junction: ramp must be inside the bridge edge
        else:
            node_lift[n] = None
    # propagate ramp lifts through chains of ground edges (degree-2 continuation only)
    ramp = {}
    q = deque()
    for n, v in node_lift.items():
        if v:
            q.append((n, v))
    seen = set()
    while q:
        n, v = q.popleft()
        if (n, round(v, 2)) in seen:
            continue
        seen.add((n, round(v, 2)))
        es = adj[n]
        ground = [i for i in es if target[i] == 0]
        for i in ground:
            e = g.edges[i]
            L = e.length
            other = e.v if e.u == n else e.u
            remain = abs(v) - RAMP_GRADE * L
            ramp.setdefault(i, []).append((n, v))
            if remain > 0.05 and len(adj[other]) == 2:
                q.append((other, np.sign(v) * remain))
    for e in g.edges.values():
        s = arclen(e.pts)
        L = max(s[-1], 1e-6)
        if target[e.id] != 0:
            tv = target[e.id]
            la = node_lift.get(e.u)
            lb = node_lift.get(e.v)
            la = tv if la is None else la
            lb = tv if lb is None else lb
            # ramp within the bridge edge where an end is pinned to 0
            lift = np.full(len(s), tv)
            if abs(la) < abs(tv):
                lift = np.minimum(np.abs(lift), np.abs(la) + RAMP_GRADE * 1.4 * s) * np.sign(tv)
            if abs(lb) < abs(tv):
                lift = np.minimum(np.abs(lift), np.abs(lb) + RAMP_GRADE * 1.4 * (L - s)) * np.sign(tv)
            e.lift = lift
        elif e.id in ramp:
            lift = np.zeros(len(s))
            for n, v in ramp[e.id]:
                d = s if n == e.u else L - s
                cand = np.sign(v) * np.maximum(abs(v) - RAMP_GRADE * d, 0)
                lift = np.where(np.abs(cand) > np.abs(lift), cand, lift)
            e.lift = lift
