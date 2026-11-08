"""Railway tracks from the Overture rail segments (standard gauge, in use, above ground):
ballast bed with concrete sleepers and two rails, merged per 250-unit ground tile
(bk_rlNNN, shared TXD bk_rail).  At level crossings (road surface under the track) only the
rails are drawn, flush with the road.  `corridors()` is used by the building, prop and
parking placement so nothing stands on the tracks.

There are no trains (tracks.dat files are emptied, see tools/gta/tracks.py); the tracks are
scenery with collision on the ballast.
"""
from __future__ import annotations

import os

import numpy as np
import pyarrow.parquet as pq
import shapely
from PIL import Image, ImageDraw

from tools.build import ground
from tools.build.mesh import MeshBuilder
from tools.geo.proj import WORLD
from tools.rw import col as COL, dff as DFF, txd as TXD
from tools.rw.col import SURF

RL_BASE = 13891               # free vanilla ids 13891..14382
RL_END = 14382
RL_TXD = "bk_rail"
GAUGE = 1.52                  # 1520 mm (former USSR); objects keep SA scale (1 unit = 1 m)
BALLAST_TOP, BALLAST_BOT = 1.55, 2.15
SKIP_FLAGS = {"is_tunnel", "is_covered", "is_disused", "is_abandoned", "is_under_construction"}


def rail_lines():
    """Game-space LineStrings of the visible tracks."""
    out = []
    world = shapely.box(-WORLD + 2, -WORLD + 2, WORLD - 2, WORLD - 2)
    for r in pq.read_table("data/raw/segment.parquet", columns=["subtype", "class", "rail_flags", "geometry"]).to_pylist():
        if r["subtype"] != "rail" or r["class"] not in ("standard_gauge", "unknown"):
            continue
        g = shapely.from_wkb(r["geometry"])
        if g.geom_type != "LineString":
            continue
        flags = r["rail_flags"] or []
        whole_skip = any(set(f["values"]) & SKIP_FLAGS and not f.get("between") for f in flags)
        if whole_skip:
            continue
        cut = [tuple(f["between"]) for f in flags if set(f["values"]) & SKIP_FLAGS and f.get("between")]
        gg = ground._to_game(g)
        L = gg.length
        keep = [(0.0, 1.0)]
        for a, b in sorted(cut):
            nk = []
            for x0, x1 in keep:
                if b <= x0 or a >= x1:
                    nk.append((x0, x1))
                    continue
                if a > x0:
                    nk.append((x0, a))
                if b < x1:
                    nk.append((b, x1))
            keep = nk
        for x0, x1 in keep:
            if (x1 - x0) * L < 6:
                continue
            sub = shapely.ops.substring(gg, x0 * L, x1 * L)
            sub = sub.intersection(world)
            for part in getattr(sub, "geoms", [sub]):
                if part.geom_type == "LineString" and part.length >= 6:
                    out.append(part)
    return out


def corridors(lines=None, half=2.4):
    lines = rail_lines() if lines is None else lines
    return [l.buffer(half, cap_style="flat") for l in lines]


# ------------------------------------------------------------------------------------ textures
def tex_ballast():
    rng = np.random.default_rng(9)
    a = np.array([122, 116, 108]) + rng.normal(0, 16, (256, 128, 1))
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for y in range(0, 256, 32):                      # concrete sleepers, 0.6 m apart
        d.rectangle([4, y + 8, 123, y + 20], fill=(150, 148, 142))
        d.line([(4, y + 20), (123, y + 20)], fill=(96, 94, 90), width=2)
    return im


def tex_steel():
    """Rail steel: the polished running band runs along u (the rail direction)."""
    a = np.zeros((32, 64, 3))
    a[:] = (108, 100, 94)
    a[12:20, :] = (178, 178, 180)
    return Image.fromarray(a.astype(np.uint8))


def tex_gravel():
    rng = np.random.default_rng(10)
    g = rng.normal(0, 18, (64, 64, 1))
    a = np.array([116, 110, 102]) + g
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


# ------------------------------------------------------------------------------------ geometry
def _densify(line, step=6.0):
    line = line.simplify(0.2)
    L = line.length
    n = max(2, int(np.ceil(L / step)) + 1)
    pts = np.array([line.interpolate(t).coords[0] for t in np.linspace(0, L, n)])
    return pts


def _normals(p):
    d = np.gradient(p, axis=0)
    d /= np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
    return np.c_[-d[:, 1], d[:, 0]]


def build_tiles(W, lines, road_parts, verbose=True):
    """-> list of dicts(name, id, txd, origin, dff, col)."""
    road_tree = shapely.STRtree([p for p in road_parts if not p.is_empty])
    per_tile = {}          # (ix, iy) -> [MeshBuilder, ...]  (split before the 65535-vertex limit)
    counts = {}

    def builder(key, nverts):
        lst = per_tile.setdefault(key, [MeshBuilder()])
        if counts.get((key, len(lst)), 0) + nverts > 50000:
            lst.append(MeshBuilder())
        counts[(key, len(lst))] = counts.get((key, len(lst)), 0) + nverts
        return lst[-1]

    for line in lines:
        p = _densify(line)
        z = ground.H_at(W, p[:, 0], p[:, 1])
        if len(z) >= 5:
            k = np.ones(5) / 5
            zs = np.convolve(np.pad(z, 2, mode="edge"), k, mode="valid")
            z = np.maximum(zs, z - 0.15)
        n = _normals(p)
        on_road = np.zeros(len(p), bool)
        hits = road_tree.query(shapely.points(p), predicate="intersects")
        on_road[np.unique(hits[0])] = True
        s = np.r_[0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]
        for i in range(len(p) - 1):
            mid = (p[i] + p[i + 1]) / 2
            ix = int((mid[0] + WORLD) // ground.TILE)
            iy = int((mid[1] + WORLD) // ground.TILE)
            if not (0 <= ix < ground.NT and 0 <= iy < ground.NT):
                continue
            mb = builder((ix, iy), 64)
            a, b = p[i], p[i + 1]
            na, nb = n[i], n[i + 1]
            za, zb = z[i], z[i + 1]
            crossing = on_road[i] or on_road[i + 1]
            if not crossing:
                # ballast: top strip with sleepers, two sloped sides
                top = np.array([[*(a + na * BALLAST_TOP), za + 0.30], [*(a - na * BALLAST_TOP), za + 0.30],
                                [*(b - nb * BALLAST_TOP), zb + 0.30], [*(b + nb * BALLAST_TOP), zb + 0.30]])
                uv = np.array([[0, -s[i] / 2.4], [1, -s[i] / 2.4], [1, -s[i + 1] / 2.4], [0, -s[i + 1] / 2.4]])
                mb.add("bk_rl_ballast", top, uv, _up(top), shade=0.95, collide=False)
                for side in (1, -1):
                    sd = np.array([[*(a + na * side * BALLAST_TOP), za + 0.30], [*(b + nb * side * BALLAST_TOP), zb + 0.30],
                                   [*(b + nb * side * BALLAST_BOT), zb - 0.08], [*(a + na * side * BALLAST_BOT), za - 0.08]])
                    uvs = np.array([[s[i], 0], [s[i + 1], 0], [s[i + 1], 0.25], [s[i], 0.25]])   # continuous: welds
                    mb.add("bk_rl_gravel", sd, uvs, _up(sd), shade=0.85, collide=False)
                # collision: the ballast top only, every second segment merged (yards have 20+
                # parallel tracks; CCollision keeps at most 600 triangles per touching vehicle)
                if i % 2 == 0:
                    j = min(i + 2, len(p) - 1)
                    if not (on_road[j] or on_road[min(i + 1, len(p) - 1)]):
                        b2, nb2, zb2 = p[j], n[j], z[j]
                        hw_c = (BALLAST_TOP + BALLAST_BOT) / 2       # mid-slope: no invisible ledge
                        topc = np.array([[*(a + na * hw_c), za + 0.26], [*(a - na * hw_c), za + 0.26],
                                         [*(b2 - nb2 * hw_c), zb2 + 0.26], [*(b2 + nb2 * hw_c), zb2 + 0.26]])
                        mb.add_col(topc, _up(topc), SURF["GRAVEL"])
                rz_a, rz_b = za + 0.30 + 0.17, zb + 0.30 + 0.17
            else:
                rz_a, rz_b = za + 0.03, zb + 0.03          # level crossing: rails flush with the road
            nm = (na + nb) / 2
            for off in (GAUGE / 2, -GAUGE / 2):
                # rail head (top) and its two flanks, single-sided, facing outwards
                for w0, w1, h0, h1, outdir in ((-0.04, 0.04, 0.0, 0.0, np.array([0.0, 0.0, 1.0])),
                                               (-0.04, -0.04, -0.17, 0.0, np.r_[-nm, 0.0]),
                                               (0.04, 0.04, 0.0, -0.17, np.r_[nm, 0.0])):
                    q = np.array([[*(a + na * (off + w0)), rz_a + h0], [*(b + nb * (off + w0)), rz_b + h0],
                                  [*(b + nb * (off + w1)), rz_b + h1], [*(a + na * (off + w1)), rz_a + h1]])
                    uvq = np.array([[s[i], 0], [s[i + 1], 0], [s[i + 1], 1], [s[i], 1]], float) * [0.25, 1]
                    mb.add("bk_rl_steel", q, uvq, _up(q, out=outdir), shade=1.0, collide=False)
    out = []
    flat = [(key, j, mb) for key, lst in sorted(per_tile.items()) for j, mb in enumerate(lst) if not mb.empty()]
    for k, ((ix, iy), j, mb) in enumerate(flat):
        mid = RL_BASE + k
        assert mid <= RL_END, "too many rail tiles"
        name = f"bk_rl{ix:02d}{iy:02d}" + ("" if j == 0 else "abcdefgh"[j - 1])
        x0 = -WORLD + ix * ground.TILE
        y0 = -WORLD + iy * ground.TILE
        allz = np.concatenate([q[0][:, 2] for parts in mb.parts.values() for q in parts])
        origin = (x0 + ground.TILE / 2, y0 + ground.TILE / 2, float(np.round(np.median(allz))))
        geo, (cV, cT, cS, cL) = mb.build(origin)
        V = np.asarray(geo.vertices, float)
        cm = COL.ColModel(name, mid, cV, cT, cS, cL, bounds=(V.min(0), V.max(0)))
        out.append({"name": name, "id": mid, "txd": RL_TXD, "origin": origin,
                    "dff": DFF.write_dff(name, geo), "col": COL.write_col3(cm)})
    if verbose:
        print(f"rail tiles: {len(out)}, track length {sum(l.length for l in lines):.0f} units")
    return out


def _up(V, out=None):
    """Two triangles of quad V facing up (or along `out`)."""
    T = np.array([[0, 1, 2], [0, 2, 3]])
    nrm = np.cross(V[1] - V[0], V[2] - V[0])
    ref = np.array([0.0, 0.0, 1.0]) if out is None else np.asarray(out, float)
    if nrm @ ref < 0:
        T = T[:, [0, 2, 1]]
    return T


def build_all(W, out_dir="build/out/rail", verbose=True):
    import json
    os.makedirs(out_dir, exist_ok=True)
    lines = rail_lines()
    tiles = build_tiles(W, lines, list(W.road_parts) + list(W.minor_parts), verbose)
    meta = []
    for t in tiles:
        open(os.path.join(out_dir, t["name"] + ".dff"), "wb").write(t["dff"])
        open(os.path.join(out_dir, t["name"] + ".col"), "wb").write(t["col"])
        meta.append({k: t[k] for k in ("name", "id", "txd", "origin")} | {"dd": 290.0})
    tex = [TXD.texture_native("bk_rl_ballast", tex_ballast(), encoding="DXT1"),
           TXD.texture_native("bk_rl_steel", tex_steel(), encoding="DXT1"),
           TXD.texture_native("bk_rl_gravel", tex_gravel(), encoding="DXT1")]
    open(os.path.join(out_dir, RL_TXD + ".txd"), "wb").write(TXD.write_txd(tex))
    json.dump({"tiles": meta}, open(os.path.join(out_dir, "rail.json"), "w"), indent=1)
    return meta


if __name__ == "__main__":
    build_all(ground.prepare())
