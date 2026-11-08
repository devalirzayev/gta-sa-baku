"""Build the signage models: road sign types (shared TXD bk_rsign) and the per-tile merged
shop-board / street-plate / zebra models.

    python3 -m tools.build.build_signs
Needs data/cache/props.pkl + crossings.pkl (python3 -m tools.build.props) and
build/out/paths/lights.json.  Writes build/out/signs/ (DFF, COL, TXD, signs.json,
object_dat_add.txt, atlas previews).
"""
from __future__ import annotations

import json
import os
import pickle
import time

from tools.build import ground, props as PR, signs as SG, signtiles as SGT

OUT = "build/out/signs"
RSIGN_DD = 60.0


def main(verbose=True):
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        os.remove(os.path.join(OUT, f))
    W = ground.prepare()
    blds, _ = pickle.load(open("data/cache/buildings.pkl", "rb"))
    lights = json.load(open("build/out/paths/lights.json"))
    cross = pickle.load(open("data/cache/crossings.pkl", "rb"))
    crossings = [(W.lay.ribbons[eid], s) for eid, s in cross if eid in W.lay.ribbons]
    meta = {"road_signs": [], "tiles": []}
    # road sign types
    models, txd_bytes = SG.build_road_sign_models()
    for name, mid, dff_b, col_b in models:
        open(os.path.join(OUT, name + ".dff"), "wb").write(dff_b)
        open(os.path.join(OUT, name + ".col"), "wb").write(col_b)
        meta["road_signs"].append({"name": name, "id": mid, "txd": SG.RSIGN_TXD, "dd": RSIGN_DD})
    open(os.path.join(OUT, SG.RSIGN_TXD + ".txd"), "wb").write(txd_bytes)
    with open(os.path.join(OUT, "object_dat_add.txt"), "w", newline="\r\n") as f:
        f.write("; Baku road signs (same parameters as bussign1)\n")
        for line in SG.object_dat_lines():
            f.write(line + "\n")
    # per-tile signage
    ctx = PR.Ctx(W, blds)
    exclude = None
    lmj = "build/out/landmarks/landmarks.json"
    if os.path.exists(lmj):
        import shapely
        exclude = shapely.union_all([shapely.from_wkt(w).buffer(1.5) for w in json.load(open(lmj))["footprints"].values()])
    tiles = SGT.build_tiles(ctx, blds, lights, crossings, verbose, exclude=exclude)
    for t in tiles:
        open(os.path.join(OUT, t["name"] + ".dff"), "wb").write(t["dff"])
        open(os.path.join(OUT, t["name"] + ".col"), "wb").write(t["col"])
        open(os.path.join(OUT, t["txd"] + ".txd"), "wb").write(t["txd_bytes"])
        meta["tiles"].append({k: t[k] for k in ("name", "id", "txd", "ix", "iy", "origin", "n_boards", "n_zebra")} |
                             {"dd": SGT.SG_DD})
    big = sorted(tiles, key=lambda t: -t["n_boards"])[:3]
    for t in big:
        t["atlas"].save(os.path.join(OUT, f"atlas_{t['name']}.png"))
    json.dump(meta, open(os.path.join(OUT, "signs.json"), "w"), indent=1)
    if verbose:
        print(f"signs built in {time.time() - t0:.0f}s: {len(models)} road sign types, {len(tiles)} sign tiles")


if __name__ == "__main__":
    main()
