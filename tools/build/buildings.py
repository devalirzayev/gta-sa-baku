"""Place original SA building models on Baku building footprints.

Every Overture footprint (scaled to game units) is reduced to one or more oriented rectangles;
each rectangle gets the SA candidate model (data/sa/building_candidates.csv) whose collision
footprint and height fit best (long slabs get a row of models).  Placements never overlap roads,
sidewalks, water or already placed models (small tolerance).  Large footprints are processed first.

Output: list of dicts {id, name, x, y, z, heading, lod_id, lod_name, style}.
"""
from __future__ import annotations

import math
import random
from collections import Counter

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import shapely
from shapely.ops import unary_union

from tools.build import ground
from tools.geo.proj import lonlat_to_game, WORLD, SCALE

STYLE_POOLS = {
    "house": ["detached_house", "rowhouse/terrace"],
    "apt_low": ["low_apartment"],
    "apt_mid": ["mid_apartment", "low_apartment"],
    "tower": ["tower", "mid_apartment"],
    "commercial": ["shop/retail", "office"],
    "office": ["office", "tower", "shop/retail"],
    "industrial": ["industrial/warehouse", "garage"],
    "garage": ["garage", "industrial/warehouse"],
    "civic": ["office", "low_apartment", "misc"],
}
FLOOR_H = 3.0


def load_candidates(path="data/sa/building_candidates.csv"):
    b = pd.read_csv(path)
    b = b[b.ground_risk != "high"].copy()
    b = b[(b.timed == 0) | b.timed.isna()]
    b["ml"] = b[["footprint_x", "footprint_y"]].max(axis=1)
    b["ms"] = b[["footprint_x", "footprint_y"]].min(axis=1)
    b["long_x"] = b.footprint_x >= b.footprint_y
    b["ox"] = (b.aabb_min_x + b.aabb_max_x) / 2
    b["oy"] = (b.aabb_min_y + b.aabb_max_y) / 2
    b = b[(b.ms > 3) & (b.height > 2.5)]
    b = b.dropna(subset=["aabb_min_x", "aabb_min_y", "aabb_min_z", "aabb_max_x", "aabb_max_y", "aabb_max_z"])
    return b.reset_index(drop=True)


def _obb(poly):
    """Oriented bounding rectangle: center, unit long axis, length, width."""
    r = poly.minimum_rotated_rectangle
    if r.geom_type != "Polygon":
        c = np.array(poly.centroid.coords[0])
        return c, np.array([1.0, 0.0]), 1.0, 1.0
    p = np.asarray(r.exterior.coords)[:4]
    e1, e2 = p[1] - p[0], p[2] - p[1]
    l1, l2 = np.hypot(*e1), np.hypot(*e2)
    c = p.mean(axis=0)
    if l1 >= l2:
        return c, e1 / max(l1, 1e-9), l1, l2
    return c, e2 / max(l2, 1e-9), l2, l1


def _rect(c, u, L, Wd):
    v = np.array([-u[1], u[0]])
    h, w = L / 2, Wd / 2
    pts = [c + u * h + v * w, c - u * h + v * w, c - u * h - v * w, c + u * h - v * w]
    return shapely.Polygon(pts)


def classify(r, area_real, aspect, landuse):
    floors = r.get("num_floors")
    h = r.get("height")
    cls = (r.get("class") or "") + "|" + (r.get("subtype") or "")
    if floors is None or (isinstance(floors, float) and math.isnan(floors)):
        floors = None
    if floors is None and h and not (isinstance(h, float) and math.isnan(h)):
        floors = max(1, int(round(h / FLOOR_H)))
    lu = landuse or ""
    if any(k in cls for k in ("industrial", "manufacture", "warehouse", "works")) or lu in ("industrial", "works"):
        style = "industrial"
        floors = floors or 2
    elif any(k in cls for k in ("garage",)) or lu == "garages":
        style = "garage"
        floors = floors or 1
    elif any(k in cls for k in ("retail", "commercial", "supermarket", "kiosk", "shop")) or lu in ("retail", "commercial"):
        style = "commercial"
        floors = floors or 2
    elif any(k in cls for k in ("office", "hotel")):
        style = "office"
        floors = floors or 8
    elif any(k in cls for k in ("school", "hospital", "university", "college", "kindergarten", "civic", "education", "medical")):
        style = "civic"
        floors = floors or 3
    else:
        if floors is None:
            if area_real < 160:
                floors = 1 if area_real < 90 else 2
            elif area_real < 450:
                floors = 3 if aspect < 2.2 else 5
            elif aspect > 2.3:
                floors = 9 if area_real > 900 else 5
            else:
                floors = 5 if area_real < 2500 else 3
        if floors <= 2:
            style = "house"
        elif floors <= 5:
            style = "apt_low"
        elif floors <= 11:
            style = "apt_mid"
        else:
            style = "tower"
    return style, int(floors)


class Placer:
    def __init__(self, W, cand, seed=7):
        self.W = W
        self.c = cand
        self.rng = random.Random(seed)
        self.placed = []          # shapely polygons of placed model footprints
        self.placed_tree = None
        self.dirty = 0
        self.out = []
        self.usage = Counter()
        self.recent = {}
        blockers = list(W.road_parts) + list(W.sw_parts) + list(W.trench_parts or [])
        sea, lakes = W.water_polys
        blockers += list(getattr(sea, "geoms", [sea])) + [p for p, _ in lakes]
        # elevated road decks are not blockers for buildings below? keep them as blockers
        for e in W.g.edges.values():
            if e.major and np.abs(e.lift).max() > 0.3:
                blockers.append(shapely.LineString(e.pts).buffer(e.width / 2 + 1.0))
        self.block_parts = np.array([b for b in blockers if b is not None and not b.is_empty], dtype=object)
        self.block_tree = shapely.STRtree(self.block_parts)
        self.minor = np.array([p for p in W.minor_parts if not p.is_empty], dtype=object)
        self.minor_tree = shapely.STRtree(self.minor)
        self.pools = {k: self.c[self.c["style"].isin(v)].reset_index(drop=True) for k, v in STYLE_POOLS.items()}

    # --------------------------------------------------------------
    def blocked_fraction(self, poly):
        a = poly.area
        tot = 0.0
        for idx in self.block_tree.query(poly, predicate="intersects"):
            tot += poly.intersection(self.block_parts[idx]).area
        for idx in self.minor_tree.query(poly, predicate="intersects"):
            tot += 0.5 * poly.intersection(self.minor[idx]).area
        if self.placed:
            if self.placed_tree is None or self.dirty > 200:
                self.placed_tree = shapely.STRtree(self.placed)
                self.dirty = 0
                self._ptree_n = len(self.placed)
            for idx in self.placed_tree.query(poly, predicate="intersects"):
                tot += poly.intersection(self.placed[idx]).area
            for q in self.placed[self._ptree_n:]:
                if q.intersects(poly):
                    tot += poly.intersection(q).area
        return tot / max(a, 1e-6)

    def choose(self, style, L, Wd, H, big_ok=False):
        pool = self.pools[style]
        if len(pool) == 0:
            return None
        ml, ms, mh = pool.ml.values, pool.ms.values, pool.height.values
        best = None
        for k in (1, 2, 3, 4, 5, 6):
            Lk = L / k
            if Lk < 4:
                break
            cost = (2.2 * np.maximum(0, ms - Wd * 1.12 - 1.0) / max(Wd, 1)
                    + 1.8 * np.maximum(0, ml - Lk * 1.10 - 1.0) / max(Lk, 1)
                    + 0.7 * np.maximum(0, Wd * 0.75 - ms) / max(Wd, 1)
                    + 0.7 * np.maximum(0, Lk * 0.80 - ml) / max(Lk, 1)
                    + 0.6 * np.abs(np.log(np.maximum(mh, 1) / max(H, 3)))
                    + 0.08 * (k - 1))
            risk = pool.ground_risk.values
            cost = cost + np.where(risk == "medium", 0.25 if big_ok else 0.8, 0.0)
            use = np.array([self.usage.get(i, 0) for i in pool.id.values])
            cost = cost + 0.03 * np.sqrt(use) + np.array([self.rng.random() * 0.12 for _ in range(len(pool))])
            i = int(np.argmin(cost))
            if best is None or cost[i] < best[0]:
                best = (float(cost[i]), k, pool.iloc[i])
        return best

    def place_rect(self, c, u, L, Wd, H, style, big_ok=False):
        ch = self.choose(style, L, Wd, H, big_ok)
        if ch is None:
            return 0
        cost, k, m = ch
        if cost > 2.6:
            return 0
        n = 0
        Lk = L / k
        for i in range(k):
            ci = c + u * ((i + 0.5) * Lk - L / 2)
            if self._place_model(m, ci, u):
                n += 1
        return n

    def _place_model(self, m, c, u):
        theta = math.atan2(u[1], u[0])
        if not m.long_x:
            theta -= math.pi / 2
        if self.rng.random() < 0.5:
            theta += math.pi
        ct, st = math.cos(theta), math.sin(theta)
        # model footprint rectangle in world
        fx, fy = m.footprint_x, m.footprint_y
        R = np.array([[ct, -st], [st, ct]])
        corners = np.array([[m.aabb_min_x, m.aabb_min_y], [m.aabb_max_x, m.aabb_min_y],
                            [m.aabb_max_x, m.aabb_max_y], [m.aabb_min_x, m.aabb_max_y]])
        off = np.array([m.ox, m.oy])
        pos = c - R @ off
        wc = (R @ corners.T).T + pos
        poly = shapely.Polygon(wc)
        if not poly.is_valid or poly.area < 1:
            return False
        if abs(pos[0]) > WORLD - 20 or abs(pos[1]) > WORLD - 20:
            return False
        frac = self.blocked_fraction(poly.buffer(-0.4) if poly.area > 30 else poly)
        if frac > 0.06:
            return False
        zs = ground.H_at(self.W, wc[:, 0], wc[:, 1])
        zc = ground.H_at(self.W, np.array([c[0]]), np.array([c[1]]))[0]
        z = float(min(zs.min(), zc)) + ground.KERB - m.aabb_min_z - 0.12
        lod_id = int(m.lod_model_id) if (not pd.isna(m.lod_model_id) and m.lod_reusable == 1) else None
        self.out.append({"id": int(m.id), "name": m["name"], "x": float(pos[0]), "y": float(pos[1]), "z": z,
                         "heading": theta, "lod_id": lod_id,
                         "lod_name": m.lod_model_name if lod_id else None, "style": m["style"],
                         "height": float(m.height), "area": float(poly.area), "poly": poly})
        self.placed.append(poly)
        self.dirty += 1
        self.usage[int(m.id)] += 1
        return True


def load_footprints():
    t = pq.read_table("data/raw/building.parquet",
                      columns=["id", "geometry", "height", "num_floors", "class", "subtype", "is_underground"]).to_pylist()
    lu = pq.read_table("data/raw/land_use.parquet", columns=["geometry", "class"]).to_pylist()
    lu_polys, lu_cls = [], []
    for r in lu:
        g = shapely.from_wkb(r["geometry"])
        if g.geom_type in ("Polygon", "MultiPolygon"):
            lu_polys.append(ground._to_game(g))
            lu_cls.append(r["class"])
    lu_tree = shapely.STRtree(lu_polys)
    geoms = shapely.from_wkb([r["geometry"] for r in t])
    gg = shapely.transform(geoms, lambda c: np.c_[lonlat_to_game(c[:, 0], c[:, 1])])
    out = []
    world = shapely.box(-WORLD + 15, -WORLD + 15, WORLD - 15, WORLD - 15)
    cents = shapely.centroid(gg)
    hits = lu_tree.query(cents, predicate="within")
    lu_at = {}
    for fi, li in zip(*hits):
        prev = lu_at.get(fi)
        if prev is None or lu_polys[li].area < lu_polys[prev].area:
            lu_at[fi] = li
    for i, (r, g) in enumerate(zip(t, gg)):
        if r.get("is_underground"):
            continue
        if g is None or g.is_empty or not g.intersects(world):
            continue
        if g.geom_type == "MultiPolygon":
            g = max(g.geoms, key=lambda p: p.area)
        a = g.area
        if a < 9.0:
            continue
        li = lu_at.get(i)
        out.append((r, g, lu_cls[li] if li is not None else None))
    return out


def place_all(W, verbose=True):
    cand = load_candidates()
    P = Placer(W, cand)
    fps = load_footprints()
    items = []
    for r, g, lu in fps:
        c, u, L, Wd = _obb(g)
        area_real = g.area / SCALE ** 2
        aspect = L / max(Wd, 0.5)
        style, floors = classify(r, area_real, aspect, lu)
        fill = g.area / max(L * Wd, 1e-6)
        items.append((g.area, g, c, u, L, Wd, style, floors, fill))
    items.sort(key=lambda t: -t[0])
    if verbose:
        print("footprints", len(items), Counter(i[6] for i in items).most_common())
    n_ok = 0
    for k, (a, g, c, u, L, Wd, style, floors, fill) in enumerate(items):
        H = floors * FLOOR_H
        big = a > 600
        rects = [(c, u, L, Wd)]
        if fill < 0.62 and a > 250:
            # split along the long axis into two halves and keep the parts that are filled
            rects = []
            for sgn in (-1, 1):
                cc = c + u * sgn * L / 4
                half = _rect(cc, u, L / 2, Wd).intersection(g)
                if half.is_empty or half.area < 0.35 * (L / 2 * Wd):
                    continue
                c2, u2, L2, W2 = _obb(half)
                rects.append((c2, u2, L2, W2))
        for (cc, uu, LL, WW) in rects:
            if style == "house":
                # SA houses are bigger than Baku houses: use a minimum target size
                LL, WW = max(LL, 11.0), max(WW, 9.0)
            n_ok += P.place_rect(cc, uu, LL, WW, H, style, big_ok=big)
        if verbose and k % 10000 == 0:
            print(f"  {k}/{len(items)} placed {len(P.out)}")
    if verbose:
        print("placed", len(P.out), "instances;", len(P.usage), "distinct models")
    return P.out, P.usage
