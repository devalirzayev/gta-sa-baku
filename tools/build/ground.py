"""Ground world builder: roads + junctions + sidewalks + kerbs + land cover + terrain, tiled.

Usage:
    W = prepare()            # heavy: graph, displacement, profiles, layout, terrain (cached)
    mb = build_tile(W, ix, iy)
"""
from __future__ import annotations

import math
import os
import pickle
from dataclasses import dataclass

import numpy as np
import pyarrow.parquet as pq
import shapely
from scipy import ndimage
from scipy.spatial import cKDTree
from shapely.ops import unary_union

from tools.geo import roads, roadmesh, terrain, profiles
from tools.geo.proj import lonlat_to_game, WORLD
from tools.rw.col import SURF
from tools.build.mesh import MeshBuilder, triangulate_area, strip, wall

TILE = 250.0
NT = int(2 * WORLD / TILE)          # 24 x 24 tiles
KERB = 0.15
CACHE = "data/cache"

SUN = np.array([-0.45, -0.55, 0.70])
SUN = SUN / np.linalg.norm(SUN)

# material -> (planar uv scale, collision surface)
GROUND_MATS = {
    "bk_asph": (14.0, SURF["TARMAC"]),
    "bk_asph2": (12.0, SURF["TARMAC_FUCKED"]),
    "bk_pave": (5.0, SURF["PAVEMENT"]),
    "bk_plaza": (6.0, SURF["PAVEMENT"]),
    "bk_curb": (4.0, SURF["PAVEMENT"]),
    "bk_grass": (12.0, SURF["GRASS_SHORT_LUSH"]),
    "bk_grassdry": (14.0, SURF["GRASS_SHORT_DRY"]),
    "bk_dirt": (14.0, SURF["DIRT"]),
    "bk_soil": (16.0, SURF["SAND_COMPACT"]),
    "bk_sand": (12.0, SURF["SAND_BEACH"]),
    "bk_seabed": (16.0, SURF["SAND_DEEP"]),
    "bk_conc": (10.0, SURF["FUCKED_CONCRETE"]),
    "bk_path": (6.0, SURF["GRAVEL"]),
    "bk_gravel": (6.0, SURF["GRAVEL"]),
}
ROAD_TEX = {(1, 1): "bk_road11", (2, 2): "bk_road22", (3, 3): "bk_road33",
            (1, 0): "bk_road10", (2, 0): "bk_road20", (3, 0): "bk_road30"}


@dataclass
class World:
    g: object
    lay: object
    H: np.ndarray
    T: object
    cover: list          # [(polygon, material)] in priority order
    minor_area: object
    path_area: object
    lifted: dict         # edge id -> bool mask of structure vertices
    water_polys: tuple
    road_parts: list = None
    sw_parts: list = None
    minor_parts: list = None
    path_parts: list = None
    trench_parts: list = None


# ---------------------------------------------------------------------------------------------
def _to_game(geom):
    return shapely.transform(geom, lambda c: np.c_[lonlat_to_game(c[:, 0], c[:, 1])])


def load_water():
    world = shapely.box(-WORLD, -WORLD, WORLD, WORLD)
    rows = pq.read_table("data/raw/water.parquet").to_pylist()
    sea = shapely.Polygon()
    lakes = []
    for r in rows:
        g = shapely.from_wkb(r["geometry"])
        if g.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        nm = (r["names"] or {}).get("primary") or ""
        if "Xəzər" in nm or r["class"] == "ocean":
            sea = sea.union(_to_game(g.intersection(shapely.box(49.70, 40.30, 49.98, 40.50))).intersection(world))
    for r in rows:
        g = shapely.from_wkb(r["geometry"])
        if g.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        nm = (r["names"] or {}).get("primary") or ""
        if "Xəzər" in nm or r["class"] == "ocean" or r["class"] in ("swimming_pool", "fountain"):
            continue
        gg = _to_game(g).intersection(world).difference(sea)
        if gg.is_empty or gg.area < 150:
            continue
        lakes.append((gg, nm))
    return sea, lakes


def load_cover():
    """Land-use / land-cover polygons -> (game polygon, material), lowest priority first."""
    world = shapely.box(-WORLD, -WORLD, WORLD, WORLD)
    out = []
    lu = pq.read_table("data/raw/land_use.parquet").to_pylist()
    lc = pq.read_table("data/raw/land_cover.parquet").to_pylist()
    ln = pq.read_table("data/raw/land.parquet").to_pylist()
    mat_lc = {"barren": "bk_soil", "shrub": "bk_grassdry", "grass": "bk_grassdry", "wetland": "bk_grass",
              "forest": "bk_grass", "crop": "bk_grassdry"}
    for r in lc:
        m = mat_lc.get(r["subtype"])
        if m:
            g = shapely.from_wkb(r["geometry"])
            out.append((0, _to_game(g).intersection(world), m))
    mat_lu = {"grass": "bk_grass", "park": "bk_grass", "garden": "bk_grass", "pitch": "bk_grass",
              "playground": "bk_plaza", "recreation_ground": "bk_grass", "village_green": "bk_grass",
              "cemetery": "bk_grassdry", "orchard": "bk_grass", "meadow": "bk_grassdry",
              "industrial": "bk_conc", "construction": "bk_dirt", "brownfield": "bk_dirt",
              "greenfield": "bk_soil", "landfill": "bk_dirt", "railway": "bk_gravel", "works": "bk_conc",
              "pedestrian": "bk_plaza", "plaza": "bk_plaza", "garages": "bk_conc", "school": "bk_soil",
              "kindergarten": "bk_grassdry", "hospital": "bk_grassdry", "university": "bk_grassdry",
              "military": "bk_soil", "stadium": "bk_conc", "commercial": "bk_conc", "retail": "bk_conc",
              "farmyard": "bk_dirt", "allotments": "bk_grass", "zoo": "bk_grass", "theme_park": "bk_plaza"}
    prio = {"bk_grass": 3, "bk_plaza": 4, "bk_conc": 2, "bk_dirt": 2, "bk_soil": 1, "bk_grassdry": 2,
            "bk_gravel": 3}
    for r in lu:
        m = mat_lu.get(r["class"])
        if m:
            g = shapely.from_wkb(r["geometry"])
            if g.geom_type in ("Polygon", "MultiPolygon"):
                out.append((prio.get(m, 1), _to_game(g).intersection(world), m))
    for r in ln:
        if r["class"] in ("forest", "wood", "grassland", "scrub", "heath", "shrub"):
            g = shapely.from_wkb(r["geometry"])
            if g.geom_type in ("Polygon", "MultiPolygon"):
                out.append((3, _to_game(g).intersection(world), "bk_grass"))
    out.sort(key=lambda t: t[0])
    return [(g.buffer(0), m) for _, g, m in out if not g.is_empty]


# ---------------------------------------------------------------------------------------------
def prepare(cache=True, verbose=True) -> World:
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "world_ground.pkl")
    if cache and os.path.exists(path):
        return pickle.load(open(path, "rb"))
    g = roads.load_graph()
    roads.simplify_degree2(g)
    roads.assign_layout(g)
    roads.detect_roundabouts(g)
    roads.side_analysis(g)
    roads.displace(g, iters=150, verbose=verbose)
    sea, lakes = load_water()
    # base terrain (lake levels from the raw DEM)
    from tools.geo.proj import elev_to_game
    demg = elev_to_game(terrain.load_dem_grid())
    lk = []
    for poly, nm in lakes:
        m = terrain.rasterize(poly) > 0
        if m.sum() < 4:
            continue
        lk.append((poly, float(np.percentile(demg[m], 30))))
    T = terrain.build_base(sea, lk)
    profiles.compute_profiles(g, T)
    lay = roadmesh.build_layout(g, level=0, verbose=verbose)
    # structure (lifted) vertices per edge
    lifted = {k: np.abs(e.lift) > 0.3 for k, e in g.edges.items()}
    road_parts, sw_parts = ground_parts(g, lay)
    from tools.build.structures import trench_footprint
    trench_parts = [p for p in trench_footprint(g) if not p.is_empty]
    # ground road heights for terrain integration
    pts, zs = [], []
    for r in lay.ribbons.values():
        e = r.edge
        s_full = profiles.arclen(e.pts)
        s_c = r.s0 + profiles.arclen(r.center)
        z = np.interp(s_c, s_full, e.z)
        lf = np.interp(s_c, s_full, e.lift)
        ok = np.abs(lf) <= 0.3
        pts.append(r.center[ok])
        zs.append(z[ok])
    for n, p in g.nodes.items():
        pts.append(p[None, :])
        zs.append(np.array([g.node_z[n]]))
    P = np.vstack(pts)
    Z = np.concatenate(zs)
    road_mask = terrain.rasterize(unary_union(road_parts).buffer(0.5)) > 0
    # remove road pixels that belong to structures
    struct_polys = []
    for e in g.edges.values():
        if e.major and lifted[e.id].any():
            sub = e.pts[lifted[e.id]]
            if len(sub) >= 2:
                struct_polys.append(shapely.LineString(sub).buffer(e.width / 2 + 1.0))
    if struct_polys:
        road_mask &= ~(terrain.rasterize(unary_union(struct_polys)) > 0)
    c = terrain.grid_xy()
    jj, ii = np.nonzero(road_mask)
    tree = cKDTree(P)
    _, idx = tree.query(np.c_[c[ii], c[jj]])
    fixed = np.zeros_like(T.h)
    fixed[jj, ii] = Z[idx]
    if verbose:
        print("membrane solve ...")
    H = terrain.membrane(T.h, road_mask, fixed)
    # water bodies keep their beds
    H[T.sea] = T.h[T.sea]
    H[T.lake > 0] = np.minimum(H[T.lake > 0], T.h[T.lake > 0])
    minor_parts, path_parts = [], []
    for e in g.edges.values():
        if e.major:
            continue
        ls = shapely.LineString(e.pts)
        (path_parts if e.cls in roads.PATHS else minor_parts).append(ls.buffer(e.width / 2, cap_style="round", quad_segs=3))
    cover = load_cover()
    W = World(g, lay, H, T, cover, None, None, lifted, (sea, lk), road_parts, sw_parts, minor_parts,
              path_parts, trench_parts)
    if cache:
        pickle.dump(W, open(path, "wb"))
    return W



def ground_parts(g, lay):
    """Road / sidewalk polygons restricted to ground-level (non-structure) runs."""
    road_parts, sw_parts = [], []
    for r in lay.ribbons.values():
        e = r.edge
        s_full = profiles.arclen(e.pts)
        s_c = r.s0 + profiles.arclen(r.center)
        lf = np.abs(np.interp(s_c, s_full, e.lift))
        ok = lf <= 0.3
        k = 0
        n = len(ok)
        while k < n:
            if not ok[k]:
                k += 1
                continue
            j = k
            while j + 1 < n and ok[j + 1]:
                j += 1
            if j > k:
                sub = roadmesh.Ribbon(e, r.center[k:j + 1], 0, r.left, r.right, r.sw_left, r.sw_right)
                road_parts.append(sub.polygon())
                if r.sw_left > 0:
                    sw_parts.append(sub.strip_polygon(+1, r.left - 0.05, r.left + r.sw_left))
                if r.sw_right > 0:
                    sw_parts.append(sub.strip_polygon(-1, r.right - 0.05, r.right + r.sw_right))
            k = j + 1
    for j in lay.junctions.values():
        road_parts.append(j.polygon)
        if j.sidewalk > 0:
            sw_parts.append(j.polygon.buffer(j.sidewalk, join_style="round", quad_segs=6))
    road_parts = [p for p in road_parts if p is not None and not p.is_empty]
    sw_parts = [p for p in sw_parts if p is not None and not p.is_empty]
    return road_parts, sw_parts


class Spatial:
    """STRtree-backed clipping of many polygons to a box."""
    def __init__(self, parts):
        self.parts = np.array([p for p in parts if p is not None and not p.is_empty], dtype=object)
        self.tree = shapely.STRtree(self.parts) if len(self.parts) else None

    def clip(self, box):
        if self.tree is None:
            return shapely.Polygon()
        idx = self.tree.query(box, predicate="intersects")
        if len(idx) == 0:
            return shapely.Polygon()
        u = unary_union(list(shapely.intersection(self.parts[idx], box)))
        return u.buffer(0)


# ---------------------------------------------------------------------------------------------
def shade_terrain(W, x, y):
    """Fake sun lighting from the height field normal."""
    fi, fj = terrain.Terrain.xy_to_ij(x, y)
    gy, gx = np.gradient(W.H, terrain.CELL)
    dzdx = ndimage.map_coordinates(gx, [fi, fj], order=1, mode="nearest")
    dzdy = ndimage.map_coordinates(gy, [fi, fj], order=1, mode="nearest")
    n = np.c_[-dzdx, -dzdy, np.ones_like(dzdx)]
    n /= np.linalg.norm(n, axis=1)[:, None]
    return np.clip(0.80 + 0.28 * (n @ SUN - 0.70), 0.62, 1.04)


def H_at(W, x, y):
    fi, fj = terrain.Terrain.xy_to_ij(x, y)
    return ndimage.map_coordinates(W.H, [np.atleast_1d(fi), np.atleast_1d(fj)], order=1, mode="nearest")


def tile_box(ix, iy):
    x0 = -WORLD + ix * TILE
    y0 = -WORLD + iy * TILE
    return shapely.box(x0, y0, x0 + TILE, y0 + TILE)


COL_GRID = 18.0          # collision triangulation grid (coarse: CCollision::ProcessColModels keeps at
COL_SIMPLIFY = 0.35      # most 600 triangles touching a vehicle's bounding sphere, Collision.cpp)
# Neighbouring areas are simplified independently, which leaves cracks of up to ~0.4 units in
# the collision (vertical lines - FindGroundZ, car generators, wheel lines - fall through).
# Collision areas therefore overlap: kerb-level areas grow by COL_OVERLAP except into the roads,
# road ribbons and junctions grow by COL_OVERLAP under the kerb-level surfaces.
COL_OVERLAP = 0.25


def add_area(mb, W, geom, mat, z_off=KERB, grid=8.0, under_water=False, clip=None):
    P, Tr = triangulate_area(geom, grid)
    if len(Tr) == 0:
        return
    dz = 0.0 if under_water else z_off
    z = H_at(W, P[:, 0], P[:, 1]) + dz
    S = GROUND_MATS[mat][0]
    UV = np.c_[P[:, 0] / S, -P[:, 1] / S]
    sh = shade_terrain(W, P[:, 0], P[:, 1])
    if under_water:
        sh = sh * 0.8
    mb.add(mat, np.c_[P, z], UV, Tr, shade=sh, collide=False)
    # collision: same area, simplified outline (grown by COL_OVERLAP, not into `clip`), coarse grid
    g2 = geom.simplify(COL_SIMPLIFY, preserve_topology=True)
    g2 = (g2 if not g2.is_empty else geom).buffer(COL_OVERLAP, join_style="mitre", mitre_limit=2.0)
    if clip is not None and not clip.is_empty:
        g2 = g2.difference(clip)
    P2, T2 = triangulate_area(g2, max(COL_GRID, grid * 2))
    if len(T2):
        mb.add_col(np.c_[P2, H_at(W, P2[:, 0], P2[:, 1]) + dz], T2, GROUND_MATS[mat][1])


_SPATIAL = {}


def spatial(W, key):
    k = (id(W), key)
    if k not in _SPATIAL:
        parts = getattr(W, key)
        if key == "cover_by_mat":
            _SPATIAL[k] = None
        _SPATIAL[k] = Spatial(parts if parts is not None else [])
    return _SPATIAL[k]


def _ribbon_index(W):
    k = (id(W), "ribbons")
    if k not in _SPATIAL:
        rs = list(W.lay.ribbons.values())
        boxes = [shapely.LineString(r.center).buffer(r.left + r.right) for r in rs]
        _SPATIAL[k] = (rs, shapely.STRtree(boxes))
    return _SPATIAL[k]


def _road_z_index(W):
    """KD-tree over ground-level ribbon centre lines -> road surface height (flat cross sections)."""
    k = (id(W), "road_z")
    if k not in _SPATIAL:
        P, Z = [], []
        for r in W.lay.ribbons.values():
            e = r.edge
            c = r.center
            s_full = profiles.arclen(e.pts)
            s_c = r.s0 + profiles.arclen(c)
            lf = np.interp(s_c, s_full, e.lift)
            ok = np.abs(lf) <= 0.3
            P.append(c[ok])
            Z.append(np.interp(s_c, s_full, e.z)[ok])
        P = np.vstack(P)
        _SPATIAL[k] = (cKDTree(P), np.concatenate(Z))
    return _SPATIAL[k]


def _cover_index(W):
    k = (id(W), "cover")
    if k not in _SPATIAL:
        polys = [p for p, _ in W.cover]
        _SPATIAL[k] = shapely.STRtree(polys)
    return _SPATIAL[k]


def _kerb_segments(road_t, box, seg_len, simplify):
    """Segments (A, B, interior_left) along the boundary of the road area inside box."""
    bx0, by0, bx1, by1 = box.bounds
    A, B, LEFT = [], [], []
    geom = road_t.simplify(simplify, preserve_topology=True) if simplify > 0 else road_t
    for poly in getattr(geom, "geoms", [geom]):
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        rings = [poly.exterior] + list(poly.interiors)
        for ri, ring in enumerate(rings):
            pts = np.asarray(ring.coords)
            if len(pts) < 3:
                continue
            ccw = shapely.LinearRing(pts).is_ccw
            interior_left = ccw if ri == 0 else not ccw
            a, b = pts[:-1], pts[1:]
            m = (a + b) / 2
            sel = (m[:, 0] >= bx0) & (m[:, 0] < bx1) & (m[:, 1] >= by0) & (m[:, 1] < by1)
            # skip artificial edges on the clip box border
            onb = ((np.abs(a[:, 0] - b[:, 0]) < 1e-6) & ((np.abs(a[:, 0] - bx0 + 2) < 1e-3) | (np.abs(a[:, 0] - bx1 - 2) < 1e-3))) | \
                  ((np.abs(a[:, 1] - b[:, 1]) < 1e-6) & ((np.abs(a[:, 1] - by0 + 2) < 1e-3) | (np.abs(a[:, 1] - by1 - 2) < 1e-3)))
            sel &= ~onb
            L = np.hypot(*(b - a).T)
            sel &= L > 0.05
            for i in np.nonzero(sel)[0]:
                nseg = max(1, int(math.ceil(L[i] / seg_len)))
                t = np.linspace(0, 1, nseg + 1)
                P = a[i] + (b[i] - a[i]) * t[:, None]
                A.append(P[:-1]); B.append(P[1:]); LEFT.append(np.full(nseg, interior_left))
    if not A:
        return None
    return np.vstack(A), np.vstack(B), np.concatenate(LEFT)


def _kerb_levels(W, A, B):
    """Bottom/top heights of the kerb wall at both segment ends.  Ribbons use their own flat
    profile, which can lie below or above the terrain membrane at the road edge: the wall spans
    both levels so there are no cracks."""
    za = H_at(W, A[:, 0], A[:, 1])
    zb = H_at(W, B[:, 0], B[:, 1])
    tree, rz = _road_z_index(W)
    da, ia = tree.query(A)
    db, ib = tree.query(B)
    zra = np.where(da < 30, rz[ia], za)
    zrb = np.where(db < 30, rz[ib], zb)
    bot_a = np.minimum(za, zra) - 0.08
    bot_b = np.minimum(zb, zrb) - 0.08
    top_a = np.maximum(za + KERB, zra + 0.02)
    top_b = np.maximum(zb + KERB, zrb + 0.02)
    road_above = (zra > za + KERB) | (zrb > zb + KERB)
    return bot_a, bot_b, top_a, top_b, road_above


def add_kerbs(mb, W, road_t, box):
    """Vertical kerb faces along the boundary of the ground road area (facing the road).
    Visual: fine outline, 4-unit segments, back faces only where the road lies above the pavement.
    Collision: simplified outline, 10-unit segments, single-sided (CCollision keeps at most 600
    triangles touching a vehicle, so the collision mesh must stay coarse)."""
    seg = _kerb_segments(road_t, box, 4.0, 0.05)
    if seg is None:
        return
    A, B, LEFT = seg
    bot_a, bot_b, top_a, top_b, road_above = _kerb_levels(W, A, B)
    n = len(A)
    V = np.zeros((n, 4, 3))
    V[:, 0, :2] = A; V[:, 0, 2] = bot_a
    V[:, 1, :2] = B; V[:, 1, 2] = bot_b
    V[:, 2, :2] = B; V[:, 2, 2] = top_b
    V[:, 3, :2] = A; V[:, 3, 2] = top_a
    L = np.hypot(*(B - A).T)
    UV = np.zeros((n, 4, 2))
    UV[:, 1, 0] = UV[:, 2, 0] = L / 4.0
    UV[:, 0, 1] = (top_a - bot_a) / 0.6
    UV[:, 1, 1] = (top_b - bot_b) / 0.6
    base = (np.arange(n) * 4)[:, None]
    Tl = np.array([[0, 2, 1], [0, 3, 2]])
    Tr = np.array([[0, 1, 2], [0, 2, 3]])
    T = np.where(LEFT[:, None, None], Tl[None], Tr[None]) + base[:, :, None]
    back = T[road_above].reshape(-1, 3)[:, [0, 2, 1]]
    T2 = np.concatenate([T.reshape(-1, 3), back])
    mb.add("bk_curb", V.reshape(-1, 3), UV.reshape(-1, 2), T2, shade=0.85, collide=False)
    seg = _kerb_segments(road_t, box, 10.0, 0.4)
    if seg is None:
        return
    A, B, LEFT = seg
    bot_a, bot_b, top_a, top_b, _ = _kerb_levels(W, A, B)
    n = len(A)
    V = np.zeros((n, 4, 3))
    V[:, 0, :2] = A; V[:, 0, 2] = bot_a
    V[:, 1, :2] = B; V[:, 1, 2] = bot_b
    V[:, 2, :2] = B; V[:, 2, 2] = top_b
    V[:, 3, :2] = A; V[:, 3, 2] = top_a
    base = (np.arange(n) * 4)[:, None]
    T = np.where(LEFT[:, None, None], Tl[None], Tr[None]) + base[:, :, None]
    mb.add_col(V.reshape(-1, 3), T.reshape(-1, 3), SURF["PAVEMENT"])


def _decimate(p3, sel, tol=0.04, max_len=15.0):
    """Indices of a centre line (x, y, z) to keep: Douglas-Peucker in 3-D plus a maximum segment
    length; the ends of every selected run are always kept (tile / structure boundaries)."""
    n = len(p3)
    keep = {0, n - 1}
    for i in range(1, n):
        if bool(sel[i - 1]) != bool(sel[min(i, len(sel) - 1)]):
            keep.add(i)

    def rec(a, b):
        if b <= a + 1:
            return
        P, Q = p3[a], p3[b]
        d = Q - P
        L = float(np.linalg.norm(d))
        seg = p3[a + 1:b]
        if L < 1e-6:
            dist = np.linalg.norm(seg - P, axis=1)
        else:
            dist = np.linalg.norm(np.cross(seg - P, d / L), axis=1)
        i = int(np.argmax(dist))
        if dist[i] > tol:
            keep.add(a + 1 + i)
            rec(a, a + 1 + i)
            rec(a + 1 + i, b)

    ks = sorted(keep)
    for a, b in zip(ks, ks[1:]):
        rec(a, b)
    ks = sorted(keep)
    s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p3[:, :2], axis=0), axis=1))]
    out = [ks[0]]
    for j in ks[1:]:
        while s[j] - s[out[-1]] > max_len:
            t = s[out[-1]] + max_len
            m = int(np.searchsorted(s, t))
            m = min(max(m, out[-1] + 1), j)
            if m == j:
                break
            out.append(m)
        out.append(j)
    return np.array(out)


def build_tile(W: World, ix: int, iy: int) -> MeshBuilder:
    from tools.build.structures import build_structures_tile
    box = tile_box(ix, iy)
    boxb = box.buffer(2.0, join_style="mitre")
    mb = MeshBuilder()
    lay = W.lay
    cx0, cy0, cx1, cy1 = box.bounds
    # ---- road ribbons (ground portions only) ----
    rs, rtree = _ribbon_index(W)
    for idx in rtree.query(box, predicate="intersects"):
        r = rs[idx]
        e = r.edge
        c = r.center
        s_full = profiles.arclen(e.pts)
        s_c = r.s0 + profiles.arclen(c)
        z = np.interp(s_c, s_full, e.z) + np.interp(s_c, s_full, e.lift)
        lf = np.abs(np.interp(s_c, s_full, e.lift))
        a, b = r.side_lines()
        tex = ROAD_TEX.get((e.lanes_f, e.lanes_b), "bk_asph")
        width = r.left + r.right
        k = np.arange(len(c) - 1)
        qc = (c[k] + c[k + 1]) / 2
        inside = (qc[:, 0] >= cx0) & (qc[:, 0] < cx1) & (qc[:, 1] >= cy0) & (qc[:, 1] < cy1)
        sel = inside & (lf[k] <= 0.3) & (lf[k + 1] <= 0.3)
        if not sel.any():
            continue
        # drop centre-line vertices where the strip stays straight and flat (visual + collision)
        keep = _decimate(np.c_[c, z], sel, tol=0.04, max_len=15.0)
        c2, a2, b2, z2, s2 = c[keep], a[keep], b[keep], z[keep], s_c[keep]
        k2 = np.arange(len(keep) - 1)
        sel2 = np.array([sel[keep[i]:keep[i + 1]].all() for i in k2]) if len(k2) else np.zeros(0, bool)
        if not sel2.any():
            continue
        V, UV, Tq = strip(a2, b2, z2, z2, s2 / max(width, 6.0))
        mb.add(tex, V, UV, Tq[np.r_[sel2, sel2]], shade=shade_terrain(W, V[:, 0], V[:, 1]), collide=False)
        keepc = _decimate(np.c_[c, z], sel, tol=0.08, max_len=24.0)
        k3 = np.arange(len(keepc) - 1)
        sel3 = np.array([sel[keepc[i]:keepc[i + 1]].all() for i in k3]) if len(k3) else np.zeros(0, bool)
        if sel3.any():
            ac, bc = a[keepc], b[keepc]
            d = ac - bc
            d = d / np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None] * COL_OVERLAP
            Vc, _, Tc = strip(ac + d, bc - d, z[keepc], z[keepc], s_c[keepc])     # under the kerbs
            mb.add_col(Vc, Tc[np.r_[sel3, sel3]], SURF["TARMAC"])
    # ---- junction polygons ----
    for j in lay.junctions.values():
        if not j.polygon.intersects(box):
            continue
        part = j.polygon.intersection(box)
        P, Tr = triangulate_area(part, 8.0)
        if len(Tr):
            z = H_at(W, P[:, 0], P[:, 1])
            S = GROUND_MATS["bk_asph"][0]
            mb.add("bk_asph", np.c_[P, z], np.c_[P[:, 0] / S, -P[:, 1] / S], Tr,
                   shade=shade_terrain(W, P[:, 0], P[:, 1]), collide=False)
            g2 = part.simplify(COL_SIMPLIFY, preserve_topology=True)
            g2 = (g2 if not g2.is_empty else part).buffer(COL_OVERLAP, join_style="mitre", mitre_limit=2.0)
            P2, T2 = triangulate_area(g2, COL_GRID)
            if len(T2):
                mb.add_col(np.c_[P2, H_at(W, P2[:, 0], P2[:, 1])], T2, SURF["TARMAC"])
    # ---- structures (bridges, ramps, trenches, tunnels) ----
    build_structures_tile(W, box, mb, ROAD_TEX)
    # ---- clipped areas ----
    road_t = spatial(W, "road_parts").clip(boxb)
    trench_t = spatial(W, "trench_parts").clip(boxb)
    add_kerbs(mb, W, road_t.difference(trench_t) if not trench_t.is_empty else road_t, box)
    taken = road_t.union(trench_t) if not trench_t.is_empty else road_t
    lowered = taken                       # road level: kerb-level collision must not grow into it
    sw = spatial(W, "sw_parts").clip(boxb).difference(taken).intersection(box)
    if not sw.is_empty:
        add_area(mb, W, sw, "bk_pave", grid=8.0, clip=lowered)
    taken = taken.union(sw)
    mn = spatial(W, "minor_parts").clip(boxb).difference(taken).intersection(box)
    if not mn.is_empty:
        add_area(mb, W, mn, "bk_asph2", grid=8.0, clip=lowered)
    taken = taken.union(mn)
    pa = spatial(W, "path_parts").clip(boxb).difference(taken).intersection(box)
    if not pa.is_empty:
        add_area(mb, W, pa, "bk_path", grid=8.0, clip=lowered)
    taken = taken.union(pa)
    # ---- land cover & default terrain ----
    rest = box.difference(taken)
    sea, lakes = W.water_polys
    water = spatial_water(W).clip(boxb)
    if not water.is_empty:
        under = rest.intersection(water)
        if not under.is_empty:
            add_area(mb, W, under, "bk_seabed", under_water=True, grid=14.0)
        rest = rest.difference(water)
        beach = rest.intersection(water.buffer(9.0)) if not sea.intersection(boxb).is_empty else shapely.Polygon()
        if not beach.is_empty:
            add_area(mb, W, beach, "bk_sand", grid=8.0, clip=lowered)
            rest = rest.difference(beach)
    tree = _cover_index(W)
    hits = sorted(tree.query(box, predicate="intersects"), reverse=True)
    for i in hits:
        if rest.is_empty:
            break
        poly, mat = W.cover[i]
        part = rest.intersection(poly)
        if part.is_empty or part.area < 1.0:
            continue
        add_area(mb, W, part, mat, grid=11.0, clip=lowered)
        rest = rest.difference(poly)
    if not rest.is_empty and rest.area > 0.5:
        add_area(mb, W, rest, "bk_soil", grid=11.0, clip=lowered)
    return mb


def spatial_water(W):
    k = (id(W), "water")
    if k not in _SPATIAL:
        sea, lakes = W.water_polys
        parts = list(getattr(sea, "geoms", [sea])) + [p for p, _ in lakes]
        _SPATIAL[k] = Spatial(parts)
    return _SPATIAL[k]
