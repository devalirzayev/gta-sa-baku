"""GTA San Andreas zone files (``data/info.zon``, ``data/map.zon``) and zone-name text helpers.

Rules (sources in docs/notes/data_files.md section b/c):

* Both files are IPL-style text loaded through ``gta.dat`` ``IPL`` lines -> ``CFileLoader::LoadScene``
  (section ``zone`` ... ``end``; '#' lines are comments) -> ``CFileLoader::LoadZone``:
  ``sscanf("%s %d %f %f %f %f %f %f %d %s")`` = name, type, x1, y1, z1, x2, y2, z2, level, gxt-key
  (commas are turned into spaces by LoadLine, so the vanilla ``a, b, c`` layout works).
* ``CTheZones::CreateZone``: type 0/1 -> navigation (info) zone, 3 -> map zone, 2 -> ignored.
  Labels are upper-cased and cut to 7 chars, coordinates truncated to int16 (min/max sorted).
* Limits (no bounds checks): 380 navigation zones and 380 zone infos including the built-in
  ``SAN_AND`` (whole map), 39 map zones including the built-in ``THEMAP``.
* Display: ``CPlaceName::Process`` shows ``TheText.Get(zone.gxt)`` of the *smallest*
  (dx+dy) navigation zone whose 3-D box contains the player; outside all zones ``SAN_AND``.
* Map-zone ``level`` (0 countryside, 1 LS, 2 SF, 3 LV) drives ``CTheZones::m_CurrLevel``
  (cop/ambulance/fire models, cop bikes, emergency-vehicle generation, shop prices, train stations).
"""
from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

ZONE_NAVI = 0          # ZONE_TYPE_NAVI      (all vanilla info.zon lines)
ZONE_LOCAL_NAVI = 1    # ZONE_TYPE_LOCAL_NAVI (navigation array too; ignored by "type 0 only" lookups)
ZONE_INFO = 2          # ignored by CreateZone -> never write it
ZONE_MAP = 3           # ZONE_TYPE_MAP       (map.zon)

LEVEL_COUNTRYSIDE, LEVEL_LOS_SANTOS, LEVEL_SAN_FIERRO, LEVEL_LAS_VENTURAS = 0, 1, 2, 3

MAX_NAVI_ZONES = 380   # CTheZones::NavigationZoneArray[380] (index 0 = SAN_AND)
MAX_ZONE_INFOS = 380   # CTheZones::ZoneInfoArray[380]       (index 0 = SAN_AND's)
MAX_MAP_ZONES = 39     # CTheZones::MapZoneArray[39]         (index 0 = THEMAP)
LABEL_LEN = 7          # char[8] labels
BUILTIN = {"SAN_AND", "THEMAP"}

# eZonePopulationType (ZoneInfo.h) == block order in popcycle.dat; set per info label from
# main.scm with SET_ZONE_POPULATION_TYPE (0x0767).  Default for every zone: RESIDENTIAL_AVERAGE.
POP_TYPES = {name: i for i, name in enumerate((
    "BUSINESS", "DESERT", "ENTERTAINMENT", "COUNTRYSIDE", "RESIDENTIAL_RICH", "RESIDENTIAL_AVERAGE",
    "RESIDENTIAL_POOR", "GANGLAND", "BEACH", "SHOPPING", "PARK", "INDUSTRY", "ENTERTAINMENT_BUSY",
    "SHOPPING_BUSY", "SHOPPING_POSH", "RESIDENTIAL_RICH_SECLUDED", "AIRPORT", "GOLF_CLUB",
    "OUT_OF_TOWN_FACTORY", "AIRPORT_RUNWAY"))}

_LABEL_RE = re.compile(r"^[A-Z0-9_]{1,7}$")


class ZoneError(ValueError):
    pass


@dataclass
class Zone:
    name: str              # info label: script handle, groups zones sharing one CZoneInfo
    type: int              # ZONE_NAVI / ZONE_LOCAL_NAVI / ZONE_MAP
    x1: float
    y1: float
    z1: float
    x2: float
    y2: float
    z2: float
    level: int = 0
    gxt: str = ""          # display key (TheText.Get); "UNUSED" for map zones

    def stored_box(self):
        """Box as stored by CreateZone: truncated to int16 and min/max ordered."""
        def t(v):
            n = int(v)                     # C cast truncates toward zero
            return ((n + 0x8000) & 0xFFFF) - 0x8000
        xs = sorted((t(self.x1), t(self.x2)))
        ys = sorted((t(self.y1), t(self.y2)))
        zs = sorted((t(self.z1), t(self.z2)))
        return xs[0], ys[0], zs[0], xs[1], ys[1], zs[1]

    def contains(self, x, y, z) -> bool:   # CTheZones::PointLiesWithinZone (inclusive)
        x1, y1, z1, x2, y2, z2 = self.stored_box()
        return x1 <= x <= x2 and y1 <= y <= y2 and z1 <= z <= z2


def navi_zone(name: str, x1, y1, x2, y2, z1=-100.0, z2=900.0, gxt: str | None = None,
              local: bool = False, level: int = 1) -> Zone:
    """info.zon entry.  ``gxt`` defaults to ``name``.  z range default -100..900 (below the
    SA interior height band at ~1000).  ``level`` is ignored by the engine for navigation zones
    (vanilla writes 1)."""
    return Zone(name.upper(), ZONE_LOCAL_NAVI if local else ZONE_NAVI, x1, y1, z1, x2, y2, z2, level,
                (gxt or name).upper())


def map_zone(name: str, x1, y1, x2, y2, level: int, z1=-500.0, z2=500.0) -> Zone:
    """map.zon entry (vanilla: z -500..500, gxt "UNUSED")."""
    return Zone(name.upper(), ZONE_MAP, x1, y1, z1, x2, y2, z2, level, "UNUSED")


def _fnum(v: float) -> str:
    s = f"{float(v):.3f}".rstrip("0")
    if s.endswith("."):
        s += "0"
    return "0.0" if s == "-0.0" else s


def format_zone_line(z: Zone) -> str:
    return ", ".join((z.name, str(z.type), _fnum(z.x1), _fnum(z.y1), _fnum(z.z1), _fnum(z.x2),
                      _fnum(z.y2), _fnum(z.z2), str(z.level), z.gxt))


def format_zon(zones: Iterable[Zone], *, comment: str | None = None, newline: str = "\n",
               validate: bool = True) -> str:
    zones = list(zones)
    if validate:
        validate_zone_file(zones)
    lines = []
    if comment:
        lines += ["# " + c for c in comment.splitlines()]
    lines += ["zone"] + [format_zone_line(z) for z in zones] + ["end"]
    return newline.join(lines) + newline


def write_zon(path, zones: Iterable[Zone], **kw):
    text = format_zon(zones, **kw)
    with open(path, "w", encoding="ascii", newline="") as f:
        f.write(text)


def parse_zon(text: str) -> list:
    """Parse zone sections like CFileLoader::LoadScene + LoadZone."""
    out = []
    in_zone = False
    for raw in text.split("\n"):
        line = "".join(" " if (ord(c) < 0x20 or c == ",") else c for c in raw).lstrip()
        if not line or line.startswith("#"):
            continue
        if not in_zone:
            in_zone = line.startswith("zone")
            continue
        if line.startswith("end"):
            in_zone = False
            continue
        t = line.split()
        if len(t) < 10:
            raise ZoneError(f"zone line needs 10 fields: {raw!r}")
        out.append(Zone(t[0].upper()[:LABEL_LEN], int(t[1]), float(t[2]), float(t[3]), float(t[4]),
                        float(t[5]), float(t[6]), float(t[7]), int(t[8]), t[9].upper()[:LABEL_LEN]))
    return out


def validate_zone_file(zones: Sequence[Zone]):
    """Checks for one .zon file (labels, types, coordinates)."""
    errors = []
    for i, z in enumerate(zones):
        tag = f"zone {i} {z.name!r}"
        for what, lab in (("name", z.name), ("gxt", z.gxt)):
            if not _LABEL_RE.match(lab):
                errors.append(f"{tag}: {what} {lab!r} must be 1-7 chars of A-Z 0-9 _ "
                              f"(sscanf %s buffers are 24/12 bytes, CZone stores 7)")
        if z.type not in (ZONE_NAVI, ZONE_LOCAL_NAVI, ZONE_MAP):
            errors.append(f"{tag}: type {z.type} (only 0, 1 navigation and 3 map are created)")
        if z.level not in (0, 1, 2, 3):
            errors.append(f"{tag}: level {z.level} not in 0..3")
        for v in (z.x1, z.y1, z.z1, z.x2, z.y2, z.z2):
            if not -32768 <= v <= 32767:
                errors.append(f"{tag}: coordinate {v} outside int16")
        x1, y1, z1, x2, y2, z2 = z.stored_box()
        if x1 == x2 or y1 == y2 or z1 == z2:
            errors.append(f"{tag}: empty box after int16 truncation")
        if z.name in BUILTIN:
            errors.append(f"{tag}: {z.name} is created by CTheZones::Init; don't redefine it")
    if errors:
        raise ZoneError("\n".join(errors))


def validate_zones(info_zones: Sequence[Zone], map_zones: Sequence[Zone]) -> dict:
    """Cross-file limits.  Returns counts; raises ZoneError when an engine array would overflow."""
    validate_zone_file(info_zones)
    validate_zone_file(map_zones)
    errors = []
    navi = [z for z in info_zones if z.type in (ZONE_NAVI, ZONE_LOCAL_NAVI)]
    maps = [z for z in map_zones if z.type == ZONE_MAP]
    if len(navi) != len(info_zones) or len(maps) != len(map_zones):
        errors.append("info.zon must only contain type 0/1 zones and map.zon only type 3 zones")
    labels = {z.name for z in navi}
    if 1 + len(navi) > MAX_NAVI_ZONES:
        errors.append(f"{len(navi)} navigation zones > {MAX_NAVI_ZONES - 1}")
    if 1 + len(labels) > MAX_ZONE_INFOS:
        errors.append(f"{len(labels)} distinct info labels > {MAX_ZONE_INFOS - 1}")
    if 1 + len(maps) > MAX_MAP_ZONES:
        errors.append(f"{len(maps)} map zones > {MAX_MAP_ZONES - 1}")
    if errors:
        raise ZoneError("\n".join(errors))
    return {"navigation_zones": len(navi) + 1, "zone_infos": len(labels) + 1, "map_zones": len(maps) + 1}


# --- engine lookups (for checking a layout) -----------------------------------------------------
SAN_AND = Zone("SAN_AND", ZONE_NAVI, -3000, -3000, -2000, 3000, 3000, 2000, 0, "SAN_AND")
THEMAP = Zone("THEMAP", ZONE_MAP, -3000, -3000, -2000, 3000, 3000, 2000, 0, "THEMAP")


def zone_at(info_zones: Sequence[Zone], x, y, z, type0_only: bool = False) -> Zone:
    """CTheZones::FindSmallestZoneForPosition: smallest (dx+dy) containing navigation zone.

    Zone 0 (SAN_AND) is the starting candidate, so ties keep the earlier zone and a point outside
    every zone (or outside SAN_AND itself) returns SAN_AND."""
    def size(zz):
        x1, y1, _, x2, y2, _ = zz.stored_box()
        return x2 - x1 + y2 - y1
    best, best_size = SAN_AND, size(SAN_AND)
    for zz in [SAN_AND] + list(info_zones):
        if type0_only and zz.type != ZONE_NAVI:
            continue
        if zz.contains(x, y, z) and size(zz) < best_size:
            best, best_size = zz, size(zz)
    return best


def level_at(map_zones: Sequence[Zone], x, y, z) -> int:
    """CTheZones::GetLevelFromPosition: first containing map zone (THEMAP excluded), else 0."""
    for zz in map_zones:
        if zz.contains(x, y, z):
            return zz.level
    return THEMAP.level


# --- zone-name text -----------------------------------------------------------------------------
# SA 8-bit text encoding (GXT / FXT bytes).  0x20-0x7E are ASCII; 0x80.. follow the font glyph
# order of CFont::FindSubFontCharacter (gta-reversed Font.cpp), confirmed by majomix GtaGxtTool.
_SA_HIGH = "ÀÁÂÄÆÇÈÉÊËÌÍÎÏÒÓÔÖÙÚÛÜßàáâäæçèéêëìíîïòóôöùúûüÑñ¿"
SA_ENCODE = {ch: 0x80 + i for i, ch in enumerate(_SA_HIGH)}
SA_DECODE = {v: k for k, v in SA_ENCODE.items()}

# Azerbaijani letters without a glyph.  "sa" keeps ç ö ü (in the font) and writes ə as ä (the
# 1991-92 Azerbaijani Latin alphabet used Ä for Ə); "ascii" is plain English-style transliteration.
AZ_TRANSLIT = {
    "sa": {"ə": "ä", "Ə": "Ä", "ş": "sh", "Ş": "Sh", "ğ": "gh", "Ğ": "Gh", "ı": "i", "İ": "I"},
    "ascii": {"ə": "e", "Ə": "E", "ş": "sh", "Ş": "Sh", "ğ": "gh", "Ğ": "Gh", "ı": "i", "İ": "I",
              "ç": "ch", "Ç": "Ch", "ö": "o", "Ö": "O", "ü": "u", "Ü": "U"},
}
# characters that break FXT/GXT text: '#' starts an FXT comment, '~' starts a GXT token
_FORBIDDEN = set("#~\r\n\t")


def to_sa_text(text: str, style: str = "sa") -> str:
    """Transliterate (Azerbaijani) text so every char exists in the SA font."""
    table = AZ_TRANSLIT[style]
    out = []
    for ch in text:
        ch = table.get(ch, ch)
        out.append(ch)
    s = "".join(out)
    if style == "ascii":
        s = "".join(c if ord(c) < 0x80 else _ascii_fallback(c) for c in s)
    return s


def _ascii_fallback(c: str) -> str:
    import unicodedata
    d = unicodedata.normalize("NFKD", c)
    base = "".join(ch for ch in d if ord(ch) < 0x80)
    return base or "?"


def encode_sa(text: str, style: str = "sa") -> bytes:
    """Unicode -> SA GXT/FXT bytes (after :func:`to_sa_text`).  Raises for unsupported chars."""
    s = to_sa_text(text, style)
    out = bytearray()
    for ch in s:
        if ch in _FORBIDDEN:
            raise ZoneError(f"character {ch!r} not allowed in game text")
        o = ord(ch)
        if 0x20 <= o < 0x7F:
            out.append(o)
        elif ch in SA_ENCODE:
            out.append(SA_ENCODE[ch])
        else:
            raise ZoneError(f"character {ch!r} (U+{o:04X}) has no glyph in the SA font")
    return bytes(out)


def decode_sa(data: bytes) -> str:
    return "".join(SA_DECODE.get(b, chr(b)) for b in data)


def gxt_key_hash(key: str) -> int:
    """CKeyGen::GetUppercaseKey: CRC-32 (reflected 0xEDB88320, init 0xFFFFFFFF, no final xor)
    of the key with a-z upper-cased."""
    up = "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in key)
    return zlib.crc32(up.encode("latin-1")) ^ 0xFFFFFFFF


def make_label(name: str, taken: set | None = None, length: int = LABEL_LEN) -> str:
    """Derive a unique <=7 char A-Z0-9 label from a (Azerbaijani) place name."""
    base = re.sub(r"[^A-Z0-9]", "", to_sa_text(name, "ascii").upper()) or "ZONE"
    if base[0].isdigit():
        base = "Z" + base
    taken = taken if taken is not None else set()
    cand = base[:length]
    n = 1
    while cand in taken or cand in BUILTIN:
        suf = str(n)
        cand = base[:length - len(suf)] + suf
        n += 1
    taken.add(cand)
    return cand


def format_fxt(entries: Mapping[str, str], style: str = "sa", *, newline: str = "\r\n") -> bytes:
    """FXT ("fake GXT") file for CLEO 4 (``CLEO/CLEO_TEXT/*.fxt``) or modloader (``*.fxt``).

    One ``KEY text`` per line: key = up to the first whitespace, text = everything after exactly
    one separator char (no left trim), '#' starts a comment, trailing whitespace is trimmed,
    CLEO 4 reads lines into a 256-byte buffer.  Bytes are passed to the game unchanged, so the
    text is written in the SA 8-bit encoding."""
    out = bytearray()
    for key, text in entries.items():
        k = key.upper()
        if not _LABEL_RE.match(k):
            raise ZoneError(f"FXT key {key!r} must be 1-7 chars A-Z 0-9 _ to be usable as a GXT key")
        body = encode_sa(text, style).strip(b" ")
        if not body:
            raise ZoneError(f"FXT key {key!r}: empty text")
        line = k.encode("ascii") + b" " + body
        if len(line) > 254:
            raise ZoneError(f"FXT line for {key!r} longer than CLEO's 255-char buffer")
        out += line + newline.encode("ascii")
    return bytes(out)


def parse_fxt(data: bytes) -> dict:
    """Parse an FXT file the way modloader's injector::ParseFXT does."""
    out = {}
    for raw in data.split(b"\n"):
        cut = len(raw)
        for i, b in enumerate(raw):
            if b in (0x23, 0x0D, 0x0A):        # '#', CR, LF end the line
                cut = i
                break
        raw = raw[:cut]
        m = re.match(rb"([^\x09-\x0d ]+)[\x09-\x0d ](.*)$", raw, re.S)
        if not m or not m.group(2):
            continue
        out[m.group(1).decode("latin-1").upper()] = decode_sa(m.group(2).rstrip(b" \t\r\n\x0b\x0c"))
    return out


# --- minimal SA GXT (version 4, 8-bit) builder / reader ------------------------------------------
def build_gxt(tables: Mapping[str, Mapping], style: str = "sa") -> bytes:
    """Build an SA GXT.  ``tables`` maps table name -> {key (str) or CRC (int): text (str|bytes)}.
    "MAIN" is required and written first (CText::Load reads TABL, then MAIN's TKEY/TDAT in file
    order); mission tables (<=7 char names, max 200 tables) are prefixed by their 8-byte name.
    Use :func:`read_gxt` + this to add keys to the vanilla american.gxt (keys are only hashes)."""
    if "MAIN" not in tables:
        raise ZoneError("GXT needs a MAIN table")
    names = ["MAIN"] + sorted(n for n in tables if n != "MAIN")
    if len(names) > 200:
        raise ZoneError("SA supports at most 200 GXT tables")

    def block(entries):
        items = []
        for k, v in entries.items():
            h = k if isinstance(k, int) else gxt_key_hash(k)
            b = v if isinstance(v, (bytes, bytearray)) else encode_sa(v, style)
            items.append((h & 0xFFFFFFFF, bytes(b)))
        items.sort(key=lambda t: t[0])
        if len({h for h, _ in items}) != len(items):
            raise ZoneError("duplicate GXT key hash in one table")
        tdat = bytearray()
        tkey = bytearray()
        for h, b in items:
            tkey += struct.pack("<II", len(tdat), h)
            tdat += b + b"\0"
        return b"TKEY" + struct.pack("<I", len(tkey)) + tkey + b"TDAT" + struct.pack("<I", len(tdat)) + tdat

    blobs = {n: block(tables[n]) for n in names}
    head = struct.pack("<HH", 4, 8)
    tabl_size = 12 * len(names)
    offset = len(head) + 8 + tabl_size
    tabl = bytearray()
    body = bytearray()
    for n in names:
        if not re.match(r"^[A-Z0-9_]{1,7}$", n):
            raise ZoneError(f"bad GXT table name {n!r}")
        if n == "MAIN":
            tabl += n.encode().ljust(8, b"\0") + struct.pack("<I", offset)
            body += blobs[n]
            offset += len(blobs[n])
        else:
            tabl += n.encode().ljust(8, b"\0") + struct.pack("<I", offset)
            chunk = n.encode().ljust(8, b"\0") + blobs[n]
            body += chunk
            offset += len(chunk)
    return head + b"TABL" + struct.pack("<I", tabl_size) + bytes(tabl) + bytes(body)


def read_gxt(data: bytes) -> dict:
    """Read an SA GXT into {table: {crc: bytes}} (raw 8-bit strings)."""
    ver, bits = struct.unpack_from("<HH", data, 0)
    if (ver, bits) != (4, 8):
        raise ZoneError(f"not an SA 8-bit GXT (version {ver}, {bits} bits)")
    if data[4:8] != b"TABL":
        raise ZoneError("missing TABL")
    n = struct.unpack_from("<I", data, 8)[0] // 12
    out = {}
    for i in range(n):
        name = data[12 + 12 * i:20 + 12 * i].split(b"\0")[0].decode("ascii")
        off = struct.unpack_from("<I", data, 20 + 12 * i)[0]
        if name != "MAIN":
            off += 8
        if data[off:off + 4] != b"TKEY":
            raise ZoneError(f"table {name}: TKEY expected")
        ksize = struct.unpack_from("<I", data, off + 4)[0]
        keys = [struct.unpack_from("<II", data, off + 8 + 8 * j) for j in range(ksize // 8)]
        doff = off + 8 + ksize
        if data[doff:doff + 4] != b"TDAT":
            raise ZoneError(f"table {name}: TDAT expected")
        dstart = doff + 8
        table = {}
        for o, h in keys:
            end = data.index(b"\0", dstart + o)
            table[h] = data[dstart + o:end]
        out[name] = table
    return out
