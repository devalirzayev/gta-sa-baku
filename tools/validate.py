"""Consistency + engine-limit validator for the packaged mod (dist/).

    python3 -m tools.validate [dist]
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


def main(dist="dist"):
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
        if src == "baku.ide" and nm.startswith("bk_t"):
            if nm.lower() not in col_models:
                err(f"{nm}: no collision")
            elif col_models[nm.lower()][1]["model_id"] != mid:
                err(f"{nm}: col model id {col_models[nm.lower()][1]['model_id']} != {mid}")
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
    stream_counts = {}
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
        stream_counts[stem] = len(insts)
        if len(stem) > 17:
            err(f"IPL slot name too long {stem}")
    cells = sorted(stream_counts.values(), reverse=True)
    peak = sum(cells[:4])
    est_pool = n_text + peak + 1500      # + vanilla interior text IPLs (estimate)
    print(f"IPL: {n_text} text instances, {sum(stream_counts.values())} streamed in {len(stream_counts)} binary IPLs; "
          f"densest 4 cells {peak} -> building pool estimate {est_pool}/13000")
    if est_pool > 12000:
        err(f"building pool estimate {est_pool} too close to 13000")
    # ------------------------------------------------------------ water, zones, paths, scm, radar
    st = WT.validate_water(WT.parse_water_dat(open(os.path.join(F, "data", "water.dat")).read()))
    print("water:", WT.summary(st))
    zs = ZN.parse_zon(open(os.path.join(F, "data", "maps", "baku", "info.zon")).read())
    ms = ZN.parse_zon(open(os.path.join(F, "data", "maps", "baku", "map.zon")).read())
    print("zones:", ZN.validate_zones(zs, ms))
    areas = PT.read_area_files(G)
    res = PT.validate_areas(areas, strict=True)
    print("paths:", res if not isinstance(res, tuple) else res[:2])
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


def _trim_rw(d: bytes) -> bytes:
    """IMG entries are sector padded: cut to the RW chunk size."""
    import struct
    t, s, v = struct.unpack_from("<III", d, 0)
    return d[:12 + s]


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:] or [])))
