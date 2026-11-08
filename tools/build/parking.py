"""Parked cars: car generators (IPL 'cars') in parking lots, along parking aisles and in the
courtyard service roads / living streets (no AI traffic runs there, see build_paths: only major
roads get car paths, so parked cars never block traffic).

Engine facts (gta-reversed CarGenerator.cpp / TheCarGenerators.cpp / FileLoader.cpp):
  * 500 generators at most (CTheCarGenerators::CarGeneratorArray); binary stream IPLs create
    theirs when the cell loads and remove them when it unloads (CIplStore::RemoveIpl) -> budget
    per 2 x 2 block of cells;
  * file angle in radians, the car's forward (+y) axis is rotated by it (0 = north);
  * a car appears when the player is 140..160 units away; the ground is found with a vertical
    line from z + 1 downwards -> z = ground + 1;
  * flag bit 1 = ignore the parked-car population limit (5 by day / 10 at night), set on part
    of the generators so Baku's courtyards look as full as they are.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import pyarrow.parquet as pq
import shapely
from scipy.spatial import cKDTree

from tools.build import ground
from tools.geo.proj import WORLD
from tools.gta.ipl import CarGen

CELL = 500.0
NC = int(2 * WORLD / CELL)
PER_CELL = 100
PER_BLOCK = 420
CAR_L, CAR_W = 4.9, 2.2


def _rect(c, fwd, L=CAR_L, Wd=CAR_W):
    r = np.array([fwd[1], -fwd[0]])
    hl, hw = L / 2, Wd / 2
    return shapely.Polygon([c + fwd * hl + r * hw, c + fwd * hl - r * hw, c - fwd * hl - r * hw, c - fwd * hl + r * hw])


def _obb_axis(poly):
    mrr = poly.minimum_rotated_rectangle
    if mrr.geom_type != "Polygon":
        return np.array([1.0, 0.0]), 0.0, 0.0
    c = np.asarray(mrr.exterior.coords)[:4]
    e1, e2 = c[1] - c[0], c[2] - c[1]
    l1, l2 = np.hypot(*e1), np.hypot(*e2)
    return (e1 / l1, l1, l2) if l1 >= l2 else (e2 / l2, l2, l1)


def candidates(W, blds, props, exclude=None, seed=3):
    rng = np.random.default_rng(seed)
    road_tree = shapely.STRtree([p for p in W.road_parts if not p.is_empty])          # major roads only
    bld_tree = shapely.STRtree([b["poly"] for b in blds if b.get("poly") is not None])
    sea, lakes = W.water_polys
    water = [g for g in getattr(sea, "geoms", [sea]) if not g.is_empty] + [p for p, _ in lakes]
    water_tree = shapely.STRtree(water)
    pxy = np.array([[p["x"], p["y"]] for p in props]) if props else np.zeros((0, 2))
    prop_tree = cKDTree(pxy) if len(pxy) else None
    out = []          # (x, y, heading, prio)

    def free(c, fwd):
        rect = _rect(c, fwd)
        if road_tree.query(rect, predicate="intersects").size or bld_tree.query(rect, predicate="intersects").size:
            return False
        if water_tree.query(rect, predicate="intersects").size:
            return False
        if exclude is not None and rect.intersects(exclude):
            return False
        if prop_tree is not None and prop_tree.query_ball_point(c, 2.6):
            return False
        return abs(c[0]) < WORLD - 8 and abs(c[1]) < WORLD - 8

    # 1. parking lots: perpendicular bays in rows along the long axis
    lots = []
    for r in pq.read_table("data/raw/infrastructure.parquet", columns=["class", "geometry"]).to_pylist():
        if r["class"] in ("parking", "parking_space"):
            g = shapely.from_wkb(r["geometry"])
            if g.geom_type in ("Polygon", "MultiPolygon"):
                g = ground._to_game(g).buffer(0)
                if g.area > 40:
                    lots.append(g)
    for lot in lots:
        for poly in getattr(lot, "geoms", [lot]):
            ax, L1, L2 = _obb_axis(poly)
            nrm = np.array([-ax[1], ax[0]])
            c0 = np.array(poly.centroid.coords[0])
            # rows of bays: car axis along nrm, rows every 6.6 units (bay 5 + half aisle)
            for v in np.arange(-L2 / 2 + 2.6, L2 / 2 - 2.4, 6.6 if L2 > 12 else 100):
                for u in np.arange(-L1 / 2 + 1.5, L1 / 2 - 1.3, 2.8):
                    c = c0 + ax * u + nrm * v
                    if not poly.contains(shapely.Point(*c)):
                        continue
                    fwd = nrm if rng.random() < 0.5 else -nrm
                    if free(c, fwd):
                        out.append((c[0], c[1], math.atan2(-fwd[0], fwd[1]), 0))
    # 2. parking aisles (OSM service=parking_aisle): bays on both sides
    # 3. courtyard service roads / living streets: parallel parking along the edges
    for e in W.g.edges.values():
        if e.major or e.cls not in ("service", "living_street"):
            continue
        p = e.pts
        if len(p) < 2 or e.length < 10:
            continue
        aisle = (e.subclass or "") in ("parking_aisle",)
        step = 2.9 if aisle else 6.2
        s_all = np.r_[0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]
        for s in np.arange(4.0, s_all[-1] - 4.0, step):
            i = min(int(np.searchsorted(s_all, s, side="right") - 1), len(p) - 2)
            f = (s - s_all[i]) / max(s_all[i + 1] - s_all[i], 1e-9)
            c = p[i] + (p[i + 1] - p[i]) * f
            t = p[i + 1] - p[i]
            t = t / max(np.hypot(*t), 1e-9)
            n = np.array([-t[1], t[0]])
            for side in (1, -1):
                if aisle:
                    q = c + n * side * (e.width / 2 + 2.7)
                    fwd = n * side if rng.random() < 0.5 else -n * side
                else:
                    q = c + n * side * (e.width / 2 + 1.25)
                    fwd = t if rng.random() < 0.5 else -t
                if free(q, fwd):
                    out.append((q[0], q[1], math.atan2(-fwd[0], fwd[1]), 1 if aisle else 2))
    return out


def select(cands, W, seed=4, occupancy=0.5):
    """Thin to an occupancy and to the generator budgets (per cell and per 2x2 block)."""
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(cands))
    taken = []
    grid = defaultdict(list)
    cell_n = defaultdict(int)
    blk_n = defaultdict(int)
    for k in order:
        x, y, h, prio = cands[k]
        if rng.random() > occupancy:
            continue
        g = (int(x // 6), int(y // 6))
        if any((x - a) ** 2 + (y - b) ** 2 < 2.3 ** 2 for i in (-1, 0, 1) for j in (-1, 0, 1)
               for a, b in grid[(g[0] + i, g[1] + j)]):
            continue
        cx = min(NC - 1, max(0, int((x + WORLD) // CELL)))
        cy = min(NC - 1, max(0, int((y + WORLD) // CELL)))
        if cell_n[(cx, cy)] >= PER_CELL:
            continue
        blocks = [(bx, by) for bx in (cx - 1, cx) for by in (cy - 1, cy)]
        if any(blk_n[b] >= PER_BLOCK for b in blocks):
            continue
        cell_n[(cx, cy)] += 1
        for b in blocks:
            blk_n[b] += 1
        grid[g].append((x, y))
        z = float(ground.H_at(W, x, y)[0]) + 1.0
        flags = 2 if rng.random() < 0.5 else 0          # ignore the parked-car population limit
        lock = 30 if rng.random() < 0.5 else 0
        taken.append(CarGen(float(x), float(y), z, float(h), -1, -1, -1, flags, 0, lock, 0, 0))
    return taken


def place_all(W, blds, props, exclude=None, verbose=True):
    c = candidates(W, blds, props, exclude)
    cars = select(c, W)
    if verbose:
        print(f"parked cars: {len(cars)} generators from {len(c)} candidate bays")
    return cars


if __name__ == "__main__":
    import json
    import os
    import pickle
    W = ground.prepare()
    blds, _ = pickle.load(open("data/cache/buildings.pkl", "rb"))
    props = pickle.load(open("data/cache/props.pkl", "rb"))
    exclude = None
    lmj = "build/out/landmarks/landmarks.json"
    if os.path.exists(lmj):
        exclude = shapely.union_all([shapely.from_wkt(w).buffer(2.0) for w in json.load(open(lmj))["footprints"].values()])
    cars = place_all(W, blds, props, exclude)
    pickle.dump(cars, open("data/cache/parked_cars.pkl", "wb"))
