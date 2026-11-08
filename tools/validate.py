"""Consistency + engine-limit validator for the packaged mod (dist/).

    python3 -m tools.validate [dist/Baku_SA]
Re-parses every generated file with the project readers and checks cross references and the
limits documented in docs/notes/*.md. Exit code 1 on any error.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

from tools.rw import img, dff, col, txd
from tools.gta import ipl as IPL, water as WT, zones as ZN, paths as PT

ERR, WARN = [], []
# kept vanilla text IPLs (package.KEEP_IPL), counted from the vanilla files: int_veg 42, int_cont 8,
# gen_int1 206, stadint 76, levelmap 53 instances; the other kept interior IPLs have none
VANILLA_KEPT_TEXT_INST = 385
VANILLA_KEPT_TEXT_IPLS_WITH_INST = 5


def err(m):
    ERR.append(m)


def warn(m):
    WARN.append(m)


def parse_ide(text):
    out = []
    sec = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if sec is None:
            sec = s.lower()
            continue
        if s.lower() == "end":
            sec = None
            continue
        if sec == "objs":
            p = [t.strip() for t in s.split(",")]
            out.append((int(p[0]), p[1], p[2], float(p[3]), int(p[4])))
    return out


def main(dist="dist/Baku_SA"):
    F = os.path.join(dist, "files")
    G = os.path.join(dist, "gta3img")
    models = pd.read_csv("data/sa/models.csv", low_memory=False)
    summ = pd.read_csv("data/sa/ide_summary.csv")
    # ------------------------------------------------------------ gta.dat
    gdat = open(os.path.join(F, "data", "gta.dat")).read().splitlines()
    ides = [l.split(None, 1)[1].strip() for l in gdat if l.startswith("IDE ")]
    ipls = [l.split(None, 1)[1].strip() for l in gdat if l.startswith("IPL ")]
    imgs = [l.split(None, 1)[1].strip() for l in gdat if l.startswith("IMG ")]
    if len(imgs) + 3 > 8:
        err(f"too many IMG archives ({len(imgs)} + 3 default > 8)")
    kept_vanilla = [i for i in ides if "\\BAKU\\" not in i.upper()]
    slots = 0
    vanilla_ids = set()
    for v in kept_vanilla:
        row = summ[summ.ide_file.str.upper() == v.upper()]
        if row.empty:
            err(f"gta.dat IDE {v} not in vanilla summary")
            continue
        slots += int(row.atomic_slots.iloc[0])
        vanilla_ids |= set(models[models.ide_file.str.upper() == v.upper()].id.astype(int))
    for v in ("DATA\\DEFAULT.IDE", "DATA\\VEHICLES.IDE", "DATA\\PEDS.IDE"):
        vanilla_ids |= set(models[models.ide_file.str.upper() == v.upper()].id.astype(int))
        slots += int(summ[summ.ide_file.str.upper() == v.upper()].atomic_slots.iloc[0])
    # ------------------------------------------------------------ our IDEs
    defs = {}
    for name in ("baku_sa.ide", "baku.ide"):
        for mid, nm, tx, ddist, fl in parse_ide(open(os.path.join(F, "data", "maps", "baku", name)).read()):
            if mid in defs or mid in vanilla_ids:
                err(f"model id {mid} defined twice ({nm})")
            if len(nm) > 23 or len(tx) > 23:
                err(f"name too long {nm} / {tx}")
            if not (0 <= mid < 20000):
                err(f"id out of range {mid}")
            defs[mid] = (nm, tx, ddist, fl, name)
            slots += 1
    names_lower = Counter(v[0].lower() for v in defs.values())
    for n, c in names_lower.items():
        if c > 1:
            err(f"model name {n} defined {c} times")
    if slots > 14000:
        err(f"atomic model info slots {slots} > 14000")
    print(f"IDE: {len(defs)} definitions in our IDEs, atomic slots used ~{slots}/14000")
    # ------------------------------------------------------------ baku.img
    entries = img.read_img(os.path.join(F, "models", "baku.img"))
    ent = {}
    for n, d in entries:
        if n.lower() in ent:
            err(f"duplicate IMG entry {n}")
        if len(n) > 23:
            err(f"IMG name too long {n}")
        ent[n.lower()] = d
    txd_tex = {}
    for n, d in ent.items():
        if n.endswith(".txd"):
            t = txd.read_txd(d.rstrip(b"\0") if False else _trim_rw(d))
            txd_tex[n[:-4]] = {x["name"].lower() for x in t}
    ndff = 0
    for mid, (nm, tx, ddist, fl, src) in defs.items():
        if src != "baku.ide":
            continue
        d = ent.get(nm.lower() + ".dff")
        if d is None:
            err(f"{nm}.dff missing from baku.img")
            continue
        r = dff.read_dff(_trim_rw(d))
        ndff += 1
        if len(r["atomics"]) != 1:
            err(f"{nm}: {len(r['atomics'])} atomics")
        if r["frames"][0].get("name", "").lower() != nm.lower():
            err(f"{nm}: frame name {r['frames'][0].get('name')}")
        if tx.lower() not in txd_tex:
            err(f"{nm}: txd {tx} not in baku.img")
            continue
        for m in r["geometries"][0]["materials"]:
            if m["texture"] and m["texture"].lower() not in txd_tex[tx.lower()]:
                err(f"{nm}: texture {m['texture']} not in {tx}.txd")
        if ddist >= 300 and src == "baku.ide" and nm.startswith("bk_t"):
            err(f"{nm}: HD tile draw distance {ddist} >= 300 (would become a BIG building without collision)")
    print(f"IMG: {len(ent)} entries; {ndff} custom DFFs parsed; {len(txd_tex)} TXDs")
    # ------------------------------------------------------------ COL replacements
    col_models = {}
    for fn in os.listdir(G):
        if fn.endswith(".col"):
            for m in col.read_col(open(os.path.join(G, fn), "rb").read()):
                if m["name"].lower() in col_models:
                    err(f"collision {m['name']} in two col files")
                col_models[m["name"].lower()] = (fn, m)
                if "vertices" in m and np.abs(m["vertices"]).max() > 255.9:
                    err(f"{m['name']}: collision vertex beyond 256")
                if m.get("groups") and len(m["groups"]) > 1200:
                    err(f"{m['name']}: {len(m['groups'])} face groups")
    for mid, (nm, tx, ddist, fl, src) in defs.items():
        if src == "baku.ide":
            # every custom model needs a CColModel (CEntity::GetBoundRect dereferences it), at least bounds
            if nm.lower() not in col_models:
                err(f"{nm}: no collision model")
            elif col_models[nm.lower()][1]["model_id"] != mid:
                err(f"{nm}: col model id {col_models[nm.lower()][1]['model_id']} != {mid}")
    # vanilla models keep the collision entries of the vanilla col files we do not replace
    vanilla_cols = json.load(open("data/sa/col_contents.json"))
    replaced = {f.lower() for f in os.listdir(G) if f.endswith(".col")}
    vanilla_col_names = {n.lower() for f, names in vanilla_cols.items() if f.lower() not in replaced for n in names}
    print(f"COL: {len(col_models)} collision models in {sum(1 for f in os.listdir(G) if f.endswith('.col'))} replaced col files")
    # ------------------------------------------------------------ IPLs
    allowed = set(defs) | vanilla_ids
    text_inst = {}
    for p in ipls:
        if "\\BAKU\\" not in p.upper():
            continue
        fn = os.path.join(F, "data", "maps", "baku", p.split("\\")[-1])
        real = [x for x in os.listdir(os.path.dirname(fn)) if x.lower() == os.path.basename(fn).lower()]
        if not real:
            err(f"gta.dat IPL {p} missing")
            continue
        fn = os.path.join(os.path.dirname(fn), real[0])
        if fn.lower().endswith(".zon"):
            continue
        insts = IPL.read_text_ipl(open(fn).read())
        stem = os.path.splitext(real[0])[0].lower()
        text_inst[stem] = insts
        for k, i in enumerate(insts):
            if i.model_id not in allowed:
                err(f"{real[0]}: undefined model {i.model_id}")
            if i.lod >= len(insts) or (i.lod >= 0 and i.lod == k):
                err(f"{real[0]}: bad lod index {i.lod}")
            if abs(i.x) > 3000 or abs(i.y) > 3000:
                err(f"{real[0]}: instance outside world {i.x:.0f},{i.y:.0f}")
    n_text = sum(len(v) for v in text_inst.values())
    dyn_ids = {int(r.id) for r in models.itertuples()
               if isinstance(r.engine_refs, str) and "object.dat" in r.engine_refs}
    # our own breakable models (lines the installer inserts into data/object.dat)
    od = os.path.join(dist, "text", "object_dat_add.txt")
    if os.path.exists(od):
        by_name = {nm.lower(): mid for mid, (nm, *_r) in defs.items()}
        for line in open(od):
            if line.strip() and not line.startswith(";"):
                nm = line.split(",")[0].strip().lower()
                if nm not in by_name:
                    err(f"object_dat_add.txt: {nm} is not defined in our IDEs")
                else:
                    dyn_ids.add(by_name[nm])
    n_text_dyn = sum(1 for v in text_inst.values() for i in v if i.model_id in dyn_ids)
    stream_counts = {}
    dyn_xy = []
    stream_by_grp = {}
    stream_files = {}
    for fn in os.listdir(G):
        if not fn.endswith(".ipl"):
            continue
        insts, cars = IPL.read_binary_ipl(open(os.path.join(G, fn), "rb").read())
        stem = fn[:-4].lower()
        grp = stem.split("_stream")[0] if "_stream" in stem else None
        parent = text_inst.get(grp, []) if grp else []
        for i in insts:
            if i.model_id not in allowed:
                err(f"{fn}: undefined model {i.model_id}")
            if i.lod >= len(parent):
                err(f"{fn}: lod index {i.lod} >= {len(parent)} ({grp})")
            if abs(i.x) > 3000 or abs(i.y) > 3000:
                err(f"{fn}: instance outside world")
                continue
            if i.model_id in dyn_ids:
                dyn_xy.append((i.x, i.y))
        stream_counts[stem] = len(insts)
        stream_by_grp.setdefault(grp, []).extend(insts)
        stream_files[stem] = (insts, cars)
        for c in cars:
            if abs(c.x) > 2995 or abs(c.y) > 2995:
                err(f"{fn}: car generator outside the world")
                continue
            if not (-7.0 < c.angle < 7.0):
                err(f"{fn}: car generator angle {c.angle} is not in radians")
            if c.model_id != -1 and not (400 <= c.model_id <= 611):
                err(f"{fn}: car generator model {c.model_id}")
        if len(stem) > 17:
            err(f"IPL slot name too long {stem}")
    # collision model available for every placed instance (own entry, or a LOD with exactly one child)
    id_name = {int(r.id): str(r.name).lower() for r in models.itertuples()}
    for mid, (nm, *_rest) in defs.items():
        id_name[mid] = nm.lower()
    children = Counter()
    for stem, insts in text_inst.items():
        for i in insts:
            if i.lod >= 0:
                children[(stem, i.lod)] += 1
    for grp, insts in stream_by_grp.items():
        for i in insts:
            if i.lod >= 0:
                children[(grp, i.lod)] += 1

    def has_col(mid):
        nm = id_name.get(mid, "")
        return nm in col_models or nm in vanilla_col_names
    nocol = Counter()
    for stem, insts in text_inst.items():
        for k, i in enumerate(insts):
            if not has_col(i.model_id) and children[(stem, k)] != 1:
                nocol[id_name.get(i.model_id, str(i.model_id))] += 1
    for grp, insts in stream_by_grp.items():
        for i in insts:
            if not has_col(i.model_id):
                nocol[id_name.get(i.model_id, str(i.model_id))] += 1
    for nm, n in nocol.most_common(20):
        err(f"model {nm} placed {n}x without a collision model (crash in CWorld::Add)")
    # LinkLods: a LOD with several children keeps no collision and unlinks children that do not own
    # their col model (FileLoader.cpp 0x5B5293) -> one child per LOD
    multi = [(s, k, n) for (s, k), n in children.items() if n > 1]
    for s, k, n in multi[:10]:
        err(f"{s}.ipl: LOD #{k} has {n} children (LinkLods needs exactly one)")
    # LoadScene keeps the text instances + every LOD-linked stream instance of its related IPLs in
    # gpLoadedBuildings[4096] (FileLoader.cpp 0x5B5180, IplStore.cpp SetupRelatedIpls)
    for stem, insts in text_inst.items():
        n_rel = sum(1 for i in stream_by_grp.get(stem, []) if i.lod >= 0)
        if len(insts) + n_rel > 4096:
            err(f"{stem}.ipl: {len(insts)} instances + {n_rel} LOD-linked stream instances > gpLoadedBuildings[4096]")
    # every text IPL with instances takes one of IplEntityIndexArrays[40] (CIplStore::GetNewIplEntityIndexArray)
    n_arrays = sum(1 for v in text_inst.values() if v) + VANILLA_KEPT_TEXT_IPLS_WITH_INST
    print(f"text IPLs with instances: {n_arrays}/40 IplEntityIndexArrays")
    if n_arrays > 40:
        err(f"{n_arrays} text IPLs with instances > IplEntityIndexArrays[40]")

    # ------------------------------------------------------------ streaming: what is loaded at once
    mrow = models.set_index("id")

    def aabb(mid):
        nm = id_name.get(mid, "")
        if nm in col_models:
            m = col_models[nm][1]
            return m["min"], m["max"]
        if mid in mrow.index and not pd.isna(mrow.at[mid, "aabb_min_x"]):
            r = mrow.loc[mid]
            return ((r.aabb_min_x, r.aabb_min_y, r.aabb_min_z), (r.aabb_max_x, r.aabb_max_y, r.aabb_max_z))
        return (-5.0, -5.0, -5.0), (5.0, 5.0, 5.0)

    def ddist(mid):
        if mid in defs:
            return defs[mid][2]
        return float(mrow.at[mid, "draw_distance"]) if mid in mrow.index else 100.0

    def is_big(insts, lod_children=None):
        # LinkLods: SetupBigBuilding when the entity has LOD children or draw distance > 300
        # (m_fLODDistMultiplier is 1 while the level loads)
        out = np.array([ddist(i.model_id) > 300.0 for i in insts], bool)
        if lod_children is not None:
            out |= lod_children
        return out

    def single_nodes(insts, rects, big):
        sta = np.array([i.model_id not in dyn_ids for i in insts], bool)
        n_norm = _sectors(rects[sta & ~big]).sum()
        n_big = _sectors(rects[sta & big], size=200.0, n=30).sum()
        return int(n_norm + n_big)

    ipl_stats = {}
    for stem, (insts, cars) in stream_files.items():
        if not insts and not cars:
            continue
        rects = _bound_rects(insts, aabb)
        if len(rects):
            bb = [rects[:, 0].min(), rects[:, 1].min(), rects[:, 2].max(), rects[:, 3].max()]
        else:
            xy = np.array([(c.x, c.y) for c in cars])
            bb = [*xy.min(0), *xy.max(0)]
        large = any(ddist(i.model_id) > 150.0 for i in insts)
        g = 350.0 if large else 200.0
        bb = (bb[0] - g, bb[1] - g, bb[2] + g, bb[3] + g)
        dyn = np.array([i.model_id in dyn_ids for i in insts], bool)
        big = is_big(insts)
        ipl_stats[stem] = (bb, {"static": int((~dyn).sum()), "dynamic": int(dyn.sum()),
                                "dyn_nodes": int(_sectors(rects[dyn]).sum()) if len(rects) else 0,
                                "single": single_nodes(insts, rects, big) if len(rects) else 0,
                                "cars": len(cars)})
    peaks = stream_load_peaks(ipl_stats)
    pk = {k: v[0] for k, v in peaks.items()}
    ncell = len(peaks.get("static", (0, 0, []))[2])
    print(f"streaming: up to {ncell} stream IPLs loaded at once (bounding boxes +350/+200)")
    # permanent text instances (ours + the kept vanilla interior / levelmap IPLs)
    text_static = n_text - n_text_dyn + VANILLA_KEPT_TEXT_INST
    pool = text_static + pk.get("static", 0)
    print(f"IPL: {n_text} text instances, {sum(stream_counts.values())} streamed in {len(stream_counts)} binary IPLs; "
          f"peak {pk.get('static', 0)} streamed static -> building pool {pool}/13000")
    if pool > 12000:
        err(f"building pool {pool} too close to 13000 at {peaks['static'][1]}")
    # dynamic (object.dat) instances -> CDummyObject: dummy pool 2500; double-link sector nodes 3200
    # shared with the vehicle / ped / object repeat sectors; CObjects within 80 units: object pool 350
    text_dyn_nodes = sum(int(_sectors(_bound_rects([i for i in v if i.model_id in dyn_ids], aabb)).sum())
                         for v in text_inst.values())
    dummies = n_text_dyn + pk.get("dynamic", 0) + 60
    dbl = text_dyn_nodes + pk.get("dyn_nodes", 0) + 60 + 700
    near = 0
    if dyn_xy:
        from scipy.spatial import cKDTree
        t = cKDTree(np.array(dyn_xy))
        gpts = np.mgrid[-3000:3000:20, -3000:3000:20].reshape(2, -1).T
        near = int(max(len(x) for x in t.query_ball_point(gpts, 100.0)))
    print(f"dynamic objects: {len(dyn_xy)} streamed, peak {pk.get('dynamic', 0)} loaded -> dummy pool {dummies}/2500, "
          f"double-link nodes {dbl}/3200 (incl. 700 for vehicles/peds/objects); max within 100 units {near} "
          f"(object pool 350)")
    if dummies > 2300:
        err(f"dummy pool {dummies} too close to 2500 at {peaks['dynamic'][1]}")
    if dbl > 3050:
        err(f"double-link nodes {dbl} too close to 3200 at {peaks['dyn_nodes'][1]}")
    if near > 160:
        err(f"{near} dynamic objects within 100 units (vanilla max 144)")
    print(f"car generators: {sum(len(c) for _, c in stream_files.values())} streamed, peak {pk.get('cars', 0)} "
          f"loaded/500 (CTheCarGenerators array)")
    if pk.get("cars", 0) > 480:
        err(f"{pk['cars']} car generators loaded at once at {peaks['cars'][1]} (array of 500)")
    # single-link sector nodes (70000): every building in each 50-unit sector its bound rect touches,
    # BIG buildings in the 200-unit LOD sectors
    text_single = VANILLA_KEPT_TEXT_INST
    for stem, insts in text_inst.items():
        if insts:
            lc = np.zeros(len(insts), bool)
            for (s, k), n in children.items():
                if s == stem and n and 0 <= k < len(lc):
                    lc[k] = True
            text_single += single_nodes(insts, _bound_rects(insts, aabb), is_big(insts, lc))
    single = text_single + pk.get("single", 0)
    print(f"single-link sector nodes {single}/70000")
    if single > 60000:
        err(f"single-link nodes {single} too close to 70000")
    # collision density (CCollision::ProcessColModels collTrisB[600])
    # Vehicles reach a bounding radius of ~8 (buses, trucks), the cheat planes ~12: hard limit at
    # 12 units, warning at 20 (big planes). Fast 2D over-estimate first, exact count when high.
    worst = {12.0: (0, ""), 20.0: (0, "")}
    for nm, (fn, m) in col_models.items():
        if col_tri_peak(m, radius=20.0) < 450:
            continue
        for r in (12.0, 20.0):
            n = col_tri_peak_exact(m, r)
            if n > worst[r][0]:
                worst[r] = (n, nm)
            if r == 12.0 and n > 560:
                err(f"{nm}: {n} collision triangles within {r:.0f} units of a point (collTrisB[600])")
            elif r == 20.0 and n > 560:
                warn(f"{nm}: {n} collision triangles within {r:.0f} units of a point (big planes, collTrisB[600])")
    print(f"collision: at most {worst[12.0][0]} triangles within 12 units of a point ({worst[12.0][1]}), "
          f"{worst[20.0][0]} within 20 ({worst[20.0][1]})")
    # visible entity lists (CRenderer ms_aVisibleEntityPtrs / ms_aVisibleLodPtrs, 1000 each, no
    # bounds check in the release game) at the maximum draw distance setting (LOD multiplier 1.8)
    # and a 90 degree horizontal field of view; vanilla peaks at 259 / 642 with the same measure
    hd, lods = [], []
    for stem, insts in text_inst.items():
        for k, i in enumerate(insts):
            (lods if children[(stem, k)] else hd).append((i.x, i.y, ddist(i.model_id)))
    for insts, _ in stream_files.values():
        hd += [(i.x, i.y, ddist(i.model_id)) for i in insts]
    hd, lods = np.array(hd).reshape(-1, 3), np.array(lods).reshape(-1, 3)
    vis = visible_peak(hd[:, :2], np.minimum(hd[:, 2] * 1.8, 2000.0))
    vis_l = visible_peak(lods[:, :2], np.minimum(lods[:, 2] * 1.8, 1500.0), step=100.0)
    print(f"visible entities: up to {vis[0]} map entities (+ vehicles / peds) at {vis[1]}, "
          f"{vis_l[0]} LODs (lists of 1000)")
    if vis[0] > 820:
        err(f"{vis[0]} map entities visible at once at {vis[1]} heading {vis[2]} (ms_aVisibleEntityPtrs[1000])")
    elif vis[0] > 650:
        warn(f"{vis[0]} map entities visible at once at {vis[1]} heading {vis[2]}")
    if vis_l[0] > 850:
        err(f"{vis_l[0]} LODs visible at once at {vis_l[1]} (ms_aVisibleLodPtrs[1000])")
    # ------------------------------------------------------------ water, zones, paths, scm, radar
    st = WT.validate_water(WT.parse_water_dat(open(os.path.join(F, "data", "water.dat")).read()))
    print("water:", WT.summary(st))
    zs = ZN.parse_zon(open(os.path.join(F, "data", "maps", "baku", "info.zon")).read())
    ms = ZN.parse_zon(open(os.path.join(F, "data", "maps", "baku", "map.zon")).read())
    print("zones:", ZN.validate_zones(zs, ms))
    areas = PT.read_area_files(G)
    res = PT.validate_areas(areas, strict=True)
    print("paths:", res if not isinstance(res, tuple) else res[:2])
    # CTrain::InitTrains reads data\paths\tracks*.dat (and again after every save load)
    from tools.gta import tracks as TR
    n_err = len(ERR)
    for k, name in TR.TRACK_FILES.items():
        fn = os.path.join(F, "data", "paths", name)
        if not os.path.exists(fn):
            err(f"data/paths/{name} missing (vanilla trains would run through Baku)")
            continue
        raw = open(fn, "rb").read()
        if len(raw) > TR.BUFFER_SIZE:
            err(f"{name}: {len(raw)} bytes > the {TR.BUFFER_SIZE} byte read buffer")
        try:
            nodes = TR.parse_tracks_dat(raw.decode("ascii"))
            TR.validate_track(nodes, track=k)
            if any(abs(n.x) < 3000 and abs(n.y) < 3000 for n in nodes):
                warn(f"{name}: track nodes inside the world")
        except Exception as e:      # TrackError or a parse error
            err(f"{name}: {e}")
    print(f"tracks: data/paths/tracks*.dat {'ok' if len(ERR) == n_err else 'ERRORS'}")
    radars = [f for f in os.listdir(G) if re.fullmatch(r"radar\d+\.txd", f)]
    if len(radars) != 144:
        err(f"{len(radars)} radar txds")
    for f in radars[:5]:
        txd.read_txd(open(os.path.join(G, f), "rb").read())
    scm = open(os.path.join(F, "data", "script", "main.scm"), "rb").read()
    if scm[:3] != b"\x02\x00\x01":
        err("main.scm does not start with a GOTO")
    print(f"main.scm {len(scm)} bytes")
    print(f"\n{len(ERR)} errors, {len(WARN)} warnings")
    for e in ERR[:60]:
        print("ERROR", e)
    for w in WARN[:30]:
        print("warn ", w)
    return 1 if ERR else 0


def _bound_rects(insts, aabb):
    """CEntity::GetBoundRect (Entity.cpp 0x534120): the four xy corners of the model's col
    bounding box, transformed by the instance matrix. insts: list of IPL.Inst; aabb(model_id)
    -> (min3, max3). Returns (n, 4) [x0, y0, x1, y1]."""
    if not insts:
        return np.zeros((0, 4))
    lo = np.array([aabb(i.model_id)[0][:2] for i in insts], np.float64)
    hi = np.array([aabb(i.model_id)[1][:2] for i in insts], np.float64)
    q = np.array([i.q() for i in insts], np.float64)
    pos = np.array([(i.x, i.y) for i in insts], np.float64)
    # rotation of the IPL quaternion (the engine uses the conjugate, see props.md section 2)
    qx, qy, qz, qw = (-q[:, 0], -q[:, 1], -q[:, 2], q[:, 3])
    r00 = 1 - 2 * (qy * qy + qz * qz)
    r01 = 2 * (qx * qy - qz * qw)
    r10 = 2 * (qx * qy + qz * qw)
    r11 = 1 - 2 * (qx * qx + qz * qz)
    xs, ys = [], []
    for cx, cy in ((lo[:, 0], lo[:, 1]), (hi[:, 0], hi[:, 1]), (hi[:, 0], lo[:, 1]), (lo[:, 0], hi[:, 1])):
        xs.append(pos[:, 0] + r00 * cx + r01 * cy)
        ys.append(pos[:, 1] + r10 * cx + r11 * cy)
    xs, ys = np.array(xs), np.array(ys)
    return np.c_[xs.min(0), ys.min(0), xs.max(0), ys.max(0)]


def _sectors(rects, size=50.0, n=120):
    """Number of world sectors (CWorld::GetSectorX/Y, 50 units, clamped to the 120x120 grid)
    an entity with this bound rect is linked into (CEntity::Add)."""
    if len(rects) == 0:
        return np.zeros(0, int)
    r = np.clip(rects, -3000.0, 2999.0)
    sx0 = np.clip(np.floor(r[:, 0] / size + n / 2), 0, n - 1)
    sy0 = np.clip(np.floor(r[:, 1] / size + n / 2), 0, n - 1)
    sx1 = np.clip(np.floor(r[:, 2] / size + n / 2), 0, n - 1)
    sy1 = np.clip(np.floor(r[:, 3] / size + n / 2), 0, n - 1)
    return ((sx1 - sx0 + 1) * (sy1 - sy0 + 1)).astype(int)


def stream_load_peaks(ipl_stats):
    """Exterior stream IPLs are loaded while the player is inside their bounding box (union of
    the instances' bound rects, inflated by 350 when one model has a draw distance > 150, else
    200: CIplStore::LoadIpl / LoadIplBoundingBox, SetIfInteriorIplIsRequired, LoadIpls).
    ipl_stats: {name: (bb (x0, y0, x1, y1) inflated, {counter: value})}. Returns
    {counter: (peak, (x, y), names)} over every player position: the maximum of a sum of
    closed-rectangle indicators is reached at a (left edge, bottom edge) pair."""
    if not ipl_stats:
        return {}
    names = list(ipl_stats)
    bbs = np.array([ipl_stats[n][0] for n in names])
    keys = sorted({k for n in names for k in ipl_stats[n][1]})
    vals = np.array([[ipl_stats[n][1].get(k, 0) for k in keys] for n in names], np.float64)
    xs = np.unique(np.clip(bbs[:, 0], -3000, 3000))
    ys = np.unique(np.clip(bbs[:, 1], -3000, 3000))
    inx = (bbs[:, 0][:, None] <= xs[None, :]) & (xs[None, :] <= bbs[:, 2][:, None])     # (ipl, x)
    iny = (bbs[:, 1][:, None] <= ys[None, :]) & (ys[None, :] <= bbs[:, 3][:, None])     # (ipl, y)
    out = {}
    for k, key in enumerate(keys):
        tot = np.einsum("i,ix,iy->xy", vals[:, k], inx.astype(np.float64), iny.astype(np.float64))
        a, b = np.unravel_index(int(np.argmax(tot)), tot.shape)
        loaded = [names[i] for i in np.nonzero(inx[:, a] & iny[:, b])[0]]
        out[key] = (int(round(tot[a, b])), (float(xs[a]), float(ys[b])), loaded)
    return out


def col_tri_peak(rec, radius=20.0, step=2.0):
    """Most triangles of one collision model that can touch a sphere of `radius` (2D bounding
    box overlap, an over-estimate of TestSphereTriangle). CCollision::ProcessColModels collects
    them in static collTrisB[600]; with face groups the inner `break` does not stop the next
    group from writing past the array."""
    V, Fc = rec.get("vertices"), rec.get("faces")
    if V is None or Fc is None or len(Fc) == 0:
        return 0
    tri = V[Fc][:, :, :2]
    lo = tri.min(1) - radius
    hi = tri.max(1) + radius
    o = lo.min(0)
    n = np.ceil((hi.max(0) - o) / step).astype(int) + 2
    acc = np.zeros((n[0] + 1, n[1] + 1), np.int32)
    i0 = np.floor((lo - o) / step).astype(int)
    i1 = np.floor((hi - o) / step).astype(int) + 1
    np.add.at(acc, (i0[:, 0], i0[:, 1]), 1)
    np.add.at(acc, (i1[:, 0], i0[:, 1]), -1)
    np.add.at(acc, (i0[:, 0], i1[:, 1]), -1)
    np.add.at(acc, (i1[:, 0], i1[:, 1]), 1)
    return int(acc.cumsum(0).cumsum(1).max())


def visible_peak(xy, rad, step=50.0, half_fov=45.0, chunk=400):
    """Most entities a camera can have in CRenderer's visible lists at once: entity i is drawn
    within rad[i] (draw distance x LOD multiplier) when inside the horizontal field of view
    (2 * half_fov; within 15 units any heading). Camera positions on a grid, headings in 5 deg
    steps. Returns (count, (x, y), heading_deg)."""
    from scipy.spatial import cKDTree
    if len(xy) == 0:
        return 0, None, None
    tree = cKDTree(xy)
    rmax = float(rad.max())
    x0, y0 = xy.min(0)
    x1, y1 = xy.max(0)
    pts = np.array(np.meshgrid(np.arange(x0, x1 + step, step), np.arange(y0, y1 + step, step))).reshape(2, -1).T
    k = min(72, int(round(2 * half_fov / 5.0)) + 1)
    best = (0, None, None)
    for c0 in range(0, len(pts), chunk):
        P = pts[c0:c0 + chunk]
        for p, idx in zip(P, tree.query_ball_point(P, rmax)):
            if len(idx) <= best[0]:
                continue
            idx = np.asarray(idx)
            d = xy[idx] - p
            dist = np.hypot(d[:, 0], d[:, 1])
            keep = dist < rad[idx]
            if keep.sum() <= best[0]:
                continue
            near = dist[keep] < 15.0
            ang = np.degrees(np.arctan2(d[keep, 1], d[keep, 0]))[~near]
            h = np.histogram(ang, bins=72, range=(-180.0, 180.0))[0]
            cs = np.cumsum(np.r_[0, h, h[:k]])
            w = cs[k:k + 72] - cs[:72] + near.sum()
            j = int(np.argmax(w))
            if w[j] > best[0]:
                best = (int(w[j]), (float(p[0]), float(p[1])), j * 5.0 - 180.0 + k * 2.5)
    return best


def _seg_dist(p, A, B):
    d = B - A
    t = np.clip(((p - A) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
    return np.sqrt(((A + d * t[:, None] - p) ** 2).sum(1))


def _point_tri_dist(p, A, B, C):
    """Distance from point p (3,) to triangles (n,3) A, B, C: the plane distance when the
    projection falls inside the triangle, else the nearest edge."""
    nrm = np.cross(B - A, C - A)
    ln = np.linalg.norm(nrm, axis=1)
    ok = ln > 1e-12
    nrm = nrm / np.where(ok, ln, 1.0)[:, None]
    h = ((p - A) * nrm).sum(1)
    q = p - nrm * h[:, None]
    inside = ok.copy()
    for U, W in ((A, B), (B, C), (C, A)):
        inside &= (np.cross(W - U, q - U) * nrm).sum(1) >= -1e-9
    d = np.minimum(np.minimum(_seg_dist(p, A, B), _seg_dist(p, B, C)), _seg_dist(p, C, A))
    return np.where(inside, np.abs(h), d)


def col_tri_peak_exact(rec, radius, probes=1500):
    """Most triangles within `radius` of a probe point (TestSphereTriangle), probes on the
    surface (+1 unit) at the triangle centroids."""
    V, Fc = rec.get("vertices"), rec.get("faces")
    if V is None or Fc is None or len(Fc) == 0:
        return 0
    T = V[Fc].astype(np.float64)
    lo, hi = T.min(1), T.max(1)
    P = T.mean(1)[:: max(1, len(T) // probes)] + np.array([0.0, 0.0, 1.0])
    best = 0
    for p in P:
        cand = np.nonzero(((lo <= p + radius) & (hi >= p - radius)).all(1))[0]
        if len(cand) <= best:
            continue
        n = int((_point_tri_dist(p, T[cand, 0], T[cand, 1], T[cand, 2]) <= radius).sum())
        best = max(best, n)
    return best


def _trim_rw(d: bytes) -> bytes:
    """IMG entries are sector padded: cut to the RW chunk size."""
    import struct
    t, s, v = struct.unpack_from("<III", d, 0)
    return d[:12 + s]


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:] or [])))
