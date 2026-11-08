"""Procedural ground textures for the Baku map (all original, generated with numpy).

Road textures span the whole carriageway width on U and the same physical length on V,
lane geometry matches tools.geo.roads (lane 4.8 units, gutter 0.6 units).
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

from .noise import value_noise, grain, blur_periodic, to_img
from tools.geo.roads import LANE_VIS, GUTTER


def _rng(seed):
    return np.random.default_rng(seed)


def asphalt(size=512, seed=1, base=(74, 76, 80), worn=0.0):
    r = _rng(seed)
    n1 = value_noise(size, 4, r, 6, 0.55)
    n2 = value_noise(size, 32, r, 3, 0.5)
    g = grain(size, r, 1.0)
    lum = (n1 - 0.5) * 18 + (n2 - 0.5) * 10 + g * 22
    # sparse light aggregate specks
    specks = (r.random((size, size)) > 0.985) * r.uniform(10, 35, (size, size))
    lum = lum + specks
    # dark patches (repairs / oil)
    patches = value_noise(size, 6, r, 3, 0.5)
    lum -= np.clip(patches - 0.68, 0, 1) * 60
    rgb = np.stack([lum + base[0], lum + base[1], lum + base[2]], -1)
    if worn:
        rgb = rgb * (1 - worn) + (rgb.mean(-1, keepdims=True) * 1.15) * worn
    return rgb


def road_texture(lanes_f, lanes_b, seed=10):
    W = (lanes_f + lanes_b) * LANE_VIS + 2 * GUTTER
    size = 1024 if W > 25 else (512 if W > 7 else 256)
    r = _rng(seed + lanes_f * 7 + lanes_b * 13)
    rgb = asphalt(size, seed + lanes_f * 3 + lanes_b)
    ppu = size / W                    # pixels per unit (both axes: V period == W units)
    u = (np.arange(size) + 0.5) / ppu  # units across
    v = (np.arange(size) + 0.5) / ppu  # units along
    U, Vv = np.meshgrid(u, v)
    # tyre wear: slightly darker smooth bands along wheel paths
    lanes = lanes_f + lanes_b
    wear = np.zeros_like(U)
    for k in range(lanes):
        cl = GUTTER + LANE_VIS * (k + 0.5)
        for off in (-0.85, 0.85):
            wear += np.exp(-((U - cl - off) / 0.35) ** 2)
    rgb -= (wear * 9)[..., None]
    paint = np.zeros_like(U)
    lw = 0.13

    def solid(x0):
        return (np.abs(U - x0) < lw / 2).astype(float)

    n_cycles = max(1, int(round(W / 8.0)))
    cyc = W / n_cycles
    dash = (np.mod(Vv, cyc) < cyc * 0.3).astype(float)
    # edge lines
    paint = np.maximum(paint, solid(GUTTER - 0.02))
    paint = np.maximum(paint, solid(W - GUTTER + 0.02))
    if lanes_b > 0:
        c = GUTTER + LANE_VIS * lanes_b
        if lanes_b + lanes_f <= 2:
            paint = np.maximum(paint, solid(c) * dash)
        else:
            paint = np.maximum(paint, solid(c - 0.12))
            paint = np.maximum(paint, solid(c + 0.12))
        for k in range(1, lanes_b):
            paint = np.maximum(paint, solid(GUTTER + LANE_VIS * k) * dash)
        for k in range(1, lanes_f):
            paint = np.maximum(paint, solid(c + LANE_VIS * k) * dash)
    else:
        for k in range(1, lanes_f):
            paint = np.maximum(paint, solid(GUTTER + LANE_VIS * k) * dash)
    # soften + wear the paint
    paint = np.asarray(Image.fromarray((paint * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.7)),
                       np.float64) / 255
    paint *= np.clip(0.75 + (value_noise(size, 16, r, 4) - 0.5) * 0.9 + grain(size, r, 0.3), 0.25, 1.0)
    white = np.array([212, 212, 205])
    rgb = rgb * (1 - paint[..., None]) + white * paint[..., None]
    # gutter: slightly lighter/dirtier concrete edge
    gut = ((U < GUTTER - 0.1) | (U > W - GUTTER + 0.1)).astype(float)
    rgb = rgb * (1 - 0.35 * gut[..., None]) + np.array([118, 114, 106]) * 0.35 * gut[..., None]
    return to_img(rgb)


def pavers(size=512, seed=3, units=5.0, brick=(0.32, 0.16), cols=((150, 148, 142), (128, 126, 122), (160, 150, 138))):
    """Running-bond paver bricks (typical Baku sidewalk 'brusçatka')."""
    r = _rng(seed)
    ppu = size / units
    bw, bh = brick[0] * ppu, brick[1] * ppu
    y = np.arange(size)[:, None] + 0.5
    x = np.arange(size)[None, :] + 0.5
    row = np.floor(y / bh).astype(int)
    xo = x + (row % 2) * bw / 2
    colidx = np.floor(xo / bw).astype(int)
    nrows = int(round(size / bh))
    ncols = int(round(size / bw)) + 1
    tone = r.random((nrows + 2, ncols + 2))
    t = tone[row % (nrows + 2), colidx % (ncols + 2)]
    fx = np.mod(xo, bw) / bw
    fy = np.mod(y, bh) / bh
    edge = np.minimum(np.minimum(fx, 1 - fx) * bw, np.minimum(fy, 1 - fy) * bh)
    grout = np.clip(1 - edge / 1.6, 0, 1)
    pal = np.array(cols, float)
    pick = (t * len(pal)).astype(int).clip(0, len(pal) - 1)
    rgb = pal[pick] + ((t * 7) % 1 - 0.5)[..., None] * 18
    n = value_noise(size, 8, r, 5)
    rgb += ((n - 0.5) * 26 + grain(size, r, 14))[..., None]
    rgb = rgb * (1 - 0.45 * grout[..., None]) + np.array([92, 90, 86]) * 0.45 * grout[..., None]
    dirt = np.clip(value_noise(size, 4, r, 4) - 0.6, 0, 1) * 40
    rgb -= dirt[..., None]
    return to_img(rgb)


def slabs(size=512, seed=4, units=6.0, slab=1.0, cols=((176, 168, 152), (164, 158, 146), (186, 180, 168))):
    """Large stone slabs (plazas, boulevard)."""
    r = _rng(seed)
    ppu = size / units
    s = slab * ppu
    y = np.arange(size)[:, None] + 0.5
    x = np.arange(size)[None, :] + 0.5
    ix, iy = np.floor(x / s).astype(int), np.floor(y / s).astype(int)
    n = int(round(size / s)) + 1
    tone = r.random((n + 1, n + 1))
    t = tone[iy % (n + 1), ix % (n + 1)]
    pal = np.array(cols, float)
    rgb = pal[(t * len(pal)).astype(int).clip(0, len(pal) - 1)]
    fx, fy = np.mod(x, s) / s, np.mod(y, s) / s
    edge = np.minimum(np.minimum(fx, 1 - fx), np.minimum(fy, 1 - fy)) * s
    grout = np.clip(1 - edge / 1.5, 0, 1)
    nn = value_noise(size, 16, r, 4)
    rgb = rgb + ((nn - 0.5) * 22 + grain(size, r, 12))[..., None]
    rgb = rgb * (1 - 0.5 * grout[..., None]) + np.array([110, 106, 98]) * 0.5 * grout[..., None]
    return to_img(rgb)


def natural(size, seed, base, var, cells=6, grain_s=20, specks=None, blades=False):
    r = _rng(seed)
    n1 = value_noise(size, cells, r, 6, 0.55)
    n2 = value_noise(size, cells * 8, r, 3, 0.5)
    lum = (n1 - 0.5) * var + (n2 - 0.5) * var * 0.5 + grain(size, r, grain_s)
    rgb = np.array(base, float)[None, None, :] + lum[..., None] * np.array([1.0, 1.0, 0.8])
    hue = value_noise(size, cells, r, 4)
    rgb[..., 0] += (hue - 0.5) * var * 0.6
    if blades:
        bl = blur_periodic(r.random((size, size)), 0.6)
        bl = np.clip((bl - 0.5) * 3, -1, 1)
        rgb += bl[..., None] * np.array([10, 16, 6])
    if specks:
        for col, dens, sz in specks:
            m = r.random((size, size)) > 1 - dens
            m = blur_periodic(m.astype(float), sz) > 0.12
            rgb[m] = rgb[m] * 0.4 + np.array(col) * 0.6
    return to_img(rgb)


def concrete(size=512, seed=7, units=10.0, joint=3.0):
    r = _rng(seed)
    base = np.array([150, 148, 142], float)
    n = value_noise(size, 6, r, 6)
    rgb = base + ((n - 0.5) * 30 + grain(size, r, 14))[..., None]
    ppu = size / units
    s = joint * ppu
    y = np.arange(size)[:, None] + 0.5
    x = np.arange(size)[None, :] + 0.5
    fx, fy = np.mod(x, s) / s, np.mod(y, s) / s
    edge = np.minimum(np.minimum(fx, 1 - fx), np.minimum(fy, 1 - fy)) * s
    rgb = rgb - (np.clip(1 - edge / 1.2, 0, 1) * 45)[..., None]
    stains = np.clip(value_noise(size, 5, r, 4) - 0.62, 0, 1) * 70
    rgb -= stains[..., None] * np.array([1, 1, 1.1])
    return to_img(rgb)


def kerb(size=256, seed=8):
    r = _rng(seed)
    n = value_noise(size, 8, r, 5)
    rgb = np.array([168, 166, 160], float) + ((n - 0.5) * 24 + grain(size, r, 12))[..., None]
    # block joints every 1 unit (u scale 4 units per texture)
    x = np.arange(size)[None, :]
    joint = (np.mod(x, size / 4) < 2).astype(float)
    rgb -= (joint * 50)[..., None]
    return to_img(np.broadcast_to(rgb, (size, size, 3)))


def gravel(size=512, seed=9):
    r = _rng(seed)
    pts = r.random((5000, 2)) * size
    img = np.zeros((size, size))
    yy, xx = np.mgrid[0:size, 0:size]
    from scipy.spatial import cKDTree
    t = cKDTree(np.vstack([pts + [dx, dy] for dx in (-size, 0, size) for dy in (-size, 0, size)]))
    d, i = t.query(np.c_[xx.ravel(), yy.ravel()], k=2)
    cell = (d[:, 1] - d[:, 0]).reshape(size, size)
    tone = r.random(len(pts) * 9)[i[:, 0]].reshape(size, size)
    rgb = np.array([118, 114, 108], float) + ((tone - 0.5) * 60)[..., None]
    rgb -= (np.clip(1 - cell / 3.0, 0, 1) * 55)[..., None]
    return to_img(rgb)


def zebra(size=256):
    """Crosswalk stripes with alpha (u across road: stripes along v)."""
    a = np.zeros((size, size), float)
    x = np.arange(size)
    stripe = (np.mod(x, size / 4) < size / 8).astype(float)
    a[:, :] = stripe[None, :]
    a[:, :] *= 1.0
    rgb = np.full((size, size, 3), 220.0)
    img = np.dstack([rgb, a * 255])
    return Image.fromarray(img.astype(np.uint8), "RGBA")


def all_textures():
    """Return dict name -> PIL image for the ground TXD."""
    T = {}
    T["bk_asph"] = to_img(asphalt(512, 1))
    T["bk_asph2"] = to_img(asphalt(512, 2, base=(86, 86, 86), worn=0.25))
    for (lf, lb) in ((1, 1), (2, 2), (3, 3), (1, 0), (2, 0), (3, 0)):
        T[f"bk_road{lf}{lb}"] = road_texture(lf, lb)
    T["bk_pave"] = pavers()
    T["bk_plaza"] = slabs()
    T["bk_curb"] = kerb()
    T["bk_grass"] = natural(512, 21, (88, 118, 58), 46, cells=6, grain_s=26, blades=True,
                            specks=[((120, 112, 70), 0.0015, 1.2)])
    T["bk_grassdry"] = natural(512, 22, (142, 136, 92), 44, cells=6, grain_s=24, blades=True,
                               specks=[((104, 112, 64), 0.003, 1.5)])
    T["bk_dirt"] = natural(512, 23, (140, 120, 94), 40, cells=5, grain_s=22,
                           specks=[((176, 166, 150), 0.004, 0.8)])
    T["bk_soil"] = natural(512, 24, (168, 152, 120), 34, cells=5, grain_s=18,
                           specks=[((128, 128, 84), 0.002, 1.6), ((190, 182, 168), 0.003, 0.7)])
    T["bk_sand"] = natural(512, 25, (204, 186, 148), 26, cells=8, grain_s=14)
    T["bk_seabed"] = natural(512, 26, (120, 112, 84), 30, cells=6, grain_s=16)
    T["bk_conc"] = concrete()
    T["bk_path"] = natural(512, 27, (158, 146, 124), 28, cells=10, grain_s=26,
                           specks=[((110, 104, 96), 0.01, 0.6)])
    T["bk_gravel"] = gravel()
    T["bk_zebra"] = zebra()
    return T


if __name__ == "__main__":
    import os, sys
    out = sys.argv[1] if len(sys.argv) > 1 else "build/tex"
    os.makedirs(out, exist_ok=True)
    for k, im in all_textures().items():
        im.save(os.path.join(out, k + ".png"))
        print(k, im.size)
