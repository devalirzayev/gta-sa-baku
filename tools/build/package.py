"""Assemble the Baku total-conversion mod from the generated parts.

    python3 -m tools.build.package
Writes:
  dist/files/      every game file in its final game-relative location (models/baku.img,
                   data/gta.dat, data/maps/baku/*.ide|*.ipl, data/water.dat, ...)
  dist/gta3img/    files that must REPLACE entries inside models/gta3.img
                   (stream IPLs, COL files, nodes*.dat, radar*.txd)
  dist/manifest.json
"""
from __future__ import annotations

import json
import math
import os
import pickle
import shutil
from collections import defaultdict

import numpy as np
import pandas as pd
import shapely

from tools.build import ground, ids, build_data as BD
from tools.geo.proj import WORLD
from tools.gta import ipl as IPL, zones as ZN, water as WT, tracks as TR, radar as RD
from tools.rw import img, col, txd, dff

DIST = os.environ.get("BAKU_DIST", "dist")
MODDIR = os.path.join(DIST, "Baku_SA")
GROUND = "build/out/ground"
SIGNS = "build/out/signs"
LANDMARKS = "build/out/landmarks"
RAIL = "build/out/rail"
CELL = 500.0
NC = int(2 * WORLD / CELL)      # 12 x 12 stream cells

# vanilla exterior stream IPL groups (text IPL name, number of streams) - data/sa/sa_ipl_slots.txt
STREAM_GROUPS = [("countn2", 9), ("countryN", 4), ("countryS", 5), ("countryE", 14), ("countryW", 9),
                 ("SFn", 3), ("SFs", 9), ("SFse", 7), ("SFe", 4), ("SFw", 6), ("vegasN", 9), ("vegasS", 6),
                 ("vegasE", 9), ("vegasW", 12), ("LAs", 6), ("LAs2", 5), ("LAe2", 7), ("LAe", 6), ("LAn", 3),
                 ("LAn2", 4), ("LAwn", 4), ("LAw", 6), ("LAw2", 5), ("LAhills", 5), ("seabed", 1)]
SPECIAL_EMPTY = ["barriers1", "barriers2", "carter", "crack", "truthsfarm"]
KEEP_IDE = ["DATA\\MAPS\\generic\\vegepart.IDE", "DATA\\MAPS\\generic\\barriers.IDE",
            "DATA\\MAPS\\generic\\dynamic.IDE", "DATA\\MAPS\\generic\\dynamic2.IDE",
            "DATA\\MAPS\\generic\\multiobj.IDE", "DATA\\MAPS\\generic\\procobj.IDE",
            "DATA\\MAPS\\interior\\int_LA.IDE", "DATA\\MAPS\\interior\\int_SF.IDE",
            "DATA\\MAPS\\interior\\int_veg.IDE", "DATA\\MAPS\\interior\\int_cont.IDE",
            "DATA\\MAPS\\leveldes\\levelmap.IDE", "DATA\\MAPS\\leveldes\\levelxre.IDE",
            "DATA\\MAPS\\interior\\gen_int1.IDE", "DATA\\MAPS\\interior\\gen_int2.IDE",
            "DATA\\MAPS\\interior\\gen_intb.IDE", "DATA\\MAPS\\interior\\gen_int3.IDE",
            "DATA\\MAPS\\interior\\gen_int4.IDE", "DATA\\MAPS\\interior\\gen_int5.IDE",
            "DATA\\MAPS\\interior\\savehous.IDE", "DATA\\MAPS\\interior\\stadint.IDE",
            "DATA\\MAPS\\interior\\props.IDE", "DATA\\MAPS\\interior\\props2.IDE",
            "DATA\\MAPS\\interior\\propext.IDE", "DATA\\MAPS\\veh_mods\\veh_mods.IDE", "DATA\\TXDCUT.IDE"]
KEEP_IPL = ["DATA\\MAPS\\interior\\int_LA.IPL", "DATA\\MAPS\\interior\\int_SF.IPL",
            "DATA\\MAPS\\interior\\int_veg.IPL", "DATA\\MAPS\\interior\\int_cont.IPL",
            "DATA\\MAPS\\interior\\gen_int1.IPL", "DATA\\MAPS\\interior\\gen_int2.IPL",
            "DATA\\MAPS\\interior\\gen_intb.IPL", "DATA\\MAPS\\interior\\gen_int3.IPL",
            "DATA\\MAPS\\interior\\gen_int4.IPL", "DATA\\MAPS\\interior\\gen_int5.IPL",
            "DATA\\MAPS\\interior\\stadint.IPL", "DATA\\MAPS\\interior\\savehous.IPL",
            "DATA\\MAPS\\leveldes\\levelmap.IPL"]
GENERIC_COLS = {'barriers.col', 'cranes_dyn2.col', 'dynamic.col', 'dynamic2.col', 'levelxre.col', 'multiobj.col',
                'procobj.col', 'propext.col', 'vegepart.col', 'veh_mods.col', 'levelmap_1.col'}
COL_REGION = 750.0
LOD_BUDGET = 2500


def stream_names():
    out = []
    for g, n in STREAM_GROUPS:
        for k in range(n):
            out.append((g, f"{g.lower()}_stream{k}"))
    return out


def gta_dat(text_ipls, img_line=True):
    L = ["#", "# Baku for GTA San Andreas - total conversion gta.dat", "#",
         "IMG DATA\\PATHS\\CARREC.IMG", "IMG DATA\\SCRIPT\\SCRIPT.IMG", "IMG MODELS\\CUTSCENE.IMG"]
    if img_line:
        L.append("IMG MODELS\\BAKU.IMG")
    L.append("SPLASH loadsc2")
    L += ["IDE " + p for p in KEEP_IDE]
    L += ["IDE DATA\\MAPS\\BAKU\\BAKU_SA.IDE", "IDE DATA\\MAPS\\BAKU\\BAKU.IDE"]
    L += ["IPL DATA\\MAPS\\BAKU\\MAP.ZON", "IPL DATA\\MAPS\\BAKU\\INFO.ZON", "SPLASH loadsc2"]
    L += ["IPL " + p for p in KEEP_IPL]
    L += ["IPL DATA\\MAPS\\BAKU\\" + t.upper() + ".IPL" for t in text_ipls]
    L += ["SPLASH loadsc4", ""]
    return "\r\n".join(L)


def main():
    W = ground.prepare(cache=True, verbose=False)
    if os.path.exists(MODDIR):
        shutil.rmtree(MODDIR)
    out_files = os.path.join(MODDIR, "files")
    out_img = os.path.join(MODDIR, "gta3img")
    for d in (out_files, out_img, os.path.join(MODDIR, "text"), os.path.join(MODDIR, "installer")):
        os.makedirs(d)
    maps = os.path.join(out_files, "data", "maps", "baku")
    os.makedirs(maps)
    models = pd.read_csv("data/sa/models.csv", low_memory=False)
    mrow = {int(r.id): r for r in models.itertuples()}
    manifest = {"text_ipls": [], "stream_ipls": {}, "cols": {}, "gta3img": []}

    # ---------------------------------------------------------------- ground tiles + LODs
    tiles = []
    for ix in range(ground.NT):
        for iy in range(ground.NT):
            p = os.path.join(GROUND, ids.tile_name(ix, iy) + ".json")
            if os.path.exists(p):
                tiles.append(json.load(open(p)))
    # one LOD per HD tile (a LOD must have exactly one child, see ids.py)
    lods = []
    for t in tiles:
        ix, iy = int(t["name"][4:6]), int(t["name"][6:8])
        lods.append(json.load(open(os.path.join(GROUND, ids.lod_name(ix, iy) + ".json"))))
    ground_insts = []
    lod_index = {}
    for k, L in enumerate(lods):
        ox, oy, oz = L["origin"]
        ground_insts.append(IPL.Inst(L["id"], L["name"], ox, oy, oz))
        lod_index[L["name"]] = k
    for t in tiles:
        ix, iy = int(t["name"][4:6]), int(t["name"][6:8])
        ox, oy, oz = t["origin"]
        ground_insts.append(IPL.Inst(t["id"], t["name"], ox, oy, oz, lod=lod_index[ids.lod_name(ix, iy)]))

    # ---------------------------------------------------------------- buildings -> cells
    blds, usage = pickle.load(open("data/cache/buildings.pkl", "rb"))
    lmk = json.load(open(os.path.join(LANDMARKS, "landmarks.json"))) if os.path.exists(os.path.join(LANDMARKS, "landmarks.json")) \
        else {"models": [], "footprints": {}}
    if lmk["footprints"]:
        lm_area = shapely.union_all([shapely.from_wkt(w).buffer(1.5) for w in lmk["footprints"].values()])
        n0 = len(blds)
        blds = [b for b in blds if b.get("poly") is None or not b["poly"].intersects(lm_area)]
        usage = defaultdict(int)
        for b in blds:
            usage[b["id"]] += 1
        manifest["landmarks_replaced_buildings"] = n0 - len(blds)
    cells = defaultdict(list)
    for b in blds:
        cx = min(NC - 1, max(0, int((b["x"] + WORLD) // CELL)))
        cy = min(NC - 1, max(0, int((b["y"] + WORLD) // CELL)))
        cells[(cx, cy)].append(b)
    names = stream_names()
    assert len(names) >= NC * NC, (len(names), NC * NC)
    cell_name = {}
    order = sorted(((cx, cy) for cx in range(NC) for cy in range(NC)))
    for (cx, cy), (grp, sname) in zip(order, names):
        cell_name[(cx, cy)] = (grp, sname)
    # LOD selection (big / tall buildings), budget
    cand = [b for b in blds if b["lod_id"] and (b["height"] >= 13 or b["area"] >= 650)]
    cand.sort(key=lambda b: -(b["height"] * 40 + b["area"]))
    lod_set = {id(b) for b in cand[:LOD_BUDGET]}
    group_lods = defaultdict(list)       # group -> list of LOD insts (text IPL)
    stream_insts = defaultdict(list)
    for key, lst in cells.items():
        grp, sname = cell_name[key]
        for b in lst:
            lod = -1
            if id(b) in lod_set:
                lm = mrow[b["lod_id"]]
                group_lods[grp].append(IPL.Inst(b["lod_id"], lm.name, b["x"], b["y"], b["z"], b["heading"]))
                lod = len(group_lods[grp]) - 1
            stream_insts[sname].append(IPL.Inst(b["id"], b["name"], b["x"], b["y"], b["z"], b["heading"], lod=lod))
    # ---------------------------------------------------------------- props (lamps, lights, trees, ...)
    props = pickle.load(open("data/cache/props.pkl", "rb")) if os.path.exists("data/cache/props.pkl") else []
    keep_ide = {p.upper() for p in KEEP_IDE}
    sg = json.load(open(os.path.join(SIGNS, "signs.json"))) if os.path.exists(os.path.join(SIGNS, "signs.json")) \
        else {"road_signs": [], "tiles": []}
    sign_ids = {r["id"] for r in sg["road_signs"]}
    for p in props:
        if p["id"] not in sign_ids:
            r = mrow[p["id"]]
            assert str(r.ide_file).upper() in keep_ide, (p["id"], r.ide_file)   # defined by a kept vanilla IDE
        cx = min(NC - 1, max(0, int((p["x"] + WORLD) // CELL)))
        cy = min(NC - 1, max(0, int((p["y"] + WORLD) // CELL)))
        stream_insts[cell_name[(cx, cy)][1]].append(IPL.Inst(p["id"], p["name"], p["x"], p["y"], p["z"], p["heading"]))
    manifest["props"] = {k: sum(1 for p in props if p["kind"] == k) for k in sorted({p["kind"] for p in props})}
    # merged signage tiles (shop boards, street plates, zebra crossings): static, one per ground tile
    for t in sg["tiles"]:
        ox, oy, oz = t["origin"]
        cx = min(NC - 1, max(0, int((ox + WORLD) // CELL)))
        cy = min(NC - 1, max(0, int((oy + WORLD) // CELL)))
        stream_insts[cell_name[(cx, cy)][1]].append(IPL.Inst(t["id"], t["name"], ox, oy, oz, 0.0))
    manifest["sign_tiles"] = len(sg["tiles"])
    for m in lmk["models"]:
        ox, oy, oz = m["origin"]
        cx = min(NC - 1, max(0, int((ox + WORLD) // CELL)))
        cy = min(NC - 1, max(0, int((oy + WORLD) // CELL)))
        grp, sname = cell_name[(cx, cy)]
        group_lods[grp].append(IPL.Inst(m["lod_id"], m["lod_name"], ox, oy, oz, 0.0))
        stream_insts[sname].append(IPL.Inst(m["id"], m["name"], ox, oy, oz, 0.0, lod=len(group_lods[grp]) - 1))
    manifest["landmarks"] = [m["name"] for m in lmk["models"]]
    rl = json.load(open(os.path.join(RAIL, "rail.json"))) if os.path.exists(os.path.join(RAIL, "rail.json")) else {"tiles": []}
    for t in rl["tiles"]:
        ox, oy, oz = t["origin"]
        cx = min(NC - 1, max(0, int((ox + WORLD) // CELL)))
        cy = min(NC - 1, max(0, int((oy + WORLD) // CELL)))
        stream_insts[cell_name[(cx, cy)][1]].append(IPL.Inst(t["id"], t["name"], ox, oy, oz, 0.0))
    manifest["rail_tiles"] = len(rl["tiles"])
    # text IPLs: ground (baku.ipl) + one per vanilla group name
    text_ipls = ["baku"]
    with open(os.path.join(maps, "baku.ipl"), "w", newline="") as f:
        f.write(IPL.text_ipl(ground_insts, comment="Baku map - ground tiles and ground LODs"))
    for grp, _n in STREAM_GROUPS:
        text_ipls.append(grp)
        with open(os.path.join(maps, grp + ".ipl"), "w", newline="") as f:
            f.write(IPL.text_ipl(group_lods.get(grp, []),
                                 comment=f"Baku map - LOD instances for the {grp.lower()}_stream* cells"))
    manifest["text_ipls"] = text_ipls
    # parked cars (car generators) per cell
    stream_cars = defaultdict(list)
    if os.path.exists("data/cache/parked_cars.pkl"):
        for c in pickle.load(open("data/cache/parked_cars.pkl", "rb")):
            cx = min(NC - 1, max(0, int((c.x + WORLD) // CELL)))
            cy = min(NC - 1, max(0, int((c.y + WORLD) // CELL)))
            stream_cars[cell_name[(cx, cy)][1]].append(c)
    manifest["parked_car_generators"] = sum(len(v) for v in stream_cars.values())
    # binary stream IPLs (all vanilla exterior names + special ones -> replace inside gta3.img)
    for grp, sname in names:
        data = IPL.binary_ipl(stream_insts.get(sname, []), stream_cars.get(sname, []))
        with open(os.path.join(out_img, sname + ".ipl"), "wb") as f:
            f.write(data)
        manifest["stream_ipls"][sname] = len(stream_insts.get(sname, []))
    for sname in SPECIAL_EMPTY:
        with open(os.path.join(out_img, sname + ".ipl"), "wb") as f:
            f.write(IPL.binary_ipl([]))
        manifest["stream_ipls"][sname] = 0

    # ---------------------------------------------------------------- IDEs
    objs = []
    for t in tiles:
        objs.append(IPL.ObjDef(t["id"], t["name"], ids.GROUND_TXD, 299, 0))
    for L in lods:
        objs.append(IPL.ObjDef(L["id"], L["name"], L["txd"], 2900, 0))
    for r in sg["road_signs"]:
        objs.append(IPL.ObjDef(r["id"], r["name"], r["txd"], r["dd"], 0))
    for t in sg["tiles"]:
        objs.append(IPL.ObjDef(t["id"], t["name"], t["txd"], t["dd"], 0))
    for m in lmk["models"]:
        objs.append(IPL.ObjDef(m["id"], m["name"], m["txd"], 299, 0))
        objs.append(IPL.ObjDef(m["lod_id"], m["lod_name"], m["txd"], 1800, 0))
    for t in rl["tiles"]:
        objs.append(IPL.ObjDef(t["id"], t["name"], t["txd"], t["dd"], 0))
    with open(os.path.join(maps, "baku.ide"), "w", newline="") as f:
        f.write(IPL.ide_text(objs, comment="Baku map - custom models (ground tiles, ground LODs, road signs, "
                                           "shop boards / street plates / zebra crossings per tile)"))
    sa_ids = set(usage.keys()) | {b["lod_id"] for b in blds if b["lod_id"] and id(b) in lod_set}
    sa_objs = []
    for mid in sorted(sa_ids):
        r = mrow[mid]
        assert r.section == "objs" and int(r.timed or 0) == 0, (mid, r.section)
        sa_objs.append(IPL.ObjDef(mid, r.name, r.txd, float(r.draw_distance), int(r.flags)))
    with open(os.path.join(maps, "baku_sa.ide"), "w", newline="") as f:
        f.write(IPL.ide_text(sa_objs, comment="Original San Andreas building models used by the Baku map\n"
                                              "(definitions copied from the vanilla IDE files; models stay in gta3.img)"))

    # ---------------------------------------------------------------- COL (replace free vanilla col files)
    cols = json.load(open("data/sa/col_contents.json"))
    id2name = {int(r.id): str(r.name).lower() for r in models.itertuples()}
    used_names = {id2name[i] for i in sa_ids if i in id2name}
    free = sorted(c for c, nm in cols.items()
                  if c not in GENERIC_COLS and not any(n.lower() in used_names for n in nm))
    ncr = int(2 * WORLD / COL_REGION)
    regions = defaultdict(list)
    for t in tiles:
        ox, oy, _ = t["origin"]
        regions[(int((ox + WORLD) // COL_REGION), int((oy + WORLD) // COL_REGION))].append(t)
    assert len(regions) <= len(free), (len(regions), len(free))
    # Every placed model needs a CColModel: CWorld::Add -> CEntity::GetBoundRect dereferences it.
    # A LOD with several children does not inherit one in LinkLods (0x5B51E0), so the ground LOD
    # tiles get bounds-only COL3 entries (no volumes -> SetUsesCollision(false)).
    lod_cols = defaultdict(list)
    for L in lods:
        g = dff.read_dff(open(os.path.join(GROUND, L["name"] + ".dff"), "rb").read())["geometries"][0]
        V = np.asarray(g["vertices"], float)
        ox, oy, _ = L["origin"]
        key = (int((ox + WORLD) // COL_REGION), int((oy + WORLD) // COL_REGION))
        lod_cols[key].append(col.write_col3(col.ColModel(L["name"], L["id"], bounds=(V.min(0), V.max(0)))))
    # signage tiles: bounds-only entries in their region file as well
    sg_cols = defaultdict(list)
    for t in sg["tiles"]:
        ox, oy, _ = t["origin"]
        key = (int((ox + WORLD) // COL_REGION), int((oy + WORLD) // COL_REGION))
        sg_cols[key].append((t["name"], open(os.path.join(SIGNS, t["name"] + ".col"), "rb").read()))
    for m in lmk["models"]:
        ox, oy, _ = m["origin"]
        key = (int((ox + WORLD) // COL_REGION), int((oy + WORLD) // COL_REGION))
        for nm in (m["name"], m["lod_name"]):
            sg_cols[key].append((nm, open(os.path.join(LANDMARKS, nm + ".col"), "rb").read()))
    for t in rl["tiles"]:
        ox, oy, _ = t["origin"]
        key = (int((ox + WORLD) // COL_REGION), int((oy + WORLD) // COL_REGION))
        sg_cols[key].append((t["name"], open(os.path.join(RAIL, t["name"] + ".col"), "rb").read()))
    keys = sorted(set(regions) | set(lod_cols) | set(sg_cols))
    assert len(keys) + 1 <= len(free), (len(keys), len(free))
    for k, key in enumerate(keys):
        cname = free[k]
        blob = b"".join(open(os.path.join(GROUND, t["name"] + ".col"), "rb").read() for t in regions.get(key, []))
        blob += b"".join(lod_cols.get(key, []))
        blob += b"".join(c for _, c in sg_cols.get(key, []))
        with open(os.path.join(out_img, cname), "wb") as f:
            f.write(blob)
        manifest["cols"][cname] = [t["name"] for t in regions.get(key, [])] + \
            [f"(bounds) {L['name']}" for L in lods
             if (int((L['origin'][0] + WORLD) // COL_REGION), int((L['origin'][1] + WORLD) // COL_REGION)) == key] + \
            [f"(bounds) {n}" for n, _ in sg_cols.get(key, [])]
    # road sign types (used all over the map) in one dedicated col file
    if sg["road_signs"]:
        cname = free[len(keys)]
        with open(os.path.join(out_img, cname), "wb") as f:
            f.write(b"".join(open(os.path.join(SIGNS, r["name"] + ".col"), "rb").read() for r in sg["road_signs"]))
        manifest["cols"][cname] = [r["name"] for r in sg["road_signs"]]

    # ---------------------------------------------------------------- baku.img
    files = []
    for t in tiles:
        files.append((t["name"] + ".dff", open(os.path.join(GROUND, t["name"] + ".dff"), "rb").read()))
    for L in lods:
        files.append((L["name"] + ".dff", open(os.path.join(GROUND, L["name"] + ".dff"), "rb").read()))
    for tname in sorted({L["txd"] for L in lods}):          # shared by 2 x 2 LOD tiles
        files.append((tname + ".txd", open(os.path.join(GROUND, tname + ".txd"), "rb").read()))
    files.append((ids.GROUND_TXD + ".txd", open(os.path.join(GROUND, ids.GROUND_TXD + ".txd"), "rb").read()))
    for r in sg["road_signs"]:
        files.append((r["name"] + ".dff", open(os.path.join(SIGNS, r["name"] + ".dff"), "rb").read()))
    if sg["road_signs"]:
        files.append((sg["road_signs"][0]["txd"] + ".txd", open(os.path.join(SIGNS, sg["road_signs"][0]["txd"] + ".txd"), "rb").read()))
    for t in sg["tiles"]:
        files.append((t["name"] + ".dff", open(os.path.join(SIGNS, t["name"] + ".dff"), "rb").read()))
        files.append((t["txd"] + ".txd", open(os.path.join(SIGNS, t["txd"] + ".txd"), "rb").read()))
    for m in lmk["models"]:
        for nm in (m["name"], m["lod_name"]):
            files.append((nm + ".dff", open(os.path.join(LANDMARKS, nm + ".dff"), "rb").read()))
    for t in rl["tiles"]:
        files.append((t["name"] + ".dff", open(os.path.join(RAIL, t["name"] + ".dff"), "rb").read()))
    if rl["tiles"]:
        files.append((rl["tiles"][0]["txd"] + ".txd", open(os.path.join(RAIL, rl["tiles"][0]["txd"] + ".txd"), "rb").read()))
    if lmk["models"]:
        tx = lmk["models"][0]["txd"]
        files.append((tx + ".txd", open(os.path.join(LANDMARKS, tx + ".txd"), "rb").read()))
    os.makedirs(os.path.join(out_files, "models"), exist_ok=True)
    img.write_img(os.path.join(out_files, "models", "baku.img"), files)

    # ---------------------------------------------------------------- paths
    pdir = "build/out/paths"
    for k in range(64):
        shutil.copy(os.path.join(pdir, f"nodes{k}.dat"), os.path.join(out_img, f"nodes{k}.dat"))
    os.makedirs(os.path.join(out_files, "data", "paths"), exist_ok=True)
    shutil.copy(os.path.join(pdir, "roadblox.dat"), os.path.join(out_files, "data", "paths", "roadblox.dat"))

    # ---------------------------------------------------------------- data files
    dd = os.path.join(out_files, "data")
    polys, nsea, st = BD.water_polys(W)
    WT.write_water_dat(os.path.join(dd, "water.dat"), polys)
    WT.write_water_dat(os.path.join(dd, "water1.dat"), polys)
    extra = [("Heydər Əliyev Mərkəzi", (1480, -260, 1680, -40)),
             ("Dənizkənarı Bulvar", (-200, -2950, 1000, -2450))]
    zs, texts = BD.zone_list(extra_named=extra)
    ZN.write_zon(os.path.join(maps, "info.zon"), zs)
    ZN.write_zon(os.path.join(maps, "map.zon"), BD.map_zones())
    with open(os.path.join(MODDIR, "text", "baku_zones.fxt"), "wb") as f:
        f.write(ZN.format_fxt(texts))
    with open(os.path.join(MODDIR, "text", "zone_texts.json"), "w", encoding="utf-8") as f:
        json.dump({k: ZN.encode_sa(v).hex() for k, v in texts.items()}, f, indent=1)
    manifest["zone_texts"] = texts
    # CTrain::InitTrains reads data\paths\tracks*.dat (Train.cpp) - dummy loops far outside the world
    for name, text in TR.no_train_files().items():
        with open(os.path.join(dd, "paths", name), "w", newline="") as f:
            f.write(text)
    scm, spawn, hosp, pol = BD.make_scm(W)
    os.makedirs(os.path.join(dd, "script"), exist_ok=True)
    with open(os.path.join(dd, "script", "main.scm"), "wb") as f:
        f.write(scm)
    manifest["spawn"] = spawn
    manifest["hospitals"] = hosp
    manifest["police"] = pol
    with open(os.path.join(dd, "gta.dat"), "w", newline="") as f:
        f.write(gta_dat(text_ipls))
    # radar
    radar = BD.radar_image(W, blds + [{"poly": shapely.from_wkt(w)} for w in lmk["footprints"].values()])
    radar.save(os.path.join(DIST, "radar_full.png"))
    for name, data in RD.build_radar_txds(RD.split_radar_image(radar, 128)):
        with open(os.path.join(out_img, name), "wb") as f:
            f.write(data)
    manifest["gta3img"] = sorted(os.listdir(out_img))
    # kept vanilla interior IPLs whose enex sections the installer removes (SA-coordinate markers)
    with open(os.path.join(MODDIR, "text", "strip_enex.txt"), "w", newline="\r\n") as f:
        for ipl_path in KEEP_IPL:
            f.write(ipl_path.replace("\\", "/").lower() + "\n")
    if os.path.exists(os.path.join(SIGNS, "object_dat_add.txt")):
        shutil.copy(os.path.join(SIGNS, "object_dat_add.txt"), os.path.join(MODDIR, "text", "object_dat_add.txt"))
    for fn in ("install.ps1", "baku_install.py"):
        shutil.copy(os.path.join("tools", "install", fn), os.path.join(MODDIR, "installer", fn))
    for fn in ("INSTALL.bat", "UNINSTALL.bat", "README_AZ.txt", "README_EN.txt"):
        src_fn = os.path.join("tools", "install", fn)
        if os.path.exists(src_fn):
            shutil.copy(src_fn, os.path.join(MODDIR, fn))
    with open(os.path.join(DIST, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False, default=float)
    print("package done:", len(files), "files in baku.img;", len(manifest["gta3img"]), "gta3.img replacements;",
          sum(manifest["stream_ipls"].values()), "streamed instances;", sum(len(v) for v in group_lods.values()),
          "building LODs")


if __name__ == "__main__":
    main()
