"""Model id / name allocation for custom Baku models (ids unused by vanilla SA, see data/sa/models.csv)."""
from tools.build.ground import NT

TILE_BASE = 18631            # 576 HD ground tiles: 18631..19206
LOD_BASE = 19207             # 144 ground LOD tiles: 19207..19350
NL = 12                      # LOD grid 12 x 12 (500 x 500 units)
PROP_BASE = 15065            # custom props, signs, landmarks: 15065..15999
PROP_END = 15999

GROUND_TXD = "bk_ground"


def tile_name(ix, iy):
    return f"bk_t{ix:02d}{iy:02d}"


def tile_id(ix, iy):
    return TILE_BASE + ix * NT + iy


def lod_name(lx, ly):
    return f"bk_l{lx:02d}{ly:02d}"


def lod_id(lx, ly):
    return LOD_BASE + lx * NL + ly


def lod_txd(lx, ly):
    return f"bk_lt{lx:02d}{ly:02d}"


assert tile_id(NT - 1, NT - 1) < LOD_BASE
assert lod_id(NL - 1, NL - 1) < 19999
