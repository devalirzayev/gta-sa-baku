"""Aerial preview of the built map: HD ground tiles near the camera, LOD tiles (with the roof
texture) further away, landmarks, sign / rail tiles, the sea, and the SA buildings / trees as
placeholder boxes / cones (their real models come from the player's game and are not available
to the build).

    python3 -m tools.preview.aerial <name> eye_x eye_y eye_z target_x target_y target_z \
        [hd_x0 hd_y0 hd_x1 hd_y1] [box_radius]
Environment: W, H (image size), FOV, FOG0 / FOG1, VIEW (radius of loaded LOD tiles / sea).
Writes build/preview/<name>.png.  docs/img renders:
    aerial_bulvar:   380 -3120 430  -330 -2330 0  -1000 -3000 750 -1750  2000
    aerial_overview: VIEW=12000 FOG0=9000 FOG1=18000  -1350 1050 4100  250 -1000 0  9000 9000 9001 9001  9000
"""
import json
import math
import os
import pickle
import sys

import numpy as np
import pandas as pd
from PIL import Image

from tools.rw import dff, txd
from tools.preview import render as R
from tools.build.mesh import MeshBuilder, triangulate_area
from tools.build import ground

SP = os.path.join("build", "preview") + os.sep
TEX = SP + "tex"
os.makedirs(TEX, exist_ok=True)

name = sys.argv[1]
eye = list(map(float, sys.argv[2:5]))
tgt = list(map(float, sys.argv[5:8]))
hd_box = list(map(float, sys.argv[8:12])) if len(sys.argv) >= 12 else [-750, -3000, 750, -2000]
box_r = float(sys.argv[12]) if len(sys.argv) >= 13 else 1800.0
W_ = int(os.environ.get("W", 1600))
H_ = int(os.environ.get("H", 900))


# ------------------------------------------------------------------ textures
def save_png(nm, img):
    p = os.path.join(TEX, nm + ".png")
    if not os.path.exists(p):
        img.convert("RGBA").save(p)


for f in os.listdir("build/tex"):
    if f.endswith(".png") and not os.path.exists(os.path.join(TEX, f)):
        os.symlink(os.path.abspath(os.path.join("build/tex", f)), os.path.join(TEX, f))


def decode_txd(path):
    for t in txd.read_txd(open(path, "rb").read(), decode=True):
        im = t["image"]
        if not isinstance(im, Image.Image):
            im = Image.fromarray(np.asarray(im))
        save_png(t["name"], im)


decode_txd("build/out/landmarks/bk_lmtx.txd")
if os.path.exists("build/out/rail/bk_rail.txd"):
    decode_txd("build/out/rail/bk_rail.txd")
save_png("pv_white", Image.new("RGB", (4, 4), (255, 255, 255)))
save_png("pv_water", Image.new("RGB", (4, 4), (255, 255, 255)))

GROUND = "build/out/ground"
models = []


def in_box(x, y, b):
    return b[0] <= x < b[2] and b[1] <= y < b[3]


# ------------------------------------------------------------------ ground: HD near, LOD elsewhere
tiles_hd = []
for f in sorted(os.listdir(GROUND)):
    if f.startswith("bk_t") and f.endswith(".json"):
        m = json.load(open(os.path.join(GROUND, f)))
        ox, oy, oz = m["origin"]
        if in_box(ox, oy, hd_box):
            tiles_hd.append(m["name"])
            models.append((open(os.path.join(GROUND, m["name"] + ".dff"), "rb").read(), m["origin"]))
lod_txds = set()
for f in sorted(os.listdir(GROUND)):
    if f.startswith("bk_l") and not f.startswith("bk_lt") and f.endswith(".json"):
        m = json.load(open(os.path.join(GROUND, f)))
        ox, oy, oz = m["origin"]
        if in_box(ox, oy, hd_box):
            continue
        if math.hypot(ox - eye[0], oy - eye[1]) > float(os.environ.get("VIEW", 4200)):
            continue
        lod_txds.add(m["txd"])
        models.append((open(os.path.join(GROUND, m["name"] + ".dff"), "rb").read(), m["origin"]))
for t in lod_txds:
    if not os.path.exists(os.path.join(TEX, t + ".png")):
        decode_txd(os.path.join(GROUND, t + ".txd"))
print("HD tiles", len(tiles_hd), "LOD tiles", len(models) - len(tiles_hd), flush=True)

# ------------------------------------------------------------------ landmarks, sign tiles, rail tiles
lmk = json.load(open("build/out/landmarks/landmarks.json"))
for m in lmk["models"]:
    if math.hypot(m["origin"][0] - eye[0], m["origin"][1] - eye[1]) < float(os.environ.get("VIEW", 4200)):
        models.append((open(f"build/out/landmarks/{m['name']}.dff", "rb").read(), m["origin"]))
sg = json.load(open("build/out/signs/signs.json"))
for t in sg["tiles"]:
    ox, oy, oz = t["origin"]
    if in_box(ox, oy, hd_box):
        tex_name = "bk_sga" + t["name"][5:]
        if not os.path.exists(os.path.join(TEX, tex_name + ".png")):
            decode_txd(f"build/out/signs/{t['txd']}.txd")
        models.append((open(f"build/out/signs/{t['name']}.dff", "rb").read(), t["origin"]))
rl = json.load(open("build/out/rail/rail.json"))
for t in rl["tiles"]:
    ox, oy, oz = t["origin"]
    if in_box(ox, oy, hd_box):
        models.append((open(f"build/out/rail/{t['name']}.dff", "rb").read(), t["origin"]))

# ------------------------------------------------------------------ sea (water.dat level 0)
W = ground.prepare(cache=True, verbose=False)
import shapely
sea, lakes = W.water_polys
VR = float(os.environ.get("VIEW", 4200))
view = shapely.box(eye[0] - VR, eye[1] - VR, eye[0] + VR, eye[1] + VR)
mbw = MeshBuilder()
for poly, lvl in [(sea, 0.0)] + [(p, float(l)) for p, l in lakes]:
    g = poly.intersection(view)
    if g.is_empty:
        continue
    P, T = triangulate_area(g, 200.0)
    if len(T):
        mbw.add("pv_water", np.c_[P, np.full(len(P), lvl)], P / 50.0, T, collide=False,
                day_rgb=np.array([70, 112, 150]))
# outside the world: open sea south / east of the coast
# outside the world: the Caspian continues south and east of the bay
outer = shapely.union_all([shapely.box(-9000, -9000, 9000, -3000), shapely.box(3000, -3000, 9000, -1400)]).intersection(view)
P, T = triangulate_area(outer, 400.0)
if len(T):
    mbw.add("pv_water", np.c_[P, np.zeros(len(P))], P / 50.0, T, collide=False, day_rgb=np.array([70, 112, 150]))
if not mbw.empty():
    geo, _ = mbw.build((eye[0], eye[1], 0.0))
    models.append((dff.write_dff("pv_sea", geo), (eye[0], eye[1], 0.0)))

# ------------------------------------------------------------------ SA buildings as boxes
cat = pd.read_csv("data/sa/models.csv", low_memory=False).set_index("id")
blds, _ = pickle.load(open("data/cache/buildings.pkl", "rb"))
STYLE_RGB = {"residential": (160, 142, 118), "commercial": (134, 140, 152), "industrial": (132, 122, 106),
             "office": (124, 140, 160), "civic": (168, 156, 134)}
SUN = np.array([0.45, -0.35, 0.82])
SUN /= np.linalg.norm(SUN)
builders = [MeshBuilder()]
cnt = [0]


def mb_for(nv):
    # RW triangles index vertices with uint16: start a new geometry before 60000 vertices
    if cnt[0] + nv > 60000:
        builders.append(MeshBuilder())
        cnt[0] = 0
    cnt[0] += nv
    return builders[-1]


n_box = 0
for b in blds:
    if math.hypot(b["x"] - tgt[0], b["y"] - tgt[1]) > box_r:
        continue
    if b["id"] not in cat.index or pd.isna(cat.at[b["id"], "aabb_min_x"]):
        continue
    r = cat.loc[b["id"]]
    lo = np.array([r.aabb_min_x, r.aabb_min_y, r.aabb_min_z])
    hi = np.array([r.aabb_max_x, r.aabb_max_y, r.aabb_max_z])
    h = b["heading"]
    c, s = math.cos(h), math.sin(h)
    corners = np.array([[lo[0], lo[1]], [hi[0], lo[1]], [hi[0], hi[1]], [lo[0], hi[1]]])
    wx = b["x"] + corners[:, 0] * c - corners[:, 1] * s
    wy = b["y"] + corners[:, 0] * s + corners[:, 1] * c
    z0, z1 = b["z"] + lo[2], b["z"] + hi[2]
    base = np.array(next((v for k, v in STYLE_RGB.items() if k in str(b.get("style", ""))), (168, 158, 142)), float)
    base = base * (0.9 + 0.2 * ((hash(b["name"]) % 97) / 96.0))
    V, T, C = [], [], []
    for k in range(4):                                   # walls
        a, d = k, (k + 1) % 4
        q = [(wx[a], wy[a], z0), (wx[d], wy[d], z0), (wx[d], wy[d], z1), (wx[a], wy[a], z1)]
        e = np.array([wx[d] - wx[a], wy[d] - wy[a], 0.0])
        nrm = np.array([e[1], -e[0], 0.0])
        nrm /= max(np.linalg.norm(nrm), 1e-9)
        lit = 0.55 + 0.45 * max(0.0, float(nrm @ SUN))
        o = len(V)
        V += q
        T += [(o, o + 2, o + 1), (o, o + 3, o + 2), (o, o + 1, o + 2), (o, o + 2, o + 3)]
        C += [base * lit * 0.92, base * lit * 0.92, base * lit, base * lit]
    o = len(V)                                           # roof
    V += [(wx[k], wy[k], z1) for k in range(4)]
    T += [(o, o + 1, o + 2), (o, o + 2, o + 3), (o, o + 2, o + 1), (o, o + 3, o + 2)]
    C += [np.array([128.0, 126.0, 122.0])] * 4
    V = np.array(V)
    mb_for(len(V)).add("pv_white", V, np.zeros((len(V), 2)), np.array(T), collide=False,
                       day_rgb=np.clip(np.array(C), 0, 255))
    n_box += 1
# trees as cones (HD area only)
props = pickle.load(open("data/cache/props.pkl", "rb"))
n_tree = 0
for p in props:
    if p["kind"] != "tree" or not in_box(p["x"], p["y"], hd_box):
        continue
    x, y, z = p["x"], p["y"], p["z"]
    rr, hh = 2.6, 7.5
    ang = np.linspace(0, 2 * math.pi, 7)[:-1]
    ring = np.c_[x + rr * np.cos(ang), y + rr * np.sin(ang), np.full(6, z + 2.0)]
    V = np.vstack([ring, [[x, y, z + 2.0 + hh]], [[x, y, z]], [[x, y, z + 2.0]]])
    T = [(k, (k + 1) % 6, 6) for k in range(6)]
    g = np.array([62, 104, 52], float)
    C = [g * (0.7 + 0.3 * max(0.0, math.cos(a - 0.6))) for a in ang] + [g * 1.2, (90, 70, 50), (90, 70, 50)]
    mb_for(len(V)).add("pv_white", V, np.zeros((len(V), 2)), np.array(T), collide=False, day_rgb=np.array(C))
    n_tree += 1
print("boxes", n_box, "trees", n_tree, flush=True)
for k, mbb in enumerate(builders):
    if not mbb.empty():
        geo, _ = mbb.build((tgt[0], tgt[1], 0.0))
        models.append((dff.write_dff(f"pv_bld{k}", geo), (tgt[0], tgt[1], 0.0)))

sc = R.scene_json(models, eye, tgt, fov=float(os.environ.get("FOV", 50)), fog=[float(os.environ.get("FOG0", 2600)), float(os.environ.get("FOG1", 6500))])
out = SP + name + ".png"
print(R.render(sc, TEX, out, w=W_, h=H_, workdir=SP + "_viewer"), flush=True)
