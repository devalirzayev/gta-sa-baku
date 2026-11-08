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
    road_mask = terrain.rasterize(lay.road_area.buffer(0.5)) > 0
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
    minor_area = unary_union(minor_parts).difference(lay.road_area).buffer(0) if minor_parts else shapely.Polygon()
    path_area = unary_union(path_parts).difference(lay.road_area).difference(minor_area).buffer(0) if path_parts else shapely.Polygon()
    cover = load_cover()
    W = World(g, lay, H, T, cover, minor_area, path_area, lifted, (sea, lk))
    if cache:
        pickle.dump(W, open(path, "wb"))
    return W


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


def add_area(mb, W, geom, mat, z_off=KERB, grid=8.0, under_water=False):
    P, Tr = triangulate_area(geom, grid)
    if len(Tr) == 0:
        return
    z = H_at(W, P[:, 0], P[:, 1]) + (0.0 if under_water else z_off)
    S = GROUND_MATS[mat][0]
    UV = np.c_[P[:, 0] / S, -P[:, 1] / S]
    sh = shade_terrain(W, P[:, 0], P[:, 1])
    if under_water:
        sh = sh * 0.8
    mb.add(mat, np.c_[P, z], UV, Tr, shade=sh, surface=GROUND_MATS[mat][1])


def build_tile(W: World, ix: int, iy: int) -> MeshBuilder:
    box = tile_box(ix, iy)
    mb = MeshBuilder()
    lay = W.lay
    # ---- road ribbons (ground portions only) ----
    for r in lay.ribbons.values():
        e = r.edge
        c = r.center
        mid = c[len(c) // 2]
        cx0, cy0, cx1, cy1 = box.bounds
        if not (cx0 - 400 < mid[0] < cx1 + 400 and cy0 - 400 < mid[1] < cy1 + 400):
            continue
        s_full = profiles.arclen(e.pts)
        s_c = r.s0 + profiles.arclen(c)
        z = np.interp(s_c, s_full, e.z) + np.interp(s_c, s_full, e.lift)
        lf = np.abs(np.interp(s_c, s_full, e.lift))
        a, b = r.side_lines()
        key = (e.lanes_f, e.lanes_b)
        tex = ROAD_TEX.get(key, "bk_asph")
        width = r.left + r.right
        v = s_c / max(width, 6.0)
        V, UV, Tq = strip(a, b, z, z, v)
        # keep quads whose centre is inside this tile and which are ground (not lifted)
        n = len(c)
        k = np.arange(n - 1)
        qc = (c[k] + c[k + 1]) / 2
        inside = (qc[:, 0] >= cx0) & (qc[:, 0] < cx1) & (qc[:, 1] >= cy0) & (qc[:, 1] < cy1)
        ground = (lf[k] <= 0.3) & (lf[k + 1] <= 0.3)
        sel = inside & ground
        if not sel.any():
            continue
        Tsel = Tq[np.r_[sel, sel]]
        sh = shade_terrain(W, V[:, 0], V[:, 1])
        mb.add(tex, V, UV, Tsel, shade=sh, surface=SURF["TARMAC"])
    # ---- junction polygons ----
    for j in lay.junctions.values():
        if not j.polygon.intersects(box):
            continue
        part = j.polygon.intersection(box)
        P, Tr = triangulate_area(part, 6.0)
        if len(Tr):
            z = H_at(W, P[:, 0], P[:, 1])
            S = GROUND_MATS["bk_asph"][0]
            mb.add("bk_asph", np.c_[P, z], np.c_[P[:, 0] / S, -P[:, 1] / S], Tr,
                   shade=shade_terrain(W, P[:, 0], P[:, 1]), surface=SURF["TARMAC"])
    # ---- kerbs along the road area boundary ----
    road_t = lay.road_area.intersection(box.buffer(2.0))
    for poly in getattr(road_t, "geoms", [road_t]):
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        rings = [shapely.LineString(poly.exterior.coords)] + [shapely.LineString(h.coords) for h in poly.interiors]
        for ri, ring in enumerate(rings):
            pts = np.asarray(ring.coords)
            # shapely exterior is CCW? ensure interior on the left for exterior, right for holes
            ccw = shapely.LinearRing(pts).is_ccw
            interior_left = ccw if ri == 0 else not ccw
            for a, b in zip(pts[:-1], pts[1:]):
                m = (a + b) / 2
                if not (box.bounds[0] <= m[0] < box.bounds[2] and box.bounds[1] <= m[1] < box.bounds[3]):
                    continue
                L = float(np.hypot(*(b - a)))
                if L < 0.05:
                    continue
                nseg = max(1, int(math.ceil(L / 4.0)))
                for t in range(nseg):
                    p0 = a + (b - a) * (t / nseg)
                    p1 = a + (b - a) * ((t + 1) / nseg)
                    z0, z1 = H_at(W, np.array([p0[0], p1[0]]), np.array([p0[1], p1[1]]))
                    V, UV, Tw = wall(p0, p1, z0 - 0.05, z1 - 0.05, z0 + KERB, z1 + KERB, facing_left=interior_left,
                                     u_scale=4.0, v_scale=1.0)
                    mb.add("bk_curb", V, UV, Tw, shade=0.85, surface=SURF["PAVEMENT"])
    # ---- sidewalks, minor roads, paths ----
    taken = lay.road_area
    sw = lay.sidewalk_area.intersection(box)
    if not sw.is_empty:
        add_area(mb, W, sw, "bk_pave", grid=6.0)
    taken_sw = unary_union([taken, lay.sidewalk_area])
    mn = W.minor_area.intersection(box).difference(taken_sw)
    if not mn.is_empty:
        add_area(mb, W, mn, "bk_asph2", grid=6.0)
    taken2 = unary_union([taken_sw, W.minor_area])
    pa = W.path_area.intersection(box).difference(taken2)
    if not pa.is_empty:
        add_area(mb, W, pa, "bk_path", grid=6.0)
    taken3 = unary_union([taken2, W.path_area])
    # ---- land cover & default terrain ----
    rest = box.difference(taken3)
    sea, lakes = W.water_polys
    water = unary_union([sea] + [p for p, _ in lakes]) if lakes else sea
    under = rest.intersection(water)
    if not under.is_empty:
        add_area(mb, W, under, "bk_seabed", under_water=True, grid=10.0)
    rest = rest.difference(water)
    # beach strip along the sea
    beach = rest.intersection(sea.buffer(9.0))
    if not beach.is_empty:
        add_area(mb, W, beach, "bk_sand", grid=6.0)
        rest = rest.difference(beach)
    for poly, mat in reversed(W.cover):
        if rest.is_empty:
            break
        if not poly.intersects(box):
            continue
        part = rest.intersection(poly)
        if part.is_empty or part.area < 1.0:
            continue
        add_area(mb, W, part, mat)
        rest = rest.difference(poly)
    if not rest.is_empty and rest.area > 0.5:
        add_area(mb, W, rest, "bk_soil")
    return mb
