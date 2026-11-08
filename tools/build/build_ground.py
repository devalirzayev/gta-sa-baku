"""Generate all ground tiles (HD DFF + COL3), ground LOD tiles and the ground texture dictionary.

    python3 -m tools.build.build_ground [--jobs 4] [--only ix,iy ...]
Outputs into build/out/ground/.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import time

import numpy as np
import shapely
from PIL import Image

from tools.build import ground, ids
from tools.build.mesh import MeshBuilder
from tools.build.topdown import TopDown, MAT_RGB
from tools.geo.proj import WORLD
from tools.rw import dff, col, txd

OUT = "build/out/ground"
_W = None


def _init():
    global _W
    _W = ground.prepare(cache=True, verbose=False)


def build_one(args):
    ix, iy = args
    W = _W
    name = ids.tile_name(ix, iy)
    mid = ids.tile_id(ix, iy)
    mb = ground.build_tile(W, ix, iy)
    if mb.empty():
        return name, None
    x0 = -WORLD + ix * ground.TILE
    y0 = -WORLD + iy * ground.TILE
    allz = np.concatenate([p[0][:, 2] for parts in mb.parts.values() for p in parts])
    zc = float(np.round(np.median(allz)))
    origin = (x0 + ground.TILE / 2, y0 + ground.TILE / 2, zc)
    geo, (cV, cT, cS, cL) = mb.build(origin)
    data = dff.write_dff(name, geo)
    colm = col.ColModel(name, mid, cV, cT, cS, cL)
    cdata = col.write_col3(colm)
    with open(os.path.join(OUT, name + ".dff"), "wb") as f:
        f.write(data)
    with open(os.path.join(OUT, name + ".col"), "wb") as f:
        f.write(cdata)
    meta = {"name": name, "id": mid, "origin": origin, "verts": int(len(geo.vertices)),
            "tris": int(len(geo.triangles)), "col_faces": int(len(cT)) if cT is not None else 0,
            "zmin": float(allz.min()), "zmax": float(allz.max()),
            "materials": [m.texture for m in geo.materials]}
    with open(os.path.join(OUT, name + ".json"), "w") as f:
        json.dump(meta, f)
    return name, meta


def ground_txd():
    from tools.tex import ground_tex
    texs = ground_tex.all_textures()
    items = []
    for k, im in texs.items():
        enc = "DXT5" if k == "bk_zebra" else "DXT1"
        items.append((k, im, enc))
    return txd.write_txd(items)


# ------------------------------------------------------------------ LOD tiles
def lod_texture(W, lx, ly, size=256, roofs=None):
    L = 6000 / ids.NL
    x0 = -WORLD + lx * L
    y0 = -WORLD + ly * L
    td = TopDown((x0, y0, x0 + L, y0 + L), size, supersample=3)
    box = shapely.box(x0, y0, x0 + L, y0 + L)
    for poly, mat in W.cover:
        if poly.intersects(box):
            td.polygon(poly, MAT_RGB.get(mat, MAT_RGB["bk_soil"]))
    sea, lakes = W.water_polys
    for p in [sea] + [p for p, _ in lakes]:
        if p.intersects(box):
            td.polygon(p, MAT_RGB["water"])
    sp = ground.spatial(W, "path_parts").clip(box.buffer(5))
    td.polygon(sp, MAT_RGB["path"])
    mn = ground.spatial(W, "minor_parts").clip(box.buffer(5))
    td.polygon(mn, MAT_RGB["minor"])
    sw = ground.spatial(W, "sw_parts").clip(box.buffer(5))
    td.polygon(sw, MAT_RGB["sidewalk"])
    rd = ground.spatial(W, "road_parts").clip(box.buffer(5))
    td.polygon(rd, MAT_RGB["road"])
    # elevated roads (structures) drawn on top
    for e in W.g.edges.values():
        if e.major and np.abs(e.lift).max() > 0.3:
            ls = shapely.LineString(e.pts)
            if ls.intersects(box):
                td.polygon(ls.buffer(e.width / 2 + 0.5, cap_style="flat"), (96, 96, 98))
    if roofs is not None:
        for p, c in roofs:
            if p.intersects(box):
                td.polygon(p, c)
    return td.result(noise=3.0, seed=lx * 100 + ly)


def lod_mesh(W, lx, ly, step=20.0):
    L = 6000 / ids.NL
    x0 = -WORLD + lx * L
    y0 = -WORLD + ly * L
    n = int(L / step)
    xs = np.linspace(x0, x0 + L, n + 1)
    ys = np.linspace(y0, y0 + L, n + 1)
    X, Y = np.meshgrid(xs, ys)
    Z = ground.H_at(W, X.ravel(), Y.ravel()).reshape(X.shape)
    V = np.c_[X.ravel(), Y.ravel(), Z.ravel()]
    UV = np.c_[(X.ravel() - x0) / L, 1 - (Y.ravel() - y0) / L]
    T = []
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            b, c, d = a + 1, a + n + 2, a + n + 1
            T += [(a, b, c), (a, c, d)]
    T = np.array(T)
    return V, UV, T


def build_lod(W, lx, ly, roofs=None):
    name = ids.lod_name(lx, ly)
    tname = ids.lod_txd(lx, ly)
    V, UV, T = lod_mesh(W, lx, ly)
    L = 6000 / ids.NL
    origin = (-WORLD + (lx + 0.5) * L, -WORLD + (ly + 0.5) * L, float(np.round(np.median(V[:, 2]))))
    mb = MeshBuilder()
    sh = ground.shade_terrain(W, V[:, 0], V[:, 1])
    mb.add(tname, V, UV, T, shade=sh, collide=False)
    geo, _ = mb.build(origin)
    for m in geo.materials:
        m.address_u = m.address_v = dff.ADDR_CLAMP
    data = dff.write_dff(name, geo)
    img = lod_texture(W, lx, ly, roofs=roofs)
    tdata = txd.write_txd([(tname, img, "DXT1")])
    return name, data, tname, tdata, origin


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--skip-tiles", action="store_true")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    W = ground.prepare(cache=True)
    global _W
    _W = W
    print("world ready", round(time.time() - t0, 1), "s", flush=True)
    with open(os.path.join(OUT, ids.GROUND_TXD + ".txd"), "wb") as f:
        f.write(ground_txd())
    if not a.skip_tiles:
        jobs = [(ix, iy) for ix in range(ground.NT) for iy in range(ground.NT)]
        if a.only:
            jobs = [tuple(map(int, s.split(","))) for s in a.only]
        ctx = mp.get_context("fork")
        done = 0
        with ctx.Pool(a.jobs) as pool:
            for name, meta in pool.imap_unordered(build_one, jobs, chunksize=2):
                done += 1
                if done % 24 == 0 or done == len(jobs):
                    print(f"  tiles {done}/{len(jobs)}  {round(time.time() - t0)} s", flush=True)
    for lx in range(ids.NL):
        for ly in range(ids.NL):
            name, data, tname, tdata, origin = build_lod(W, lx, ly)
            with open(os.path.join(OUT, name + ".dff"), "wb") as f:
                f.write(data)
            with open(os.path.join(OUT, tname + ".txd"), "wb") as f:
                f.write(tdata)
            with open(os.path.join(OUT, name + ".json"), "w") as f:
                json.dump({"name": name, "id": ids.lod_id(lx, ly), "origin": origin, "txd": tname}, f)
    print("done", round(time.time() - t0, 1), "s")


if __name__ == "__main__":
    main()
