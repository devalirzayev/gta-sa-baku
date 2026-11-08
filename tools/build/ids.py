"""Model id / name allocation for custom Baku models (ids unused by vanilla SA, see data/sa/models.csv)."""
from tools.build.ground import NT

TILE_BASE = 18631            # 576 HD ground tiles: 18631..19206
LOD_BASE = 19207             # 576 ground LOD tiles (one per HD tile): 19207..19782
NL = 12                      # LOD texture grid 12 x 12 (500 x 500 units, one TXD per 2 x 2 LOD tiles)
# One LOD per HD tile: LinkLods (FileLoader.cpp 0x5B51E0) unlinks every extra child of a LOD unless
# the child is a BIG building, so a LOD must have exactly one HD child.
PROP_BASE = 15065            # custom props, signs, landmarks: 15065..15999
PROP_END = 15999

GROUND_TXD = "bk_ground"


def tile_name(ix, iy):
    return f"bk_t{ix:02d}{iy:02d}"


def tile_id(ix, iy):
    return TILE_BASE + ix * NT + iy


def lod_name(ix, iy):
    """LOD of HD tile (ix, iy)."""
    return f"bk_l{ix:02d}{iy:02d}"


def lod_id(ix, iy):
    return LOD_BASE + ix * NT + iy


def lod_txd(lx, ly):
    return f"bk_lt{lx:02d}{ly:02d}"


assert tile_id(NT - 1, NT - 1) < LOD_BASE
assert lod_id(NT - 1, NT - 1) <= 19999
