"""GTA San Andreas ``data/water.dat`` writer, parser, validator and engine simulation.

The rules implemented here are documented (with sources) in ``docs/notes/data_files.md`` section (a).
Short version of what the game (CWaterLevel, gta_sa.exe 1.0) does with the file:

* ``WaterLevelInitialise`` (0x6EAE80) reads ``DATA\\water.dat`` line by line with
  ``CFileLoader::LoadLine`` (fgets 512 bytes, chars < 0x20 and ',' become spaces, leading
  whitespace skipped).  Lines whose first character is ``;``, ``*``, ``p`` or NUL are skipped
  (that is how the ``processed`` header is ignored).  **'#' is NOT a comment** - such a line is
  turned into a triangle made of the previous line's values.
* A line with >= 28 floats is a quad, otherwise a triangle (21 floats); an optional trailing
  integer is the flag word (default 1).  Each vertex is ``x y z flowX flowY bigWaves smallWaves``.
* x/y are truncated to int (int16 storage), flow is stored as int8(flow*64).
* Vertices are merged when (x, y, float32 z) are identical (``AddWaterLevelVertex`` 0x6E5A40);
  a vertex with |x| or |y| >= 3000 is clamped to the edge and gets z=0, bigWaves=1, smallWaves=0.
* Fixed pools without bounds checks: 1021 vertices, 301 quads, 6 triangles, 700 block-list words.
* Quads are axis-aligned rectangles, triangles are right triangles with a horizontal and a
  vertical leg; the high-detail renderer divides by half the width/height of every piece, so all
  coordinates must be even (and triangles 45 degrees) or the game can crash with a division by 0.
"""
from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# --- engine constants (see docs/notes/data_files.md, section a) --------------------------------
WORLD_LIMIT = 3000            # AddWaterLevelVertex clamps |x|,|y| >= 3000
BLOCK_SIZE = 500              # CWaterLevel::WATER_BLOCK_SIZE
NUM_BLOCKS = 12               # 6000 / 500
MAX_VERTICES = 1021           # CWaterLevel::m_aVertices[1021]   @0xC22910 (MTA: 0x3FD)
MAX_QUADS = 301               # CWaterLevel::WaterQuads[301]     @0xC21C90 (MTA: 0x12D)
MAX_TRIANGLES = 6             # CWaterLevel::WaterTriangles[6]   @0xC22854
MAX_BLOCK_LIST = 700          # CWaterLevel::m_PolyCombos[700]   @0xC215F8 (MTA: 0x2BC)
MAX_LINE_CHARS = 510          # fgets(ms_line, 512): text + '\n' + NUL must fit
INTERIOR_Z = 950.0            # CWaterPolygon::IsInInterior(): first vertex z > 950
LIMITED_DEPTH = 6.0           # GetWaterLevelNoWavesForQuad: limited && level - 6 > z -> no water
MAX_HEIGHT_ABOVE = 20.0       # level + 20 < z -> this poly is ignored
FLAG_VISIBLE = 1              # (flags & 1) == 0 -> bInvisible
FLAG_LIMITED_DEPTH = 2        # (flags & 2) != 0 -> bLimitedDepth
HEADER = "processed"          # vanilla first line (skipped by the parser because it starts with 'p')

# values the engine forces on vertices clamped to the world edge (AddWaterLevelVertex)
EDGE_Z, EDGE_BIG_WAVES, EDGE_SMALL_WAVES = 0.0, 1.0, 0.0


def f32(v: float) -> float:
    """Round a Python float to IEEE single precision (what sscanf("%f") stores)."""
    return struct.unpack("<f", struct.pack("<f", float(v)))[0]


class WaterError(ValueError):
    """Raised when water data violates an engine rule or limit."""


@dataclass(frozen=True)
class WaterVertex:
    x: int
    y: int
    z: float = 0.0
    flow_x: float = 0.0
    flow_y: float = 0.0
    big_waves: float = 0.0
    small_waves: float = 0.0


@dataclass
class WaterPoly:
    """One water.dat line: 4 vertices (quad) or 3 vertices (triangle)."""
    verts: tuple
    visible: bool = True
    limited_depth: bool = False

    @property
    def is_quad(self) -> bool:
        return len(self.verts) == 4

    @property
    def flags(self) -> int:
        return (FLAG_VISIBLE if self.visible else 0) | (FLAG_LIMITED_DEPTH if self.limited_depth else 0)


# --- constructors ------------------------------------------------------------------------------
def _waves4(value, n):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return [float(value)] * n
    value = list(value)
    if len(value) != n:
        raise WaterError(f"expected {n} per-corner values, got {len(value)}")
    return [float(v) for v in value]


def quad(x0: int, y0: int, x1: int, y1: int, z: float = 0.0, *, big_waves=0.0, small_waves=0.0,
         flow=(0.0, 0.0), visible: bool = True, limited_depth: bool = False,
         z_corners: Sequence[float] | None = None) -> WaterPoly:
    """Axis-aligned water rectangle [x0,x1] x [y0,y1] (even integer coordinates).

    Vertices are emitted in the vanilla order SW, SE, NW, NE (min-y pair first, each pair sorted
    by x), which is also the order the engine sorts them into.  ``z_corners``/``big_waves``/
    ``small_waves`` may be scalars or 4 values in that SW, SE, NW, NE order.
    """
    xa, xb = sorted((int(x0), int(x1)))
    ya, yb = sorted((int(y0), int(y1)))
    zs = _waves4(z_corners, 4) or [float(z)] * 4
    bw = _waves4(big_waves, 4)
    sw = _waves4(small_waves, 4)
    fx, fy = float(flow[0]), float(flow[1])
    pts = [(xa, ya), (xb, ya), (xa, yb), (xb, yb)]
    verts = tuple(WaterVertex(px, py, zs[i], fx, fy, bw[i], sw[i]) for i, (px, py) in enumerate(pts))
    return WaterPoly(verts, visible, limited_depth)


def triangle(x: int, y: int, dx: int, dy: int, z: float = 0.0, *, big_waves=0.0, small_waves=0.0,
             flow=(0.0, 0.0), visible: bool = True, limited_depth: bool = False) -> WaterPoly:
    """Right isosceles water triangle with the right angle at (x, y).

    Legs go to (x+dx, y) (horizontal) and (x, y+dy) (vertical); |dx| must equal |dy| (45 degree
    hypotenuse, like all 6 vanilla triangles).  Output order: horizontal pair sorted by x, then
    the third vertex (the order AddWaterLevelTriangle sorts into).
    """
    x, y, dx, dy = int(x), int(y), int(dx), int(dy)
    a, b, c = (x, y), (x + dx, y), (x, y + dy)
    p, q = sorted((a, b))
    bw = _waves4(big_waves, 3)
    sw = _waves4(small_waves, 3)
    fx, fy = float(flow[0]), float(flow[1])
    verts = tuple(WaterVertex(px, py, float(z), fx, fy, bw[i], sw[i]) for i, (px, py) in enumerate((p, q, c)))
    return WaterPoly(verts, visible, limited_depth)


def snap_even(v: float, mode: str = "nearest") -> int:
    """Snap a coordinate to an even integer (the engine requires even water coordinates)."""
    if mode == "down":
        return int(math.floor(v / 2.0)) * 2
    if mode == "up":
        return int(math.ceil(v / 2.0)) * 2
    return int(round(v / 2.0)) * 2


# --- text format -------------------------------------------------------------------------------
def _num(v: float, decimals: int) -> str:
    s = f"{v:.{decimals}f}"
    return "0." + "0" * decimals if s.startswith("-") and float(s) == 0.0 else s  # no "-0.00000"


def format_vertex(v: WaterVertex) -> str:
    return " ".join((_num(v.x, 1), _num(v.y, 1), _num(v.z, 5), _num(v.flow_x, 5), _num(v.flow_y, 5),
                     _num(v.big_waves, 5), _num(v.small_waves, 5)))


def format_line(p: WaterPoly) -> str:
    """One line in the vanilla layout: vertices separated by 4 spaces, 2 spaces, integer flag."""
    return "    ".join(format_vertex(v) for v in p.verts) + f"  {p.flags}"


def format_water_dat(polys: Iterable[WaterPoly], *, header: str = HEADER, newline: str = "\n",
                     validate: bool = True) -> str:
    """Build the water.dat text.  Raises WaterError if ``validate`` and a rule/limit is broken."""
    polys = list(polys)
    if validate:
        validate_water(polys)
    if header and header[0] not in ";*p":
        raise WaterError("header line must start with ';', '*' or 'p' or the game parses it as data")
    lines = ([header] if header else []) + [format_line(p) for p in polys]
    return newline.join(lines) + newline


def write_water_dat(path, polys: Iterable[WaterPoly], **kw) -> "WaterStats":
    polys = list(polys)
    stats = validate_water(polys)
    text = format_water_dat(polys, validate=False, **kw)
    with open(path, "w", encoding="ascii", newline="") as f:
        f.write(text)
    return stats


# --- parser (mirrors 0x6EAE80 / euryopa InitialiseWater) ---------------------------------------
_FLOAT_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_INT_PREFIX_RE = re.compile(r"[+-]?\d+")


def sanitize_line(raw: str) -> str:
    """CFileLoader::LoadLine: chars < 0x20 and ',' -> ' ', then skip leading chars <= ' '."""
    s = "".join(" " if (ord(ch) < 0x20 or ch == ",") else ch for ch in raw)
    i = 0
    while i < len(s) and ord(s[i]) <= 0x20:
        i += 1
    return s[i:]


def _leading_floats(tokens):
    out = []
    for t in tokens:
        if not _FLOAT_RE.match(t):
            break
        out.append(f32(float(t)))
    return out


def _vertex_from(vals) -> WaterVertex:
    x, y, z, fx, fy, bw, sw = vals
    return WaterVertex(int(math.trunc(x)), int(math.trunc(y)), z, fx, fy, bw, sw)


def parse_water_dat(text: str) -> list:
    """Parse water.dat text like the game; raises WaterError for lines the game would misread."""
    polys = []
    for lineno, raw in enumerate(text.split("\n"), 1):
        line = sanitize_line(raw)
        if not line or line[0] in ";*p":
            continue
        toks = line.split()
        nums = _leading_floats(toks)
        if len(nums) >= 28:
            nv, flag_tok = 4, toks[28] if len(toks) > 28 else None
        elif len(nums) >= 21:
            nv, flag_tok = 3, toks[21] if len(toks) > 21 else None
        else:
            raise WaterError(f"line {lineno}: only {len(nums)} numbers; the game would add a garbage "
                             f"triangle from the previous line's values: {raw[:60]!r}")
        flags = 1
        if flag_tok is not None:
            m = _INT_PREFIX_RE.match(flag_tok)
            if m:
                flags = int(m.group(0))
        verts = tuple(_vertex_from(nums[i * 7:(i + 1) * 7]) for i in range(nv))
        polys.append(WaterPoly(verts, visible=bool(flags & 1), limited_depth=bool(flags & 2)))
    return polys


# --- engine simulation -------------------------------------------------------------------------
@dataclass
class GameVertex:
    x: int
    y: int
    z: float
    big_waves: float
    small_waves: float
    flow_x: int
    flow_y: int


@dataclass
class GamePoly:
    idx: tuple            # vertex indices in engine order
    invisible: bool
    limited_depth: bool
    source: int           # index into the input list


def _i8(v: float) -> int:
    n = int(math.trunc(f32(v) * 64.0)) & 0xFF
    return n - 256 if n > 127 else n


class GameWater:
    """What CWaterLevel holds after WaterLevelInitialise() for the given polys (no bounds checks:
    counts may exceed the real pools so that validation can report by how much)."""

    def __init__(self, polys: Sequence[WaterPoly]):
        self.vertices: list = []
        self._vmap: dict = {}
        self.quads: list = []
        self.triangles: list = []
        self.skipped: list = []           # degenerate polys dropped by the engine
        for i, p in enumerate(polys):
            if p.is_quad:
                self._add_quad(p, i)
            else:
                self._add_triangle(p, i)
        self._fill_blocks()

    # AddWaterLevelVertex 0x6E5A40
    def add_vertex(self, v: WaterVertex) -> int:
        x, y = int(v.x), int(v.y)
        z, bw, sw = f32(v.z), f32(v.big_waves), f32(v.small_waves)
        fx, fy = _i8(v.flow_x), _i8(v.flow_y)
        clamped = False
        if x <= -WORLD_LIMIT:
            x, clamped = -WORLD_LIMIT, True
        if x >= WORLD_LIMIT:
            x, clamped = WORLD_LIMIT, True
        if y <= -WORLD_LIMIT:
            y, clamped = -WORLD_LIMIT, True
        if y >= WORLD_LIMIT:
            y, clamped = WORLD_LIMIT, True
        if clamped:
            z, bw, sw, fx, fy = EDGE_Z, EDGE_BIG_WAVES, EDGE_SMALL_WAVES, 0, 0
        key = (x, y, z)
        if key in self._vmap:
            return self._vmap[key]
        self._vmap[key] = len(self.vertices)
        self.vertices.append(GameVertex(x, y, z, bw, sw, fx, fy))
        return len(self.vertices) - 1

    def _add_quad(self, p: WaterPoly, src: int):
        xs = [v.x for v in p.verts]
        ys = [v.y for v in p.verts]
        if len(set(xs)) == 1 or len(set(ys)) == 1:      # AddWaterLevelQuad early return
            self.skipped.append(src)
            return
        ids = [self.add_vertex(v) for v in p.verts]
        miny = min(self.vertices[i].y for i in ids)
        a = [i for i in ids if self.vertices[i].y == miny]
        b = [i for i in ids if self.vertices[i].y != miny]
        a.sort(key=lambda i: self.vertices[i].x)
        b.sort(key=lambda i: self.vertices[i].x)
        self.quads.append(GamePoly(tuple((a + b)[:4]), not p.visible, p.limited_depth, src))

    def _add_triangle(self, p: WaterPoly, src: int):
        xs = [v.x for v in p.verts]
        ys = [v.y for v in p.verts]
        if len(set(xs)) == 1 or len(set(ys)) == 1:      # AddWaterLevelTriangle early return
            self.skipped.append(src)
            return
        v0, v1, v2 = (self.add_vertex(v) for v in p.verts)
        a, b, c = self.vertices[v0], self.vertices[v1], self.vertices[v2]
        if a.y == b.y:
            i0, i1 = (v0, v1) if a.x < b.x else (v1, v0)
            i2 = v2
        elif a.y == c.y:
            i0, i1 = (v0, v2) if a.x < c.x else (v2, v0)
            i2 = v1
        else:
            i0, i1 = (v1, v2) if b.x < c.x else (v2, v1)
            i2 = v0
        self.triangles.append(GamePoly((i0, i1, i2), not p.visible, p.limited_depth, src))

    # FillQuadsAndTrianglesList 0x6E7B30 + AddToQuadsAndTrianglesList 0x6E5750
    def _fill_blocks(self):
        V = self.vertices
        self.blocks = [[[] for _ in range(NUM_BLOCKS)] for _ in range(NUM_BLOCKS)]
        for bx in range(NUM_BLOCKS):
            xlo = bx * BLOCK_SIZE - WORLD_LIMIT
            xhi = xlo + BLOCK_SIZE
            for by in range(NUM_BLOCKS):
                ylo = by * BLOCK_SIZE - WORLD_LIMIT
                yhi = ylo + BLOCK_SIZE
                lst = self.blocks[bx][by]
                for qi, q in enumerate(self.quads):
                    v0, v1, v2 = V[q.idx[0]], V[q.idx[1]], V[q.idx[2]]
                    if v1.x > xlo and v0.x < xhi and v2.y > ylo and v0.y < yhi:
                        lst.append(("Q", qi))
                for ti, t in enumerate(self.triangles):
                    v0, v1, v2 = V[t.idx[0]], V[t.idx[1]], V[t.idx[2]]
                    if v1.x > xlo and v0.x < xhi and max(v0.y, v2.y) > ylo and min(v0.y, v2.y) < yhi:
                        lst.append(("T", ti))
        # a block with 1 poly stores it inline; n >= 2 polys use n + 1 words of m_PolyCombos
        self.block_list_words = sum(len(l) + 1 for row in self.blocks for l in row if len(l) >= 2)

    # GetWaterLevelNoWaves 0x6E8580 (+ ForQuad 0x6E5BB0 / ForTriangle 0x6E5E90)
    def level_at(self, x: float, y: float, z: float = 0.0):
        """Water level (no waves) at x,y for a point at height z, or None if not in water."""
        bx = math.floor(x * 0.002 + 6.0)
        by = math.floor(y * 0.002 + 6.0)
        if not (0 <= bx < NUM_BLOCKS and 0 <= by < NUM_BLOCKS):
            return 0.0                                   # endless ocean outside +-3000
        for kind, i in self.blocks[bx][by]:
            lvl = self._quad_level(self.quads[i], x, y, z) if kind == "Q" else \
                self._tri_level(self.triangles[i], x, y, z)
            if lvl is not None:
                return lvl
        return None

    def _depth_ok(self, level, poly, z):
        if level - LIMITED_DEPTH > z and poly.limited_depth:
            return None
        if level + MAX_HEIGHT_ABOVE < z:
            return None
        return level

    def _quad_level(self, q, x, y, z):
        v0, v1, v2, v3 = (self.vertices[i] for i in q.idx)
        if not (v0.x <= x <= v1.x and v0.y <= y <= v2.y):
            return None
        u = (x - v0.x) / (v1.x - v0.x)
        v = (y - v0.y) / (v2.y - v0.y)
        if u + v <= 1.0:
            lvl = (v2.z - v0.z) * v + (v1.z - v0.z) * u + v0.z
        else:
            lvl = (v2.z - v3.z) * (1.0 - u) + (v1.z - v3.z) * (1.0 - v) + v3.z
        return self._depth_ok(lvl, q, z)

    def _tri_level(self, t, x, y, z):
        v0, v1, v2 = (self.vertices[i] for i in t.idx)
        if not (v0.x <= x <= v1.x and min(v0.y, v2.y) <= y <= max(v0.y, v2.y)):
            return None
        u = (x - v0.x) / (v1.x - v0.x)
        v = (y - v0.y) / (v2.y - v0.y)
        if v0.x == v2.x:
            if u + v > 1.0:
                return None
            lvl = (v2.z - v0.z) * v + (v1.z - v0.z) * u + v0.z
        else:
            if u < v:
                return None
            lvl = (v2.z - v1.z) * v + (v0.z - v1.z) * (1.0 - u) + v1.z
        return self._depth_ok(lvl, t, z)

    def is_interior(self, poly: GamePoly) -> bool:
        return self.vertices[poly.idx[0]].z > INTERIOR_Z


# --- validation --------------------------------------------------------------------------------
@dataclass
class WaterStats:
    quads: int
    triangles: int
    vertices: int
    block_list_words: int
    max_polys_per_block: int
    blocks_with_water: int
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def headroom(self) -> dict:
        return {"quads": MAX_QUADS - self.quads, "triangles": MAX_TRIANGLES - self.triangles,
                "vertices": MAX_VERTICES - self.vertices, "block_list": MAX_BLOCK_LIST - self.block_list_words}


def _check_poly(i: int, p: WaterPoly, errors: list, warnings: list):
    tag = f"poly {i} ({'quad' if p.is_quad else 'triangle'})"
    if len(p.verts) not in (3, 4):
        errors.append(f"{tag}: needs 3 or 4 vertices")
        return
    for v in p.verts:
        if not (isinstance(v.x, int) and isinstance(v.y, int)):
            errors.append(f"{tag}: x/y must be ints (the game truncates), got {v.x!r},{v.y!r}")
            return
        if v.x % 2 or v.y % 2:
            errors.append(f"{tag}: odd coordinate ({v.x},{v.y}); water coordinates must be even "
                          f"(high-detail renderer divides by half-width -> division by zero)")
        if abs(v.x) > WORLD_LIMIT or abs(v.y) > WORLD_LIMIT:
            errors.append(f"{tag}: vertex ({v.x},{v.y}) outside +-{WORLD_LIMIT}")
        on_edge = abs(v.x) == WORLD_LIMIT or abs(v.y) == WORLD_LIMIT
        if on_edge and f32(v.z) != 0.0:
            errors.append(f"{tag}: vertex ({v.x},{v.y}) on the world edge must have z=0 "
                          f"(the engine forces z=0, bigWaves=1, smallWaves=0 there)")
        for name in ("z", "flow_x", "flow_y", "big_waves", "small_waves"):
            val = getattr(v, name)
            if not math.isfinite(val):
                errors.append(f"{tag}: {name} not finite")
        if v.big_waves < 0 or v.small_waves < 0:
            errors.append(f"{tag}: negative wave amplitude")
        for fl in (v.flow_x, v.flow_y):
            if not -2.0 <= fl <= 127 / 64:
                errors.append(f"{tag}: flow {fl} outside int8/64 range [-2, 1.984]")
    xs = sorted({v.x for v in p.verts})
    ys = sorted({v.y for v in p.verts})
    if p.is_quad:
        pts = {(v.x, v.y) for v in p.verts}
        if len(xs) != 2 or len(ys) != 2 or pts != {(x, y) for x in xs for y in ys}:
            errors.append(f"{tag}: not an axis-aligned rectangle {sorted(pts)}")
        elif xs[1] - xs[0] < 2 or ys[1] - ys[0] < 2:
            errors.append(f"{tag}: rectangle thinner than 2 units")
    else:
        (a, b, c) = [(v.x, v.y) for v in p.verts]
        pairs = [(a, b, c), (a, c, b), (b, c, a)]
        horiz = [(p1, p2, p3) for p1, p2, p3 in pairs if p1[1] == p2[1]]
        if len(horiz) != 1:
            errors.append(f"{tag}: needs exactly one horizontal edge, got {a},{b},{c}")
        else:
            p1, p2, p3 = horiz[0]
            if p3[0] not in (p1[0], p2[0]):
                errors.append(f"{tag}: third vertex must be vertically above/below one end of the "
                              f"horizontal edge (right angle), got {a},{b},{c}")
            elif abs(p2[0] - p1[0]) != abs(p3[1] - p1[1]):
                errors.append(f"{tag}: legs {abs(p2[0] - p1[0])} and {abs(p3[1] - p1[1])} differ; "
                              f"only 45-degree triangles render without cracks/odd splits")
    if len(format_line(p)) > MAX_LINE_CHARS:
        errors.append(f"{tag}: line longer than {MAX_LINE_CHARS} chars")


def validate_water(polys: Sequence[WaterPoly], *, raise_on_error: bool = True) -> WaterStats:
    """Check every engine rule/limit; returns WaterStats (raises WaterError on violations)."""
    errors: list = []
    warnings: list = []
    for i, p in enumerate(polys):
        _check_poly(i, p, errors, warnings)
    gw = GameWater(polys)
    for src in gw.skipped:
        errors.append(f"poly {src}: degenerate (all x or all y equal) - the engine silently drops it")
    for poly in gw.quads + gw.triangles:
        if gw.is_interior(poly):
            warnings.append(f"poly {poly.source}: first vertex z > {INTERIOR_Z}: interior water, "
                            f"rendered only when the camera is in an interior area")
    # shared vertices keep the wave/flow values of the first poly that created them
    seen = {}
    for i, p in enumerate(polys):
        for v in p.verts:
            if abs(v.x) >= WORLD_LIMIT or abs(v.y) >= WORLD_LIMIT:
                continue
            key = (v.x, v.y, f32(v.z))
            val = (f32(v.big_waves), f32(v.small_waves), _i8(v.flow_x), _i8(v.flow_y))
            if key in seen and seen[key][1] != val:
                warnings.append(f"poly {i}: vertex {key[:2]} shared with poly {seen[key][0]} but has "
                                f"different waves/flow; the engine keeps poly {seen[key][0]}'s values")
            seen.setdefault(key, (i, val))
    nq, nt, nv, nb = len(gw.quads), len(gw.triangles), len(gw.vertices), gw.block_list_words
    if nq > MAX_QUADS:
        errors.append(f"{nq} quads > {MAX_QUADS} (WaterQuads overflow corrupts WaterTriangles)")
    if nt > MAX_TRIANGLES:
        errors.append(f"{nt} triangles > {MAX_TRIANGLES} (overflow corrupts the water counters)")
    if nv > MAX_VERTICES:
        errors.append(f"{nv} unique vertices > {MAX_VERTICES}")
    if nb > MAX_BLOCK_LIST:
        errors.append(f"{nb} block-list words > {MAX_BLOCK_LIST} (m_PolyCombos overflow)")
    counts = [len(l) for row in gw.blocks for l in row]
    stats = WaterStats(nq, nt, nv, nb, max(counts) if counts else 0, sum(1 for c in counts if c),
                       warnings, errors)
    if errors and raise_on_error:
        raise WaterError("water.dat invalid:\n  " + "\n  ".join(errors[:50]) +
                         (f"\n  ... {len(errors) - 50} more" if len(errors) > 50 else ""))
    return stats


# --- helpers for building the water set --------------------------------------------------------
def rects_from_mask(mask, x0: int, y0: int, cell: int) -> list:
    """Greedy decomposition of a boolean grid into axis-aligned rectangles.

    ``mask[j][i]`` True means water in x:[x0+i*cell, x0+(i+1)*cell], y:[y0+j*cell, y0+(j+1)*cell]
    (row j grows towards +Y = north).  ``x0``, ``y0`` and ``cell`` must be even so all corners
    are even.  Returns (xmin, ymin, xmax, ymax) tuples; feed them to :func:`quad`.
    """
    if cell % 2 or x0 % 2 or y0 % 2:
        raise WaterError("x0, y0 and cell must be even")
    rows = [list(map(bool, r)) for r in mask]
    h_n = len(rows)
    w_n = len(rows[0]) if rows else 0
    out = []
    for j in range(h_n):
        i = 0
        while i < w_n:
            if not rows[j][i]:
                i += 1
                continue
            w = 1
            while i + w < w_n and rows[j][i + w]:
                w += 1
            h = 1
            while j + h < h_n and all(rows[j + h][i:i + w]):
                h += 1
            for jj in range(j, j + h):
                for ii in range(i, i + w):
                    rows[jj][ii] = False
            out.append((x0 + i * cell, y0 + j * cell, x0 + (i + w) * cell, y0 + (j + h) * cell))
            i += w
    return out


def summary(stats: WaterStats) -> str:
    return (f"quads {stats.quads}/{MAX_QUADS}, triangles {stats.triangles}/{MAX_TRIANGLES}, "
            f"vertices {stats.vertices}/{MAX_VERTICES}, block-list words "
            f"{stats.block_list_words}/{MAX_BLOCK_LIST}, max polys per 500x500 block "
            f"{stats.max_polys_per_block}")
