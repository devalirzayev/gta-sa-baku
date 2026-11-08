"""Street furniture and vegetation placed with ORIGINAL San Andreas models.

    props = place_all(W, blds)      # list of dict(id, name, x, y, z, heading, kind, dyn)

Model axes were measured from the vanilla placements (vanilla IPLs + the vanilla car navis, see
docs/notes/props.md):
  * single-arm lamp posts (mlamppost, lamppost1, lamppost3) reach over the road along local -x;
    lamppost2 has two arms along +-x (medians);
  * traffic-light masts (CJ_TRAFFIC_LIGHT3/4/5, trafficlight1) have their forward (+y) axis along
    the traffic they control, the lamps face -y (the approaching cars) and the mast arm runs
    along -x, i.e. the pole stands on the right kerb.  The engine derives the displayed phase from
    that heading (60-150 / 240-330 deg -> group 1); build_paths uses the same rule for the AI;
  * the bus shelter (bustopm) opens towards -x.

Dynamic models (object.dat) become CDummyObjects: 2500-slot dummy pool, double-link sector nodes
(3200-slot pool shared with all peds/cars/objects), and they turn into CObjects (350-slot pool)
within 80 units of the player.  The budgets below keep them at vanilla densities (vanilla: max
1537 dynamic instances in a 1 km block of loaded stream IPLs, max 144 within 100 units).
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import shapely
from shapely.ops import unary_union

from tools.build import ground
from tools.geo import profiles
from tools.geo.proj import lonlat_to_game, WORLD
from tools.geo.roadmesh import polyline_normals

RNG_SEED = 11
CELL = 500.0                      # stream IPL cell (package.py)
NC = int(2 * WORLD / CELL)

# ---------------------------------------------------------------- models (kept vanilla IDEs only)
TL_MAST = 1350                    # CJ_TRAFFIC_LIGHT4: 8 m pole, 3.6 m mast arm (LA)
TL_POLE = 1352                    # CJ_TRAFFIC_LIGHT3: pole-mounted head (left side of wide approaches)
LAMP_MAIN = 1294                  # mlamppost
LAMP_SMALL = 1226                 # lamppost3
LAMP_ALT = 1297                   # lamppost1
LAMP_MEDIAN = 1290                # lamppost2, double arm
LAMP_DECOR = (1232, 1231)         # Streetlamp1 / Streetlamp2 (promenades, parks)
BUS_SHELTER = 1257                # bustopm
BENCH = 1280                      # parkbench1
BENCH_STONE = 1368                # CJ_BLOCKER_BENCH (static)
WASTEBIN = 1347                   # CJ_WASTEBIN (static)

TREES = {
    "street": [(767, 3), (768, 3), (669, 3), (673, 2), (769, 2), (618, 1), (763, 1)],
    "park": [(700, 3), (691, 2), (715, 3), (772, 2), (777, 1), (726, 1), (617, 1), (768, 2),
             (766, 2), (764, 1), (770, 2), (661, 1), (660, 1)],
    "forest": [(661, 3), (660, 3), (657, 2), (770, 3), (655, 1), (766, 1), (669, 1), (700, 1)],
    "cemetery": [(766, 4), (764, 3), (765, 3), (770, 1)],
    "court": [(673, 3), (767, 3), (768, 2), (669, 2), (769, 2), (763, 1), (618, 1), (774, 1), (765, 1)],
    "boulevard": [(711, 3), (718, 3), (641, 2), (766, 2), (661, 1), (700, 1)],
}

# lamp spacing (units along the road) per class; dual carriageways put lamppost2 in the median
LAMP_SPACING = {"motorway": 32, "trunk": 28, "primary": 26, "secondary": 28, "tertiary": 32,
                "residential": 36, "unclassified": 36}
BOTH_SIDES = {"trunk", "primary", "secondary", "tertiary"}
OUTER_ON_DUAL = {"motorway": 0, "trunk": 0, "primary": 52, "secondary": 52, "tertiary": 56,
                 "residential": 60, "unclassified": 60}

# budgets
DYN_BLOCK = 1450                  # dynamic instances per 2x2 block of stream cells (1 km^2)
DYN_R100 = 120                    # dynamic instances within 100 units
STATIC_BLOCK = 2700               # static props (trees, stone benches, bins) per 2x2 block

BOULEVARD_BOX = (-200.0, -2950.0, 1000.0, -2450.0)


class Models:
    def __init__(self):
        m = pd.read_csv("data/sa/models.csv", low_memory=False)
        self.row = {int(r.id): r for r in m.itertuples()}
        self.dyn = {i for i, r in self.row.items()
                    if isinstance(r.engine_refs, str) and "object.dat" in r.engine_refs}

    def name(self, mid):
        return str(self.row[mid].name)

    def min_z(self, mid):
        v = self.row[mid].aabb_min_z
        return float(v) if v == v else 0.0

    def base(self, mid, tree=False):
        """Height of the model pivot above the ground it stands on."""
        mz = self.min_z(mid)
        if tree:
            return -mz if mz < -1.0 else -0.12
        return -mz


def _unit(v):
    n = float(np.hypot(*v))
    return v / n if n > 1e-9 else v * 0


def _heading_fwd(d):
    """Heading whose local +y axis points along d."""
    return math.atan2(-d[0], d[1])


def _heading_negx(d):
    """Heading whose local -x axis points along d."""
    return math.atan2(-d[1], -d[0])


def _sample(p, s_query):
    """Points and unit tangents at arc positions along a polyline."""
    s = profiles.arclen(p)
    s_query = np.clip(np.asarray(s_query, float), 0, s[-1])
    i = np.clip(np.searchsorted(s, s_query, side="right") - 1, 0, len(p) - 2)
    f = (s_query - s[i]) / np.maximum(s[i + 1] - s[i], 1e-9)
    pts = p[i] + (p[i + 1] - p[i]) * f[:, None]
    t = p[i + 1] - p[i]
    t = t / np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
    return pts, t


class Ctx:
    """Shared geometry for the placement passes."""

    def __init__(self, W, blds):
        self.W = W
        self.M = Models()
        self.rng = np.random.default_rng(RNG_SEED)
        from tools.build import rail
        self.road_parts = [p for p in W.road_parts + W.minor_parts + rail.corridors(half=2.6)
                           if p is not None and not p.is_empty]
        self.road_tree = shapely.STRtree(self.road_parts)
        self.path_parts = [p for p in (W.path_parts or []) if not p.is_empty]
        self.path_tree = shapely.STRtree(self.path_parts) if self.path_parts else None
        self.sw_parts = [p for p in (W.sw_parts or []) if not p.is_empty]
        self.sw_tree = shapely.STRtree(self.sw_parts)
        self.bld = [b["poly"] for b in blds if b.get("poly") is not None and not b["poly"].is_empty]
        self.bld_tree = shapely.STRtree(self.bld)
        sea, lakes = W.water_polys
        self.water = [g for g in getattr(sea, "geoms", [sea]) if not g.is_empty] + [p for p, _ in lakes]
        self.water_tree = shapely.STRtree(self.water)
        # elevated / sunken structure footprints (nothing grows under viaducts or in trenches)
        from tools.build.structures import spans, span_polygon
        st = []
        for e in W.g.edges.values():
            if e.major:
                for i0, i1 in spans(e):
                    p = span_polygon(e, i0, i1, extra=1.5)
                    if not p.is_empty:
                        st.append(p)
        self.struct = st
        self.struct_tree = shapely.STRtree(st) if st else None
        self.hard = self._landuse_hard()
        self.hard_tree = shapely.STRtree(self.hard) if self.hard else None
        self.placed = defaultdict(list)          # grid cell (8 units) -> [(x, y, kind)]

    def _landuse_hard(self):
        """Paved/sport/parking areas where no trees grow."""
        out = []
        world = shapely.box(-WORLD, -WORLD, WORLD, WORLD)
        hard_lu = {"pitch", "playground", "plaza", "pedestrian", "stadium", "track", "garages", "railway"}
        for r in pq.read_table("data/raw/land_use.parquet", columns=["class", "geometry"]).to_pylist():
            if r["class"] in hard_lu:
                g = ground._to_game(shapely.from_wkb(r["geometry"])).intersection(world)
                if not g.is_empty and g.geom_type in ("Polygon", "MultiPolygon"):
                    out.append(g)
        for r in pq.read_table("data/raw/infrastructure.parquet", columns=["class", "geometry"]).to_pylist():
            if r["class"] in ("parking", "platform", "helipad", "substation", "storage_tank", "fountain",
                              "pier", "parking_space"):
                g = shapely.from_wkb(r["geometry"])
                if g.geom_type in ("Polygon", "MultiPolygon"):
                    g = ground._to_game(g).intersection(world)
                    if not g.is_empty:
                        out.append(g)
        return out

    # -- point tests (vectorised) --------------------------------------------------------------
    @staticmethod
    def _hits(tree, xy, dist):
        if tree is None or len(xy) == 0:
            return np.zeros(len(xy), bool)
        pts = shapely.points(xy)
        if dist > 0:
            idx = tree.query(pts, predicate="dwithin", distance=dist)
        else:
            idx = tree.query(pts, predicate="intersects")
        out = np.zeros(len(xy), bool)
        out[np.unique(idx[0])] = True
        return out

    def on_road(self, xy, dist=0.0):
        return self._hits(self.road_tree, xy, dist)

    def in_building(self, xy, dist=0.0):
        return self._hits(self.bld_tree, xy, dist)

    def in_water(self, xy, dist=0.0):
        return self._hits(self.water_tree, xy, dist)

    def near_struct(self, xy):
        return self._hits(self.struct_tree, xy, 0.0)

    def free_for_tree(self, xy, road=2.0, bld=2.5, path=1.2, sw=1.0):
        bad = self.on_road(xy, road) | self.in_building(xy, bld) | self.in_water(xy, 1.0) | self.near_struct(xy)
        if self.path_tree is not None and path >= 0:
            bad |= self._hits(self.path_tree, xy, path)
        if sw >= 0:
            bad |= self._hits(self.sw_tree, xy, sw)
        bad |= self._hits(self.hard_tree, xy, 0.5)
        inside = (np.abs(xy[:, 0]) < WORLD - 4) & (np.abs(xy[:, 1]) < WORLD - 4)
        return ~bad & inside

    # -- spacing between placed props -------------------------------------------------------------
    def near_placed(self, x, y, r, kinds=None):
        g = 8.0
        cx, cy = int(math.floor(x / g)), int(math.floor(y / g))
        k = int(math.ceil(r / g))
        for i in range(cx - k, cx + k + 1):
            for j in range(cy - k, cy + k + 1):
                for (px, py, kd) in self.placed.get((i, j), ()):
                    if (kinds is None or kd in kinds) and (px - x) ** 2 + (py - y) ** 2 < r * r:
                        return True
        return False

    def mark(self, x, y, kind):
        g = 8.0
        self.placed[(int(math.floor(x / g)), int(math.floor(y / g)))].append((x, y, kind))

    def ground_z(self, x, y, kerb=True):
        return float(ground.H_at(self.W, x, y)[0]) + (ground.KERB if kerb else 0.0)


def _prop(ctx, mid, x, y, z, h, kind, prio, rank=0):
    return {"id": int(mid), "name": ctx.M.name(mid), "x": float(x), "y": float(y), "z": float(z),
            "heading": float(h) % (2 * math.pi), "kind": kind, "dyn": mid in ctx.M.dyn,
            "prio": prio, "rank": rank, "r": float(ctx.rng.random())}


# ================================================================================ traffic lights
def traffic_lights(ctx, lights):
    """One mast per signalised approach on the right kerb (+ pole head on the median of wide
    one-way approaches).  `lights`: build/out/paths/lights.json records."""
    from scipy.spatial import cKDTree
    W = ctx.W
    g = W.g
    nid = list(g.nodes.keys())
    nxy = np.array([g.nodes[n] for n in nid])
    tree = cKDTree(nxy)
    inc = defaultdict(list)
    for e in g.edges.values():
        if e.major and e.level == 0:
            inc[e.u].append((e, "u"))
            inc[e.v].append((e, "v"))
    out = []
    miss = 0
    for L in lights:
        J = np.array([L["jx"], L["jy"]])
        O = np.array([L["ox"], L["oy"]])
        d = _unit(J - O)
        dist, k = tree.query(J)
        if dist > 1.0:
            miss += 1
            continue
        gn = nid[k]
        best, bc = None, 0.6
        for e, end in inc[gn]:
            p = e.pts if end == "u" else e.pts[::-1]
            away = _unit(p[min(len(p) - 1, 3)] - p[0])
            c = float(away @ -d)
            if c > bc:
                best, bc = (e, end), c
        if best is None:
            miss += 1
            continue
        e, end = best
        if e.id in W.lay.ribbons:
            r = W.lay.ribbons[e.id]
            n_all = polyline_normals(r.center)
            if end == "v":
                pc, nrm, half = r.center[-1], n_all[-1], r.right
                right = -nrm
            else:
                pc, nrm, half = r.center[0], n_all[0], r.left
                right = nrm
        else:
            # approach edge lies inside a junction cluster: stand at the edge of the junction area
            jid = W.lay.node_junction.get(gn)
            if jid is None:
                miss += 1
                continue
            jp = W.lay.junctions[jid].polygon
            ts = np.arange(0.0, min(60.0, e.length), 0.5)
            q = J[None, :] - d[None, :] * ts[:, None]
            inside = shapely.contains_xy(jp, q[:, 0], q[:, 1])
            out_idx = np.nonzero(~inside)[0]
            if len(out_idx) == 0:
                miss += 1
                continue
            pc = q[out_idx[0]]
            half = e.width / 2
            right = np.array([d[1], -d[0]])
        right = _unit(right)
        placed = False
        for extra in (0.8, 1.6, 2.4):
            P = pc + right * (half + extra) - d * 0.6
            if not ctx.on_road(P[None])[0] and not ctx.in_building(P[None], 0.4)[0]:
                placed = True
                break
        if not placed:
            miss += 1
            continue
        z = ctx.ground_z(*P) + ctx.M.base(TL_MAST)
        h = _heading_fwd(d)
        # the phase the engine will show (object forward axis) must equal the AI light group
        from tools.build.build_paths import light_group_for_dir
        if light_group_for_dir(-math.sin(h), math.cos(h)) != L["group"]:
            raise AssertionError(f"traffic light phase mismatch at {P}")
        out.append(_prop(ctx, TL_MAST, P[0], P[1], z, h, "tlight", 0))
        ctx.mark(P[0], P[1], "tlight")
        # left-side head on wide one-way approaches (dual carriageways)
        if L["lanes_in"] >= 3 and L["lanes_out"] == 0:
            left = -right
            for extra in (0.7, 1.3):
                Q = pc + left * (half + extra) - d * 0.6
                if not ctx.on_road(Q[None])[0] and not ctx.in_building(Q[None], 0.3)[0]:
                    zq = ctx.ground_z(*Q) + ctx.M.base(TL_POLE)
                    out.append(_prop(ctx, TL_POLE, Q[0], Q[1], zq, h, "tlight", 0))
                    ctx.mark(Q[0], Q[1], "tlight")
                    break
    return out, miss


# ================================================================================ bus stops
def bus_stops(ctx):
    W = ctx.W
    rs = [r for r in W.lay.ribbons.values() if len(r.center) >= 2]
    lines = [shapely.LineString(r.center) for r in rs]
    tree = shapely.STRtree(lines)
    out = []
    pts = []
    for r in pq.read_table("data/raw/infrastructure.parquet", columns=["class", "geometry"]).to_pylist():
        if r["class"] == "bus_stop":
            p = shapely.from_wkb(r["geometry"])
            if p.geom_type == "Point":
                pts.append(lonlat_to_game(p.x, p.y))
    for x, y in pts:
        if abs(x) > WORLD - 10 or abs(y) > WORLD - 10:
            continue
        p = shapely.Point(x, y)
        i = tree.nearest(p)
        if i is None or lines[int(i)].distance(p) > 25:
            continue
        r = rs[int(i)]
        e = r.edge
        if e.cls in ("motorway",):
            continue
        s = lines[int(i)].project(p)
        L = lines[int(i)].length
        if s < 6 or s > L - 6:
            s = min(max(s, 6.0), max(L - 6.0, 6.0))
            if L < 12:
                continue
        (c,), (t,) = _sample(r.center, [s])
        nrm = np.array([-t[1], t[0]])
        # lift check (no shelters on structures)
        s_full = profiles.arclen(e.pts)
        lf = float(np.interp(r.s0 + s, s_full, e.lift))
        if abs(lf) > 0.3:
            continue
        off = float((np.array([x, y]) - c) @ nrm)
        if e.oneway:
            side = -1 if not e.attrs.get("inner_right") else 1
            if e.attrs.get("inner_right") and e.attrs.get("inner_left"):
                continue
        else:
            side = 1 if off > 0.5 else -1
        half = r.left if side > 0 else r.right
        sw = r.sw_left if side > 0 else r.sw_right
        to_road = -side * nrm
        ok = False
        for extra in (1.25, 2.2, 3.2):
            P = c + side * nrm * (half + extra)
            rect = shapely.Polygon([P + t * 2.6 + side * nrm * 1.05, P - t * 2.6 + side * nrm * 1.05,
                                    P - t * 2.6 - side * nrm * 1.05, P + t * 2.6 - side * nrm * 1.05])
            if (not ctx.road_tree.query(rect, predicate="intersects").size and
                    not ctx.bld_tree.query(rect, predicate="intersects").size and
                    not ctx.near_placed(P[0], P[1], 18.0, {"bus"}) and
                    not ctx.near_placed(P[0], P[1], 4.0)):
                ok = True
                break
        if not ok:
            continue
        z = ctx.ground_z(*P) + ctx.M.base(BUS_SHELTER)
        out.append(_prop(ctx, BUS_SHELTER, P[0], P[1], z, _heading_negx(to_road), "bus", 1))
        ctx.mark(P[0], P[1], "bus")
        # waste bin next to the shelter
        B = P + t * 3.4 + side * nrm * 0.2
        if not ctx.on_road(B[None])[0] and not ctx.in_building(B[None], 0.3)[0]:
            out.append(_prop(ctx, WASTEBIN, B[0], B[1], ctx.ground_z(*B) + ctx.M.base(WASTEBIN),
                             _heading_negx(to_road), "bin", 1))
    return out


# ================================================================================ lamps
def _gap_left(ctx, r, P, nrm, base, max_gap=14.0, step=0.5):
    """Distance from the carriageway edge to the next road surface along +nrm (median width)."""
    ks = np.arange(step, max_gap + step, step)
    probe = P[:, None, :] + nrm[:, None, :] * (base + ks)[None, :, None]
    flat = probe.reshape(-1, 2)
    hit = ctx.on_road(flat).reshape(len(P), len(ks))
    gap = np.where(hit.any(1), ks[np.argmax(hit, axis=1)], np.inf)
    return gap


def lamps(ctx):
    W = ctx.W
    out = []
    for r in W.lay.ribbons.values():
        e = r.edge
        S = LAMP_SPACING.get(e.cls)
        if not S:
            continue
        c = r.center
        L = float(profiles.arclen(c)[-1])
        if L < 14:
            continue
        s_full = profiles.arclen(e.pts)
        dual = e.oneway and e.attrs.get("inner_left") and not e.attrs.get("inner_right")
        d_edge = _unit(c[-1] - c[0])
        model_side = LAMP_MAIN if e.cls in ("trunk", "primary", "secondary", "motorway") else \
            (LAMP_SMALL if e.cls == "tertiary" else LAMP_ALT)
        runs = []          # (side, spacing, phase, kind)
        if dual:
            # median lamps from one carriageway of the pair only (positive x direction)
            if d_edge[0] > 0.05 or (abs(d_edge[0]) <= 0.05 and d_edge[1] > 0):
                runs.append((+1, S, 0.0, "median"))
            so = OUTER_ON_DUAL.get(e.cls, 0)
            if so:
                runs.append((-1, so, so * 0.25, "side"))
            runs.append((-1, S, 0.0, "deck"))
        elif e.oneway:
            runs.append((-1, S, 0.0, "side"))
        else:
            if e.cls in BOTH_SIDES:
                runs.append((-1, S, 0.0, "side"))
                runs.append((+1, S, S / 2, "side"))
            else:
                runs.append((-1, S, 0.0, "side"))
        for side, sp, phase, kind in runs:
            s = np.arange(7.0 + phase, L - 6.0, sp)
            if len(s) == 0:
                continue
            P, T = _sample(c, s)
            N = np.c_[-T[:, 1], T[:, 0]]
            lf = np.interp(r.s0 + s, s_full, e.lift)
            zr = np.interp(r.s0 + s, s_full, e.z) + lf
            if kind == "median":
                gap = _gap_left(ctx, r, P, N, r.left)
                for k in range(len(s)):
                    if not (1.0 <= gap[k] < 14.0) or abs(lf[k]) > 0.3:
                        continue
                    Q = P[k] + N[k] * (r.left + gap[k] / 2)
                    if ctx.near_placed(Q[0], Q[1], 7.0):
                        continue
                    z = ctx.ground_z(*Q) + ctx.M.base(LAMP_MEDIAN)
                    out.append(_prop(ctx, LAMP_MEDIAN, Q[0], Q[1], z, math.atan2(N[k][1], N[k][0]), "lamp", 2,
                                     rank=k % 2))
                    ctx.mark(Q[0], Q[1], "lamp")
                continue
            half = r.left if side > 0 else r.right
            for k in range(len(s)):
                to_road = -side * N[k]
                if lf[k] > 0.3 and kind == "side" and dual:
                    continue
                if lf[k] <= 0.3 and kind == "deck":
                    continue
                if lf[k] > 0.3:
                    # bridge / viaduct: lamp on the parapet
                    from tools.build.structures import PARAPET_H
                    hw = e.width / 2
                    Q = P[k] + side * N[k] * (hw - 0.2)
                    z = zr[k] + PARAPET_H + ctx.M.base(model_side)
                elif lf[k] < -0.3:
                    continue
                else:
                    Q = P[k] + side * N[k] * (half + 0.75)
                    if ctx.on_road(Q[None])[0] or ctx.in_building(Q[None], 0.5)[0] or ctx.in_water(Q[None])[0]:
                        continue
                    z = ctx.ground_z(*Q) + ctx.M.base(model_side)
                if ctx.near_placed(Q[0], Q[1], 6.0) or ctx.near_placed(Q[0], Q[1], 12.0, {"lamp"}):
                    continue
                prio = 3 if e.cls in ("trunk", "primary", "motorway") else 4
                out.append(_prop(ctx, model_side, Q[0], Q[1], z, _heading_negx(to_road), "lamp", prio, rank=k % 2))
                ctx.mark(Q[0], Q[1], "lamp")
    return out


# ================================================================================ paths: benches, decor lamps
def _green_index(ctx):
    world = shapely.box(-WORLD, -WORLD, WORLD, WORLD)
    polys = []
    for r in pq.read_table("data/raw/land_use.parquet", columns=["class", "geometry"]).to_pylist():
        if r["class"] in ("park", "garden", "recreation_ground", "village_green", "zoo"):
            g = shapely.from_wkb(r["geometry"])
            if g.geom_type in ("Polygon", "MultiPolygon"):
                g = ground._to_game(g).intersection(world)
                if not g.is_empty:
                    polys.append(g)
    return polys


def park_furniture(ctx):
    W = ctx.W
    greens = _green_index(ctx)
    gtree = shapely.STRtree(greens) if greens else None
    bx0, by0, bx1, by1 = BOULEVARD_BOX
    out = []
    for e in W.g.edges.values():
        if e.cls not in ("footway", "path", "pedestrian") or e.length < 20:
            continue
        p = e.pts
        if not ((np.abs(p[:, 0]) < WORLD - 5) & (np.abs(p[:, 1]) < WORLD - 5)).all():
            continue
        mid = p[len(p) // 2]
        in_blvd = bx0 <= mid[0] <= bx1 and by0 <= mid[1] <= by1
        in_park = gtree is not None and gtree.query(shapely.Point(*mid), predicate="intersects").size > 0
        if not (in_blvd or in_park):
            continue
        sp = 26.0 if in_blvd else 38.0
        L = float(profiles.arclen(p)[-1])
        s = np.arange(sp / 2, L - 4, sp)
        if len(s) == 0:
            continue
        P, T = _sample(p, s)
        N = np.c_[-T[:, 1], T[:, 0]]
        hw = e.width / 2
        for k in range(len(s)):
            # bench on the left, decorative lamp on the right (offset along the path)
            B = P[k] + N[k] * (hw + 0.75)
            if ctx.free_for_tree(B[None], road=1.5, bld=1.0, path=0.0, sw=0.0)[0] and not ctx.near_placed(B[0], B[1], 5.0):
                fwd = T[k]
                if in_blvd or k % 2 == 0:
                    z = ctx.ground_z(*B) + ctx.M.base(BENCH)
                    out.append(_prop(ctx, BENCH, B[0], B[1], z, _heading_negx(-N[k]), "bench", 5, rank=k % 2))
                else:
                    z = ctx.ground_z(*B) + ctx.M.base(BENCH_STONE)
                    out.append(_prop(ctx, BENCH_STONE, B[0], B[1], z, math.atan2(fwd[1], fwd[0]), "bench", 5, rank=k % 2))
                ctx.mark(B[0], B[1], "bench")
                if k % 3 == 1:
                    C = B + T[k] * 1.9
                    out.append(_prop(ctx, WASTEBIN, C[0], C[1], ctx.ground_z(*C) + ctx.M.base(WASTEBIN), 0.0, "bin", 5))
            if k % 2 == 0 or in_blvd:
                (Lp,), (Lt,) = _sample(p, [min(s[k] + sp / 2, L - 1)])
                Ln = np.array([-Lt[1], Lt[0]])
                Q = Lp - Ln * (hw + 0.55)
                if ctx.free_for_tree(Q[None], road=1.0, bld=0.8, path=0.0, sw=0.0)[0] and not ctx.near_placed(Q[0], Q[1], 6.0):
                    mid_ = LAMP_DECOR[k % 2]
                    out.append(_prop(ctx, mid_, Q[0], Q[1], ctx.ground_z(*Q) + ctx.M.base(mid_), ctx.rng.random() * 6.28,
                                     "lamp", 6, rank=k % 2))
                    ctx.mark(Q[0], Q[1], "lamp")
    # OSM benches
    for r in pq.read_table("data/raw/infrastructure.parquet", columns=["class", "geometry"]).to_pylist():
        if r["class"] != "bench":
            continue
        g = shapely.from_wkb(r["geometry"])
        if g.geom_type != "Point":
            continue
        x, y = lonlat_to_game(g.x, g.y)
        if abs(x) > WORLD - 5 or abs(y) > WORLD - 5 or ctx.near_placed(x, y, 3.0):
            continue
        xy = np.array([[x, y]])
        if ctx.on_road(xy, 0.8)[0] or ctx.in_building(xy, 0.6)[0] or ctx.in_water(xy)[0]:
            continue
        out.append(_prop(ctx, BENCH, x, y, ctx.ground_z(x, y) + ctx.M.base(BENCH), ctx.rng.random() * 6.28,
                         "bench", 5))
        ctx.mark(x, y, "bench")
    return out


# ================================================================================ trees
def _pick(ctx, kind, n):
    lst = TREES[kind]
    ids = np.array([m for m, _ in lst])
    w = np.array([w for _, w in lst], float)
    return ids[ctx.rng.choice(len(ids), size=n, p=w / w.sum())]


def _tree_props(ctx, xy, kind, prio, ranks=None):
    out = []
    if len(xy) == 0:
        return out
    mids = _pick(ctx, kind, len(xy))
    zs = ground.H_at(ctx.W, xy[:, 0], xy[:, 1]) + ground.KERB
    hs = ctx.rng.random(len(xy)) * 2 * math.pi
    for k in range(len(xy)):
        mid = int(mids[k])
        out.append(_prop(ctx, mid, xy[k, 0], xy[k, 1], zs[k] + ctx.M.base(mid, tree=True), hs[k], "tree", prio,
                         rank=0 if ranks is None else int(ranks[k])))
        ctx.mark(xy[k, 0], xy[k, 1], "tree")
    return out


def _jitter_grid(ctx, poly_list, spacing, jitter=0.42):
    """Jittered grid samples inside polygons."""
    out = []
    for poly in poly_list:
        if poly.is_empty:
            continue
        x0, y0, x1, y1 = poly.bounds
        gx = np.arange(x0 + spacing / 2, x1, spacing)
        gy = np.arange(y0 + spacing / 2, y1, spacing)
        if len(gx) == 0 or len(gy) == 0:
            c = poly.representative_point()
            out.append([c.x, c.y])
            continue
        X, Y = np.meshgrid(gx, gy)
        P = np.c_[X.ravel(), Y.ravel()]
        P = P + (ctx.rng.random(P.shape) - 0.5) * 2 * jitter * spacing
        P = P[shapely.contains_xy(poly, P[:, 0], P[:, 1])]
        if len(P):
            out.append(P)
    return np.vstack([np.atleast_2d(np.asarray(a, float)) for a in out]) if out else np.zeros((0, 2))


def trees(ctx):
    W = ctx.W
    out = []
    world = shapely.box(-WORLD + 5, -WORLD + 5, WORLD - 5, WORLD - 5)
    # 1. OSM single trees and tree rows
    pts, rows = [], []
    for r in pq.read_table("data/raw/land.parquet", columns=["subtype", "class", "geometry"]).to_pylist():
        g = shapely.from_wkb(r["geometry"])
        if r["class"] == "tree" and g.geom_type == "Point":
            pts.append(lonlat_to_game(g.x, g.y))
        elif r["class"] == "tree_row" and g.geom_type == "LineString":
            gg = ground._to_game(g)
            L = gg.length
            for s in np.arange(2.0, L, 7.0):
                q = gg.interpolate(s)
                rows.append((q.x, q.y))
    for arr, prio in ((np.array(pts, float).reshape(-1, 2), 0), (np.array(rows, float).reshape(-1, 2), 1)):
        ok = ctx.free_for_tree(arr, road=1.2, bld=1.5, path=0.6, sw=-1) if len(arr) else np.zeros(0, bool)
        # (sidewalk trees allowed: sw=-1 -> distance check skipped below)
        out += _tree_props(ctx, arr[ok], "street" if prio == 1 else "park", prio)
    # 2. street trees on wide sidewalks (between the lamps)
    st_xy, st_rank = [], []
    for r in W.lay.ribbons.values():
        e = r.edge
        if e.cls not in ("residential", "tertiary", "secondary", "unclassified", "primary"):
            continue
        L = float(profiles.arclen(r.center)[-1])
        if L < 30:
            continue
        s_full = profiles.arclen(e.pts)
        for side, sw, half in ((-1, r.sw_right, r.right), (+1, r.sw_left, r.left)):
            if sw < 1.9:
                continue
            if e.cls == "residential" and side > 0 and not e.oneway:
                continue
            s = np.arange(10.0 + (4.0 if side > 0 else 0.0), L - 10.0, 13.0)
            if len(s) == 0:
                continue
            P, T = _sample(r.center, s)
            N = np.c_[-T[:, 1], T[:, 0]]
            lf = np.abs(np.interp(r.s0 + s, s_full, e.lift))
            Q = P + side * N * (half + 0.8)
            keep = lf < 0.3
            for k in np.nonzero(keep)[0]:
                if not ctx.near_placed(Q[k, 0], Q[k, 1], 4.5):
                    st_xy.append(Q[k])
                    st_rank.append(k % 3)
    if st_xy:
        st_xy = np.array(st_xy)
        st_rank = np.array(st_rank)
        ok = ~ctx.on_road(st_xy, 0.3) & ~ctx.in_building(st_xy, 2.6) & ~ctx.in_water(st_xy) & ~ctx.near_struct(st_xy)
        out += _tree_props(ctx, st_xy[ok], "street", 3, st_rank[ok])
    # 3. areas
    cls_spacing = {"forest": ("forest", 9.0), "wood": ("forest", 10.0), "park": ("park", 12.0),
                   "garden": ("park", 12.0), "recreation_ground": ("park", 14.0), "village_green": ("park", 14.0),
                   "cemetery": ("cemetery", 9.0), "grass": ("court", 18.0), "residential": ("court", 24.0),
                   "school": ("court", 17.0), "kindergarten": ("court", 15.0), "hospital": ("court", 16.0),
                   "university": ("court", 16.0), "college": ("court", 17.0), "zoo": ("park", 13.0),
                   "grassland": ("court", 22.0), "scrub": ("court", 22.0), "heath": ("court", 22.0)}
    by_kind = defaultdict(list)
    for fn in ("land_use", "land"):
        for r in pq.read_table(f"data/raw/{fn}.parquet", columns=["class", "geometry"]).to_pylist():
            if r["class"] not in cls_spacing:
                continue
            g = shapely.from_wkb(r["geometry"])
            if g.geom_type not in ("Polygon", "MultiPolygon"):
                continue
            g = ground._to_game(g).intersection(world)
            if g.is_empty:
                continue
            by_kind[cls_spacing[r["class"]]].append(g)
    bx0, by0, bx1, by1 = BOULEVARD_BOX
    blvd = shapely.box(bx0, by0, bx1, by1)
    for (kind, spacing), polys in by_kind.items():
        prio = 5 if kind == "court" else 4
        xy = _jitter_grid(ctx, polys, spacing)
        if len(xy) == 0:
            continue
        ok = ctx.free_for_tree(xy)
        xy = xy[ok]
        xy = xy[[not ctx.near_placed(x, y, 3.5) for x, y in xy]] if len(xy) else xy
        inb = shapely.contains_xy(blvd, xy[:, 0], xy[:, 1]) if len(xy) else np.zeros(0, bool)
        out += _tree_props(ctx, xy[~inb], kind, prio)
        out += _tree_props(ctx, xy[inb], "boulevard", 2)
    return out


# ================================================================================ budgets
def apply_budgets(props, verbose=True):
    """Keep props in priority order (rank: every-other lamp / every-third tree drops first)
    while the per-block and radius budgets hold."""
    order = sorted(range(len(props)), key=lambda i: (props[i]["prio"], props[i]["rank"], props[i]["r"]))
    blk_dyn = defaultdict(int)
    blk_sta = defaultdict(int)
    G = 25.0
    grid = defaultdict(int)
    keep = []
    rr = int(math.ceil(100.0 / G))
    dropped = defaultdict(int)
    for i in order:
        p = props[i]
        cx = min(NC - 1, max(0, int((p["x"] + WORLD) // CELL)))
        cy = min(NC - 1, max(0, int((p["y"] + WORLD) // CELL)))
        blocks = [(bx, by) for bx in (cx - 1, cx) for by in (cy - 1, cy)]
        if p["dyn"]:
            if any(blk_dyn[b] >= DYN_BLOCK for b in blocks):
                dropped[p["kind"]] += 1
                continue
            gx, gy = int(math.floor(p["x"] / G)), int(math.floor(p["y"] / G))
            cnt = 0
            for a in range(gx - rr, gx + rr + 1):
                for b in range(gy - rr, gy + rr + 1):
                    if ((a + 0.5) * G - p["x"]) ** 2 + ((b + 0.5) * G - p["y"]) ** 2 <= (100.0 + G * 0.71) ** 2:
                        cnt += grid.get((a, b), 0)
            if cnt >= DYN_R100:
                dropped[p["kind"]] += 1
                continue
            grid[(gx, gy)] += 1
            for b in blocks:
                blk_dyn[b] += 1
        else:
            if any(blk_sta[b] >= STATIC_BLOCK for b in blocks):
                dropped[p["kind"]] += 1
                continue
            for b in blocks:
                blk_sta[b] += 1
        keep.append(p)
    if verbose:
        print("budget: kept", len(keep), "of", len(props), "dropped", dict(dropped),
              "max dyn/block", max(blk_dyn.values(), default=0), "max static/block", max(blk_sta.values(), default=0))
    return keep


def place_all(W, blds, lights, verbose=True):
    ctx = Ctx(W, blds)
    out = []
    tl, miss = traffic_lights(ctx, lights)
    out += tl
    if verbose:
        print(f"traffic lights: {len(tl)} ({miss} approaches without a usable kerb)")
    b = bus_stops(ctx)
    out += b
    if verbose:
        print("bus stops + bins:", len(b))
    from tools.build import signs
    bs = signs.bus_stop_signs(ctx, b)
    out += bs
    lp = lamps(ctx)
    out += lp
    if verbose:
        print("street lamps:", len(lp))
    rs = signs.road_sign_candidates(ctx, lights)
    out += rs
    if verbose:
        print("road signs:", len(rs) + len(bs))
    pf = park_furniture(ctx)
    out += pf
    if verbose:
        print("park furniture:", len(pf))
    tr = trees(ctx)
    out += tr
    if verbose:
        print("trees:", len(tr))
    lim = WORLD - 4.0
    out = [p for p in out if abs(p["x"]) < lim and abs(p["y"]) < lim and math.isfinite(p["z"])]
    keep = apply_budgets(out, verbose)
    for p in keep:
        for k in ("prio", "rank", "r"):
            p.pop(k, None)
    # OSM crossings on major roads (edge id, arc position) for the zebra markings (signtiles.py)
    place_all.crossings = [(r.edge.id, float(s)) for r, s in getattr(ctx, "crossings", [])]
    return keep


def stats(props):
    from collections import Counter
    c = Counter(p["kind"] for p in props)
    d = sum(p["dyn"] for p in props)
    return dict(c), d


if __name__ == "__main__":
    import json
    import pickle
    import time
    t0 = time.time()
    W = ground.prepare()
    blds, _ = pickle.load(open("data/cache/buildings.pkl", "rb"))
    lights = json.load(open("build/out/paths/lights.json"))
    props = place_all(W, blds, lights)
    pickle.dump(props, open("data/cache/props.pkl", "wb"))
    pickle.dump(place_all.crossings, open("data/cache/crossings.pkl", "wb"))
    print("props:", len(props), stats(props), f"{time.time() - t0:.0f}s")
