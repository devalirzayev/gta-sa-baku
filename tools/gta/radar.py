"""GTA San Andreas radar / pause-map tiles (``radar00.txd`` .. ``radar143.txd`` in gta3.img).

Engine facts (docs/notes/data_files.md section d; gta-reversed Radar.cpp):

* ``CRadar::Initialise`` (0x587FB0): ``gRadarTextures[y][x] = CTxdStore::FindTxdSlot(
  "radar%02d" % (y*12 + x))`` for a 12x12 grid -> names radar00..radar99, radar100..radar143.
  The TXD slots exist only if an IMG archive (gta3.img) contains ``radarNN.txd``.
* Tile (col x, row y) covers world X [500x-3000, 500x-2500] and Y [2500-500y, 3000-500y]
  (``GetTextureCorners`` 0x584D90, ``TransformRealWorldToTexCoordSpace`` 0x583600): column 0 =
  west, row 0 = north, texture u -> east, v -> south (normal north-up image orientation).
* ``DrawRadarSection`` / ``DrawRadarSectionMap`` draw ``GetFirstTexture(txd)``: the texture name
  is irrelevant (vanilla uses ``radarNN``); outside the 12x12 grid the minimap draws the flat sea
  colour (111, 137, 170).
* Vanilla tiles: one 128x128 texture per TXD, DXT1 without alpha, one mip level (see doc for the
  evidence and the uncertainty about filter flags).  Any power-of-two size works (HD radar mods
  use 256/512); memory is 144 tiles x size.
"""
from __future__ import annotations

from typing import Sequence

from PIL import Image

GRID = 12                     # MAX_RADAR_WIDTH_TILES / MAX_RADAR_HEIGHT_TILES
TILE_WORLD = 500.0            # world units per tile
WORLD_MIN, WORLD_MAX = -3000.0, 3000.0
TILE_SIZE = 128               # vanilla texture size
NUM_TILES = GRID * GRID       # 144 (loop bound 0x90 at 0x588035)
SEA_RGB = (111, 137, 170)     # CRadar::DrawRadarSection colour for sections outside the grid
OFF_MAP_RGB = (204, 204, 204) # drawn instead of tiles while CTheScripts::bPlayerIsOffTheMap

# recommended raster parameters for tools.rw.txd (vanilla layout)
TXD_ENCODING = "DXT1"
TXD_MIPMAPS = False


def tile_name(index: int) -> str:
    """'radar%02d' (two-digit minimum, so tiles >= 100 have three digits)."""
    if not 0 <= index < NUM_TILES:
        raise ValueError(index)
    return "radar%02d" % index


def tile_index(col: int, row: int) -> int:
    if not (0 <= col < GRID and 0 <= row < GRID):
        raise ValueError((col, row))
    return row * GRID + col


def tile_colrow(index: int) -> tuple:
    return index % GRID, index // GRID


def tile_world_rect(col: int, row: int) -> tuple:
    """(xmin, ymin, xmax, ymax) world rectangle shown by tile (col, row)."""
    xmin = TILE_WORLD * col + WORLD_MIN
    ymax = WORLD_MAX - TILE_WORLD * row
    return xmin, ymax - TILE_WORLD, xmin + TILE_WORLD, ymax


def world_to_tile(x: float, y: float) -> tuple:
    """Section the game streams around a world point (CRadar::StreamRadarSections 0x5858D0):
    col = floor((x+3000)/500), row = ceil(11 - (y+3000)/500).  May be outside 0..11."""
    import math
    return (int(math.floor((x + 3000.0) / TILE_WORLD)),
            int(math.ceil(11.0 - (y + 3000.0) / TILE_WORLD)))


def world_to_pixel(x: float, y: float, width: int, height: int) -> tuple:
    """Pixel position of a world point in a north-up image spanning the +-3000 square."""
    return ((x - WORLD_MIN) / (WORLD_MAX - WORLD_MIN) * width,
            (WORLD_MAX - y) / (WORLD_MAX - WORLD_MIN) * height)


def split_radar_image(img: Image.Image, tile_size: int = TILE_SIZE, *,
                      resample=Image.LANCZOS) -> list:
    """Cut a north-up image of the whole +-3000 world into the 144 radar tiles.

    Returns [(txd_name, texture_name, PIL.Image RGB tile_size x tile_size), ...] in index order
    0..143 (row-major, row 0 = north, col 0 = west).  The image is first resampled to exactly
    12*tile_size pixels square so tile borders fall on pixel borders.  Use ``SEA_RGB`` for open
    water so the map blends into the colour the minimap draws beyond +-3000."""
    if tile_size < 4 or tile_size & (tile_size - 1):
        raise ValueError("tile_size must be a power of two >= 4 (DXT1 blocks)")
    w, h = img.size
    if w != h:
        raise ValueError(f"radar image must be square (the world is), got {w}x{h}")
    full = GRID * tile_size
    src = img.convert("RGB")
    if src.size != (full, full):
        src = src.resize((full, full), resample)
    out = []
    for index in range(NUM_TILES):
        col, row = tile_colrow(index)
        box = (col * tile_size, row * tile_size, (col + 1) * tile_size, (row + 1) * tile_size)
        name = tile_name(index)
        out.append((name, name, src.crop(box)))
    return out


def assemble_radar(tiles: Sequence, tile_size: int | None = None) -> Image.Image:
    """Inverse of :func:`split_radar_image` (accepts its output or a list of 144 images)."""
    ims = [t[2] if isinstance(t, tuple) else t for t in tiles]
    if len(ims) != NUM_TILES:
        raise ValueError(f"need {NUM_TILES} tiles")
    ts = tile_size or ims[0].size[0]
    full = Image.new("RGB", (GRID * ts, GRID * ts))
    for index, im in enumerate(ims):
        col, row = tile_colrow(index)
        if im.size != (ts, ts):
            im = im.resize((ts, ts), Image.LANCZOS)
        full.paste(im.convert("RGB"), (col * ts, row * ts))
    return full


def build_radar_txds(tiles: Sequence) -> list:
    """[(file name 'radarNN.txd', TXD bytes)] using tools.rw.txd (DXT1, one level, vanilla layout).

    Kept optional: tools.rw.txd is maintained separately; this only calls its documented
    ``texture_native(name, img, encoding, mipmaps=...)`` and ``write_txd([raster_bytes])``."""
    from tools.rw import txd as rwtxd   # imported lazily
    out = []
    for txd_name, tex_name, im in tiles:
        raster = rwtxd.texture_native(tex_name, im.convert("RGBA"), TXD_ENCODING, mipmaps=TXD_MIPMAPS)
        out.append((txd_name + ".txd", rwtxd.write_txd([raster])))
    return out
