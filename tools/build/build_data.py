"""Game data files: water.dat, info.zon / map.zon (+ zone texts), tracks, main.scm, radar."""
from __future__ import annotations

import math
import os

import numpy as np
import pyarrow.parquet as pq
import shapely
from scipy import ndimage

from tools.gta import water as WT, zones as ZN, tracks as TR, scm as SCM, radar as RD
from tools.geo import terrain
from tools.geo.proj import lonlat_to_game, WORLD
from tools.build import ground
from tools.build.topdown import TopDown, MAT_RGB


# ------------------------------------------------------------------ water
def water_polys(W, cell=24, max_quads=290):
    T = W.T
    polys = []
    # sea: rectangles from the sea mask on a coarse grid; slightly dilated (land hides overflow)
    n = int(2 * WORLD / cell)
    sea = T.sea
    f = int(cell / terrain.CELL)
    coarse = sea[:n * f, :n * f].reshape(n, f, n, f).mean(axis=(1, 3))
    m = coarse > 0.05
    # land under a dilated water rect must be above 0: check terrain max inside the cell
    hmax = W.H[:n * f, :n * f].reshape(n, f, n, f).max(axis=(1, 3))
    m &= (hmax > -0.5) | (coarse > 0.5) | (hmax < 0.0)
    rects = WT.rects_from_mask(m, -int(WORLD), -int(WORLD), cell)
    for x0, y0, x1, y1 in rects:
        polys.append(WT.quad(x0, y0, x1, y1, 0.0, big_waves=0.25, small_waves=0.6))
    nsea = len(polys)
    # lakes (each at its own level) on a finer grid, shrunk to stay inside the raised shore ring
    for k, lvl in T.lake_levels.items():
        mask = T.lake == k
        if mask.sum() < 30:
            continue
        lc = 8
        fl = int(lc / terrain.CELL)
        nn = int(2 * WORLD / lc)
        mm = mask[:nn * fl, :nn * fl].reshape(nn, fl, nn, fl).mean(axis=(1, 3)) > 0.5
        if mm.sum() == 0:
            continue
        # keep lake quads off the world edge (the engine forces z=0 on edge vertices)
        edge = int(16 / lc) + 1
        mm[:edge, :] = mm[-edge:, :] = False
        mm[:, :edge] = mm[:, -edge:] = False
        rr = WT.rects_from_mask(mm, -int(WORLD), -int(WORLD), lc)
        if len(polys) + len(rr) > max_quads:
            continue
        z = round(float(lvl), 1)
        for x0, y0, x1, y1 in rr:
            polys.append(WT.quad(x0, y0, x1, y1, z, small_waves=0.2))
    st = WT.validate_water(polys)
    return polys, nsea, st


# ------------------------------------------------------------------ zones
ZONE_KEEP = {"locality", "neighborhood", "macrohood", "microhood"}


def zone_list(cell=200, extra_named=()):
    """Partition the map into rectangles labelled with the most specific district/neighbourhood
    polygon containing each grid cell (smallest polygon wins), merged greedily."""
    t = pq.read_table("data/raw/division_area.parquet").to_pylist()
    world = shapely.box(-WORLD, -WORLD, WORLD, WORLD)
    polys = []
    for r in t:
        if r["subtype"] not in ZONE_KEEP or r["class"] != "land":
            continue
        nm = (r["names"] or {}).get("primary")
        if not nm or "siəd" in nm or "seçki" in nm.lower():
            continue
        g = ground._to_game(shapely.from_wkb(r["geometry"])).intersection(world)
        if g.is_empty or g.area < 2000:
            continue
        nm = nm.replace(" rayonu", "").replace(" Qəsəbəsi", "").replace(" qəsəbəsi", "")
        polys.append((g.area, nm, g))
    for nm, box in extra_named:
        g = shapely.box(*box)
        polys.append((g.area, nm, g))
    polys.sort(key=lambda p: p[0])
    n = int(2 * WORLD / cell)
    lab = np.full((n, n), -1)
    cx = -WORLD + cell * (np.arange(n) + 0.5)
    pts = shapely.points(*np.meshgrid(cx, cx))
    for k, (a, nm, g) in reversed(list(enumerate(polys))):
        inside = shapely.contains(g, pts)
        lab[inside] = k                     # smaller polygons overwrite larger ones
    names = sorted({polys[k][1] for k in np.unique(lab) if k >= 0})
    keys = {nm: f"BKZ{i:03d}" for i, nm in enumerate(names)}
    texts = {keys[nm]: nm for nm in names}
    out = []
    zid = 0
    for k in np.unique(lab):
        if k < 0:
            continue
        rects = WT.rects_from_mask(lab == k, -int(WORLD), -int(WORLD), cell)
        for x0, y0, x1, y1 in rects:
            out.append(ZN.navi_zone(f"BK{zid:03d}", x0, y0, x1, y1, gxt=keys[polys[k][1]]))
            zid += 1
    texts["SAN_AND"] = "Baki"
    return out, texts


def map_zones():
    return [ZN.map_zone("BAKU", -WORLD, -WORLD, WORLD, WORLD, level=1)]


# ------------------------------------------------------------------ scm
def poi_points(cat):
    out = []
    for r in pq.read_table("data/raw/place.parquet", columns=["geometry", "taxonomy", "names"]).to_pylist():
        tax = r["taxonomy"] or {}
        if tax.get("primary") == cat:
            p = shapely.from_wkb(r["geometry"])
            x, y = lonlat_to_game(p.x, p.y)
            if abs(x) < WORLD - 60 and abs(y) < WORLD - 60:
                out.append((float(x), float(y)))
    return out


def spread(points, k):
    if not points:
        return []
    pts = np.array(points)
    chosen = [int(np.argmin(np.hypot(*(pts - pts.mean(0)).T)))]
    d = np.hypot(*(pts - pts[chosen[0]]).T)
    while len(chosen) < min(k, len(pts)):
        i = int(np.argmax(d))
        chosen.append(i)
        d = np.minimum(d, np.hypot(*(pts - pts[i]).T))
    return [tuple(pts[i]) for i in chosen]


def snap_to_sidewalk(W, x, y, cp=None):
    """Nearest point on the sidewalk area (or road) with ground z."""
    p = shapely.Point(x, y)
    box = p.buffer(80)
    sw = ground.spatial(W, "sw_parts").clip(box.envelope)
    target = sw if not sw.is_empty else ground.spatial(W, "road_parts").clip(box.envelope)
    if target.is_empty:
        q = p
    else:
        q = shapely.ops.nearest_points(target, p)[0]
    z = float(ground.H_at(W, np.array([q.x]), np.array([q.y]))[0]) + ground.KERB + 0.1
    return (q.x, q.y, z)


def make_scm(W, spawn=None):
    hosp = spread(poi_points("hospital"), 10)
    pol = spread(poi_points("police_station"), 10) or hosp[:3]
    hosp = [snap_to_sidewalk(W, x, y) + (0.0,) for x, y in hosp]
    pol = [snap_to_sidewalk(W, x, y) + (0.0,) for x, y in pol]
    if spawn is None:
        sx, sy, sz = snap_to_sidewalk(W, 1480.0, -175.0)
        # face the Heydar Aliyev Centre (ped heading: 0 = north, counter-clockwise, degrees)
        tx, ty = 1569.0, -126.0
        heading = math.degrees(math.atan2(-(tx - sx), ty - sy)) % 360.0
        spawn = (sx, sy, sz, heading)
    data = SCM.build_freeroam_scm(spawn, hosp, pol, weather=0, hour=13, money=25000,
                                  extras=[("SWITCH_RANDOM_TRAINS", 0)], max_wanted_level=6)
    return data, spawn, hosp, pol


# ------------------------------------------------------------------ radar
def radar_image(W, buildings=None, size=1536):
    td = TopDown((-WORLD, -WORLD, WORLD, WORLD), size, supersample=2, bg=(150, 140, 112))
    for poly, mat in W.cover:
        c = {"bk_grass": (106, 134, 78), "bk_grassdry": (146, 140, 100), "bk_plaza": (176, 170, 156)}.get(mat)
        if c:
            td.polygon(poly, c, clip=False)
    sea, lakes = W.water_polys
    td.polygon(sea, (111, 137, 170), clip=False)
    for p, _ in lakes:
        td.polygon(p, (111, 137, 170), clip=False)
    if buildings:
        for b in buildings:
            td.polygon(b["poly"], (122, 116, 108), clip=False)
    world = shapely.box(-WORLD, -WORLD, WORLD, WORLD)
    td.polygon(ground.spatial(W, "minor_parts").clip(world), (176, 172, 164), clip=False)
    # railway tracks (dark grey with sleepers look at radar scale)
    try:
        from tools.build import rail
        for line in rail.rail_lines():
            td.line(np.asarray(line.coords), (92, 86, 80), 6.0)
    except Exception:          # pragma: no cover - rails are optional scenery
        pass
    for e in sorted(W.g.edges.values(), key=lambda e: e.lanes_f + e.lanes_b):
        if not e.major:
            continue
        w = e.width + 2.0 * (1 + (e.cls in ("motorway", "trunk", "primary")))
        col = (64, 64, 64) if e.cls not in ("motorway", "trunk") else (60, 60, 60)
        td.line(e.pts, col, w + 3)
    for e in sorted(W.g.edges.values(), key=lambda e: e.lanes_f + e.lanes_b):
        if not e.major:
            continue
        w = e.width + 2.0 * (1 + (e.cls in ("motorway", "trunk", "primary")))
        col = (230, 230, 230) if e.cls not in ("motorway", "trunk", "primary") else (250, 235, 170)
        td.line(e.pts, col, w)
    return td.result()
