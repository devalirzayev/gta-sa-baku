#!/usr/bin/env python3
"""Build the GTA San Andreas (PC, v1.0) model catalog for the Baku total conversion.

Run (from anywhere; stdlib only):

    python3 -I tools/catalog/build_catalog.py [--cache DIR] [--offline] [--no-prineside]

All downloads are cached under --cache (default: the session scratchpad
``catalog_cache`` directory, see CACHE_DEFAULT). A second run with a warm cache
does no network traffic. Every downloaded file is treated as *data*.

Outputs (repository relative):
    data/sa/gta_dat_vanilla.txt     vanilla gta.dat (verified against >=3 copies)
    data/sa/models.csv              one row per model id defined by the vanilla
                                    IDE files (gta.dat + default.dat)
    data/sa/ide_summary.csv         per gta.dat IDE file: model-info slot usage, ...
    data/sa/building_candidates.csv subset usable as Baku buildings (+ style,
                                    ground_risk)

Sources (pinned commits where possible; see docs/notes/catalog.md):
    * gta.dat / default.dat: 7 independent public copies (git blob hashes checked)
    * IDE files: gtastories/gta (primary), Sir-Teo/web-gta, gta191977649/sa_object_finder
    * text IPLs: gtastories/gta (primary), Sir-Teo/web-gta, Fernando-A-Rocha/mta-binary-ipl-to-text
    * binary (stream) IPLs converted to text: Fernando-A-Rocha/mta-binary-ipl-to-text
      (primary), gta191977649/sa_object_finder data/all (cross-check)
    * collision bounds: Crayder/Model-Sizes-Plus modelsizes.inc (ColAndreas export)
    * model names: gta-reversed eModelID.h (fastman92 enum from unmodified SA data)
    * hardcoded model names looked up by the exe: gta-reversed ModelIndices.cpp
    * categories / titles: dev.prineside.com category listing pages
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
CACHE_DEFAULT = ("/tmp/claude-0/-home-user-ila/5b6b71aa-4af7-540b-9036-4dec5b516689/"
                 "scratchpad/catalog_cache")
REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "data" / "sa"

USER_AGENT = "ila-baku-sa-catalog/1.0 (research; cached, <=2 req/s)"

# --------------------------------------------------------------------------------------
# Sources (pinned)
# --------------------------------------------------------------------------------------
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"

# repo key -> (owner/name, commit)
REPOS = {
    "gtastories": ("gtastories/gta", "ecfea46726a4ca84ea45785697064d5f98c76212"),
    "webgta": ("Sir-Teo/web-gta", "77ce6581cf943a4332e286e18b60dfd568d4d330"),
    "sof": ("gta191977649/sa_object_finder", "b7831cac689ca26760d510dc2dcd4abb6db6ad9e"),
    "fern": ("Fernando-A-Rocha/mta-binary-ipl-to-text", "a234eb28c46b05a5844144f1955d79911b3d4e8e"),
    "alf21": ("Alf21/samp", "8c174cd4134effd255c7871ce06772a3ed12b294"),
    "google61": ("Google61/gta3sa.app", "67f70cb72eb207aeca650ea9e8c10b0e7dda166a"),
    "cybersys": ("CyberSys/SA-MP", "318bcd6371a3de6b51b4ce06b287797a12b97bfb"),
    "justarch": ("Just-Archives/SA-MP", "b7bc40179f8cac462ea290167715b23e35cca996"),
    "xdatsec": ("xdatsec/GTA_2.11.311", "c7266cf5f8f1e3216fa51b289d238baf7d4ded49"),
    "dashr": ("dashr9230/SA-MP", "44395b4a615356015573fac7ac17908646378caf"),
    # big files are not in the GitHub code-search index -> no commit known; branch +
    # content sha256 check (warning only) instead.
    "msp": ("crayder/model-sizes-plus", "master"),
    "gtarev": ("gta-reversed/gta-reversed", "master"),
    # commit seen by code search (used by --scan-gtarev for MODEL_* usage)
    "gtarev_pinned": ("gta-reversed/gta-reversed", "afbc26d70675b9138b740b857ffdcaeaa7c5ca7c"),
}

# (repo key, path, expected git blob sha1 or None). The gta.dat copies listed first are
# expected to be vanilla; dashr9230/SA-MP is a *control* (SA-MP modified gta.dat).
GTA_DAT_COPIES = [
    ("gtastories", "data/gta.dat", "566bb9deb0799842c7128f148487e0c51d306538"),
    ("webgta", "public/play/world/files/gta.dat", "566bb9deb0799842c7128f148487e0c51d306538"),
    ("alf21", "archive/files/gta.dat", "9c80612e979c618aa0d18237204383b4e644d84b"),
    ("google61", "gta.dat", "9c80612e979c618aa0d18237204383b4e644d84b"),
    ("cybersys", "archive/files/gta.dat", "9c80612e979c618aa0d18237204383b4e644d84b"),
    ("justarch", "archive/files/gta.dat", "9c80612e979c618aa0d18237204383b4e644d84b"),
    ("xdatsec", "app/src/main/assets/data/gta.dat", "9c80612e979c618aa0d18237204383b4e644d84b"),
    ("dashr", "archive/files/gta.dat", "ea7341111b7a8728dfe237227e7b25617937e323"),
]
DEFAULT_DAT_COPIES = [
    ("gtastories", "data/default.dat", "e6e29d6ffc50e253bc93f7faeebfc5ce7e4dcbb4"),
    ("alf21", "archive/files/default.dat", "0e0440286ea759abd8aa35533c046a64534e8142"),
    ("google61", "default.dat", "0e0440286ea759abd8aa35533c046a64534e8142"),
    ("cybersys", "archive/files/default.dat", "0e0440286ea759abd8aa35533c046a64534e8142"),
    ("xdatsec", "app/src/main/assets/data/default.dat", "0e0440286ea759abd8aa35533c046a64534e8142"),
]

# Actual file paths in the IDE source repos (from GitHub code search at the pinned
# commits). Matching against gta.dat entries is case-insensitive on the basename.
_GTASTORIES_IDE = """
data/default.ide data/peds.ide data/vehicles.ide data/txdcut.ide data/maps/txd.ide
data/maps/LA/LAe.ide data/maps/LA/LAe2.ide data/maps/LA/LAhills.ide data/maps/LA/LAn.ide
data/maps/LA/LAn2.ide data/maps/LA/LAs.ide data/maps/LA/LAs2.ide data/maps/LA/LAw.ide
data/maps/LA/LAw2.ide data/maps/LA/LAxref.ide data/maps/LA/LaWn.ide data/maps/SF/SFSe.ide
data/maps/SF/SFe.ide data/maps/SF/SFn.ide data/maps/SF/SFs.ide data/maps/SF/SFw.ide
data/maps/SF/SFxref.ide data/maps/country/countn2.ide data/maps/country/countryN.ide
data/maps/country/countryS.ide data/maps/country/countryW.ide data/maps/country/countrye.ide
data/maps/country/counxref.ide data/maps/generic/barriers.ide data/maps/generic/dynamic.ide
data/maps/generic/dynamic2.ide data/maps/generic/multiobj.ide data/maps/generic/procobj.ide
data/maps/generic/vegepart.ide data/maps/interior/gen_int1.ide data/maps/interior/gen_int2.ide
data/maps/interior/gen_int3.ide data/maps/interior/gen_int4.ide data/maps/interior/gen_int5.ide
data/maps/interior/gen_intb.ide data/maps/interior/int_LA.ide data/maps/interior/int_SF.ide
data/maps/interior/int_cont.ide data/maps/interior/int_veg.ide data/maps/interior/propext.ide
data/maps/interior/props.ide data/maps/interior/props2.ide data/maps/interior/savehous.ide
data/maps/interior/stadint.ide data/maps/leveldes/leveldes.ide data/maps/leveldes/levelmap.ide
data/maps/leveldes/levelxre.ide data/maps/leveldes/seabed.ide data/maps/vegas/VegasN.ide
data/maps/vegas/VegasS.ide data/maps/vegas/VegasW.ide data/maps/vegas/vegasE.ide
data/maps/vegas/vegaxref.ide data/maps/veh_mods/veh_mods.ide
""".split()
_WEBGTA_IDE = ["public/play/world/files/data/default.ide",
               "public/play/world/files/data/peds.ide",
               "public/play/world/files/vehicles/vehicles.ide"] + [
    "public/play/world/files/ide/" + p.split("data/maps/", 1)[1]
    for p in _GTASTORIES_IDE if p.startswith("data/maps/")]
_SOF_IDE = ["data/all_ide/" + p.rsplit("/", 1)[1] for p in _GTASTORIES_IDE
            if p.startswith("data/maps/")]

_TEXT_IPL_REL = """
LA/LAe.ipl LA/LAe2.ipl LA/LAhills.ipl LA/LAn.ipl LA/LAn2.ipl LA/LAs.ipl LA/LAs2.ipl LA/LAw.ipl
LA/LAw2.ipl LA/LaWn.ipl SF/SFSe.ipl SF/SFe.ipl SF/SFn.ipl SF/SFs.ipl SF/SFw.ipl
country/countn2.ipl country/countryN.ipl country/countryS.ipl country/countrye.ipl
country/countryw.ipl interior/gen_int1.ipl interior/gen_int2.ipl interior/gen_int3.ipl
interior/gen_int4.ipl interior/gen_int5.ipl interior/gen_intb.ipl interior/int_LA.ipl
interior/int_SF.ipl interior/int_cont.ipl interior/int_veg.ipl interior/savehous.ipl
interior/stadint.ipl leveldes/leveldes.ipl leveldes/levelmap.ipl leveldes/seabed.ipl
vegas/vegasE.ipl vegas/vegasN.ipl vegas/vegasS.ipl vegas/vegasW.ipl vegas/vegaxref.ipl
Audiozon.ipl cull.ipl occlu.ipl occluLA.ipl occluint.ipl occlusf.ipl occluveg.ipl paths4.ipl
tunnels.ipl
""".split()
TEXT_IPL_SOURCES = {
    "gtastories": ["data/maps/" + p for p in _TEXT_IPL_REL],
    "webgta": ["public/play/world/files/ipl/" + p for p in _TEXT_IPL_REL],
    "fern": ["other_stuff/normal_ipls/" + p for p in _TEXT_IPL_REL],
}

# 190 binary IPLs inside gta3.img (= data/sa/sa_ipl_slots.txt minus "generic")
BINARY_IPL_NAMES = None  # filled from data/sa/sa_ipl_slots.txt at run time
STREAM_IPL_SOURCES = {
    "fern": "output/{name}.ipl",
    "sof": "data/all/{name}.ipl",
}

# data files that reference models by name: name -> [(repo key, path), ...]
DATA_FILES = {
    "procobj.dat": [("gtastories", "data/procobj.dat"), ("webgta", "public/play/world/files/procobj.dat")],
    "object.dat": [("gtastories", "data/object.dat"), ("webgta", "public/play/world/files/object.dat")],
    "carmods.dat": [("gtastories", "data/carmods.dat"),
                    ("webgta", "public/play/world/files/vehicles/carmods.dat")],
}

MODELSIZES = ("msp", "modelsizes.inc",
              "ca7a03d5d34e1658b57a5840974145f28e8dd0af52d6f518272d9a8b4b4bec71")
EMODELID = ("gtarev", "source/game_sa/Enums/eModelID.h",
            "6f5398454b1f5507f0086e9bed7462f24837af58077379df5536cd00a0ad38b5")
MODELINDICES = ("gtarev", "source/game_sa/ModelIndices.cpp", None)

PRINESIDE_BASE = "https://dev.prineside.com/en/gtasa_samp_model_id/category/{cat}/"
# category -> parent (from the site's category tree); scraped in this order
PRINESIDE_CATEGORIES = [
    ("buildings", None),
    ("houses-and-apartments", "buildings"),
    ("offices-and-skyscrapers", "buildings"),
    ("factories-and-warehouses", "buildings"),
    ("stores-and-shops", "buildings"),
    ("restaurants-and-hotels", "buildings"),
    ("other-buildings", "buildings"),
    ("sports-and-stadium-objects", "buildings"),
    ("bars-clubs-and-casinos", "buildings"),
    ("graveyard", "buildings"),
    ("industrial", None),
    ("general-industrial", "industrial"),
    ("construction-industrial", "industrial"),
    ("cranes", "industrial"),
    ("crates-drums-and-racks", "industrial"),
    ("structures", None),
    ("garages-and-petrol-stations", "structures"),
    ("car-parks", "structures"),
    ("fences-walls-gates-and-barriers", "structures"),
    ("signs-billboards-and-statues", "structures"),
    ("airport-and-aircraft-objects", "structures"),
    ("farm-objects", "structures"),
    ("ramps", "structures"),
    ("nature", None),
    ("trees", "nature"),
    ("plants", "nature"),
    ("rocks", "nature"),
    ("miscellaneous", None),
    ("lights", "miscellaneous"),
    ("street-and-road-items", "miscellaneous"),
    ("trash", "miscellaneous"),
    ("ladders-stairs-and-scaffolding", "miscellaneous"),
    ("land-masses", None),
    ("concrete-and-rock", "land-masses"),
    ("grass-and-dirt", "land-masses"),
    ("transportation", None),
    ("roads-bridges-and-tunnels", "transportation"),
    ("railroads", "transportation"),
    ("beach-and-sea", None),
    ("interior-objects", None),
    ("more-interiors", "interior-objects"),
    ("wires-and-cables", None),
    ("alpha-channels-and-non-collidable", None),
    ("nighttime-objects", None),
]


# --------------------------------------------------------------------------------------
# Cached, rate-limited fetcher
# --------------------------------------------------------------------------------------
class Fetcher:
    MIN_INTERVAL = {"dev.prineside.com": 0.6}  # seconds between requests (<= 2 req/s)
    DEFAULT_INTERVAL = 0.05

    def __init__(self, cache_dir: Path, offline: bool = False, verbose: bool = True):
        self.cache_dir = Path(cache_dir)
        self.offline = offline
        self.verbose = verbose
        self._last = {}
        self.n_network = 0

    def cache_path(self, url: str) -> Path:
        u = urllib.parse.urlsplit(url)
        parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
        safe = [re.sub(r"[^A-Za-z0-9._ +()'-]", "_", p) for p in parts] or ["index"]
        if u.path.endswith("/"):
            safe.append("index.html")
        if u.query:
            safe[-1] += "__" + re.sub(r"[^A-Za-z0-9._=-]", "_", u.query)
        return self.cache_dir / "http" / u.netloc / Path(*safe)

    def get(self, url: str, required: bool = True) -> bytes | None:
        path = self.cache_path(url)
        miss = path.with_name(path.name + ".404")
        if path.exists():
            return path.read_bytes()
        if miss.exists():
            if required:
                raise FileNotFoundError(url)
            return None
        if self.offline:
            raise RuntimeError(f"offline mode and not cached: {url}")
        host = urllib.parse.urlsplit(url).netloc
        interval = self.MIN_INTERVAL.get(host, self.DEFAULT_INTERVAL)
        for attempt in range(5):
            wait = self._last.get(host, 0) + interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.time()
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read()
                self.n_network += 1
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    miss.parent.mkdir(parents=True, exist_ok=True)
                    miss.write_text(url)
                    if required:
                        raise FileNotFoundError(url) from e
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt < 4:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        if self.verbose and self.n_network % 50 == 1:
            print(f"  [net] {self.n_network} downloads so far (last: {url})", file=sys.stderr)
        return data

    def github(self, repo_key: str, path: str, required: bool = True) -> bytes | None:
        repo, ref = REPOS[repo_key]
        url = RAW.format(repo=repo, ref=ref, path=urllib.parse.quote(path))
        return self.get(url, required=required)


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def norm_text(data: bytes) -> str:
    """Decode + normalise for comparisons: BOM, CRLF, trailing blanks."""
    t = data.decode("latin-1")
    if t.startswith("\xef\xbb\xbf"):
        t = t[3:]
    lines = [ln.rstrip() for ln in t.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# Engine-faithful text parsing (CFileLoader::LoadLine: chars < 0x20 and ',' -> ' ',
# leading blanks skipped, '#' is a comment only at the start of a line, sections are
# recognised by prefix ("objs", ..., "end").)
# --------------------------------------------------------------------------------------
def engine_lines(text: str):
    tr = {i: " " for i in range(32)}
    tr[ord(",")] = " "
    for no, raw in enumerate(text.split("\n"), 1):
        s = raw.translate(tr).lstrip(" ")
        if not s or s.startswith("#"):
            continue
        yield no, s


_INT_RE = re.compile(r"[+-]?\d+")
_FLT_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def scan(tokens: list[str], fmt: str) -> list:
    """Minimal sscanf emulation over whitespace separated tokens ('d', 'f', 's')."""
    vals = []
    for tok, f in zip(tokens, fmt):
        if f == "s":
            vals.append(tok)
            continue
        m = (_INT_RE if f == "d" else _FLT_RE).match(tok)
        if not m:
            break
        vals.append(int(m.group()) if f == "d" else float(m.group()))
        if m.end() != len(tok):  # sscanf would continue inside the token; never in vanilla
            break
    return vals


def parse_dat(text: str) -> list[tuple[str, str]]:
    out = []
    for _, s in engine_lines(text):
        parts = s.split(None, 1)
        out.append((parts[0].upper(), parts[1].strip() if len(parts) > 1 else ""))
    return out


IDE_SECTIONS = ("objs", "tobj", "weap", "hier", "anim", "cars", "peds", "path", "2dfx", "txdp")

# model-info store each IDE section allocates from (CModelInfo::Add*Model; gta-reversed)
STORE_OF_SECTION = {"objs": "atomic", "tobj": "time", "anim": "clump", "hier": "clump",
                    "weap": "weapon", "cars": "vehicle", "peds": "ped"}
STORE_CAPACITY = {"atomic": 14000, "damage_atomic": 70, "time": 169, "clump": 92,
                  "weapon": 51, "vehicle": 212, "ped": 278}
FLAG_IS_DAMAGEABLE = 0x1000  # objs entries with this flag use CDamageAtomicModelInfo

IDE_FLAG_NAMES = [  # SA IDE flags (gtamods wiki "Item Definition#IDE Flags")
    (0x1, "IS_ROAD"), (0x4, "DRAW_LAST"), (0x8, "ADDITIVE"), (0x20, "ANIM_FLAG_0x20"),
    (0x40, "NO_ZBUFFER_WRITE"), (0x80, "DONT_RECEIVE_SHADOWS"), (0x200, "IS_GLASS_TYPE_1"),
    (0x400, "IS_GLASS_TYPE_2"), (0x800, "IS_GARAGE_DOOR"), (0x1000, "IS_DAMAGABLE"),
    (0x2000, "IS_TREE"), (0x4000, "IS_PALM"), (0x8000, "DOES_NOT_COLLIDE_WITH_FLYER"),
    (0x100000, "IS_TAG"), (0x200000, "DISABLE_BACKFACE_CULLING"),
    (0x400000, "IS_BREAKABLE_STATUE"),
]


def flag_names(flags: int | None) -> str:
    if flags is None:
        return ""
    names = [n for b, n in IDE_FLAG_NAMES if flags & b]
    rest = flags & ~sum(b for b, _ in IDE_FLAG_NAMES)
    if rest:
        names.append(hex(rest))
    return "|".join(names)


def _parse_objs(tok):
    v0 = scan(tok, "dssfd")
    if len(v0) == 5 and v0[3] >= 4.0:
        return dict(id=v0[0], name=v0[1], txd=v0[2], draw_distance=v0[3], flags=v0[4],
                    mesh_count=None)
    v = scan(tok, "dssd")
    if len(v) != 4 or v[3] not in (1, 2, 3):
        return None
    n = v[3]
    v = scan(tok, "dssd" + "f" * n + "d")
    if n == 1 and len(v) == 5 and len(v0) == 5:
        # CFileLoader::LoadObject only VERIFYs ">= 5" for mesh count 1, so a short line
        # like "1489 DYN_SALE_POST BREAK_STREET2 1 0" is accepted: draw distance = 0 and
        # the flags keep the value read by the first sscanf (here 0).
        return dict(id=v[0], name=v[1], txd=v[2], draw_distance=v[4], flags=v0[4],
                    mesh_count=n, malformed=True)
    if len(v) != 5 + n:
        return None
    return dict(id=v[0], name=v[1], txd=v[2], draw_distance=v[4], flags=v[4 + n], mesh_count=n)


def _parse_tobj(tok):
    v = scan(tok, "dssfddd")
    if len(v) == 7 and v[3] >= 4.0:
        return dict(id=v[0], name=v[1], txd=v[2], draw_distance=v[3], flags=v[4],
                    time_on=v[5], time_off=v[6], mesh_count=None)
    v = scan(tok, "dssd")
    if len(v) != 4 or v[3] not in (1, 2, 3):
        return None
    n = v[3]
    v = scan(tok, "dssd" + "f" * n + "ddd")
    if len(v) != 7 + n:
        return None
    return dict(id=v[0], name=v[1], txd=v[2], draw_distance=v[4], flags=v[4 + n],
                time_on=v[5 + n], time_off=v[6 + n], mesh_count=n)


def _parse_anim(tok):
    v = scan(tok, "dsssfd")
    if len(v) != 6:
        return None
    return dict(id=v[0], name=v[1], txd=v[2], anim=v[3], draw_distance=v[4], flags=v[5])


def _parse_weap(tok):
    v = scan(tok, "dsssdf")
    if len(v) != 6:
        return None
    return dict(id=v[0], name=v[1], txd=v[2], anim=v[3], draw_distance=v[5], flags=None)


def _parse_simple(tok):  # hier / cars / peds: id, model, txd, ...
    v = scan(tok, "dss")
    if len(v) != 3:
        return None
    return dict(id=v[0], name=v[1], txd=v[2], draw_distance=None, flags=None)


_DEF_PARSERS = {"objs": _parse_objs, "tobj": _parse_tobj, "anim": _parse_anim,
                "weap": _parse_weap, "hier": _parse_simple, "cars": _parse_simple,
                "peds": _parse_simple}


def parse_ide(text: str) -> dict:
    res = {"defs": [], "txdp": [], "counts": collections.Counter(), "anomalies": []}
    sec = None
    for no, s in engine_lines(text):
        if sec is None:
            sec = next((n for n in IDE_SECTIONS if s.startswith(n)), None)
            continue
        if s.startswith("end"):
            sec = None
            continue
        tok = s.split()
        res["counts"][sec] += 1
        if sec in _DEF_PARSERS:
            d = _DEF_PARSERS[sec](tok)
            if d is None:
                res["anomalies"].append((no, s))
                continue
            d["section"] = sec
            d["line"] = no
            res["defs"].append(d)
        elif sec == "txdp":
            v = scan(tok, "ss")
            if len(v) == 2:
                res["txdp"].append((v[0], v[1]))
            else:
                res["anomalies"].append((no, s))
    return res


def parse_ipl_insts(text: str) -> tuple[list[tuple], collections.Counter]:
    """Return inst tuples (id, name, interior, x, y, z, qx, qy, qz, qw, lod) + other
    section line counts. Works for text IPLs and for the converted binary IPLs."""
    insts, other = [], collections.Counter()
    sec = None
    for no, s in engine_lines(text):
        if sec is None:
            sec = s[:4].lower()
            continue
        if s.startswith("end"):
            sec = None
            continue
        if sec == "inst":
            tok = s.split()
            v = scan(tok, "dsdfffffffd")
            if len(v) != 11:
                raise ValueError(f"bad inst line {no}: {s!r}")
            insts.append(tuple(v))
        else:
            other[sec] += 1
    return insts, other


def parse_emodelid(text: str) -> tuple[dict, dict]:
    name2id, id2names = {}, collections.defaultdict(list)
    for m in re.finditer(r"^\s*(MODEL_[A-Za-z0-9_]+)\s*=\s*(-?\d+)\s*,", text, re.M):
        name2id[m.group(1)] = int(m.group(2))
        id2names[int(m.group(2))].append(m.group(1))
    return name2id, id2names


def emodelid_name(model_name: str) -> str:
    return "MODEL_" + re.sub(r"[^A-Za-z0-9]", "_", model_name).upper()


def parse_modelsizes(text: str) -> dict[int, dict]:
    """Crayder's modelsizes.inc -> {id: dict(radius, cx, cy, cz, min(3), max(3))}."""
    def block(name):
        i = text.index(name)
        j = text.index("{", i)
        depth, k = 0, j
        while True:
            c = text[k]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        return [float(x) for x in _FLT_RE.findall(text[j:k])]

    rad = block("Float:MODELS_gColRadius[20000]")
    off = block("Float:MODELS_gColOffset[20000][3]")
    box = block("Float:MODELS_gColAABB[20000][2][3]")
    if not (len(rad) == 20000 and len(off) == 60000 and len(box) == 120000):
        raise ValueError(f"unexpected modelsizes array sizes {len(rad)} {len(off)} {len(box)}")
    out = {}
    for i in range(20000):
        b = box[6 * i:6 * i + 6]
        out[i] = dict(radius=rad[i], cx=off[3 * i], cy=off[3 * i + 1], cz=off[3 * i + 2],
                      min=b[:3], max=b[3:])
    return out


def parse_model_indices(text: str) -> list[str]:
    """Names in MatchAllModelStrings() (exe looks these up by name; missing -> -1)."""
    i = text.index("MatchAllModelStrings")
    j = text.index("};", i)
    return re.findall(r'\{\s*"([^"]+)"\s*,', text[i:j])


def parse_prineside_listing(page: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<a href="/en/gtasa_samp_model_id/model/[^"]*"\s+class="mp-item"(.*?)>',
                         page, re.S):
        attrs = dict(re.findall(r'(data-[a-z]+)="([^"]*)"', m.group(1)))
        if "data-model" not in attrs:
            continue
        out.append(dict(id=int(attrs["data-model"]), name=html.unescape(attrs.get("data-name", "")),
                        title=html.unescape(attrs.get("data-title", "")),
                        radius=attrs.get("data-radius"), width=attrs.get("data-width"),
                        length=attrs.get("data-length"), height=attrs.get("data-height"),
                        samp=attrs.get("data-samp")))
    return out


def prineside_last_page(page: str) -> int:
    nums = [int(x) for x in re.findall(r"\?page=(\d+)", page)]
    return max(nums) if nums else 1


# --------------------------------------------------------------------------------------
# Source loading + cross verification
# --------------------------------------------------------------------------------------
class Report:
    """Collects verification facts (printed and returned for the notes)."""

    def __init__(self):
        self.lines = []

    def __call__(self, msg):
        self.lines.append(msg)
        print(msg, file=sys.stderr)


def _basename(p: str) -> str:
    return p.replace("\\", "/").rsplit("/", 1)[-1].lower()


def load_vanilla_dat(fetch: Fetcher, copies, label, rep: Report) -> tuple[bytes, str]:
    """Download all copies, check git blob hashes, group by normalised content and
    return (raw bytes of a CRLF copy of the majority content, normalised text)."""
    groups = collections.defaultdict(list)
    raw_by_key = {}
    for key, path, blob in copies:
        data = fetch.github(key, path)
        got = git_blob_sha1(data)
        if blob and got != blob:
            rep(f"WARNING {label}: {REPOS[key][0]}:{path} blob {got} != expected {blob}")
        n = norm_text(data)
        groups[n].append(key)
        raw_by_key[key] = data
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    best, members = ranked[0]
    if len(members) < 3:
        raise RuntimeError(f"{label}: no content shared by >= 3 copies")
    rep(f"{label}: {len(members)} of {len(copies)} copies identical after CRLF/whitespace "
        f"normalisation ({', '.join(REPOS[k][0] for k in members)}); md5(normalised LF)="
        f"{hashlib.md5(best.encode('latin-1')).hexdigest()}")
    for other, mem in ranked[1:]:
        rep(f"  {label}: differing copy/copies: {', '.join(REPOS[k][0] for k in mem)}")
    crlf = next((raw_by_key[k] for k in members if b"\r\n" in raw_by_key[k]), None)
    if crlf is None:
        crlf = best.replace("\n", "\r\n").encode("latin-1")
    return crlf, best


def ide_source_paths(gta_path: str) -> list[tuple[str, str]]:
    """gta.dat / default.dat IDE path -> [(repo key, repo path), ...] (primary first)."""
    b = _basename(gta_path)
    out = []
    for key, lst in (("gtastories", _GTASTORIES_IDE), ("webgta", _WEBGTA_IDE), ("sof", _SOF_IDE)):
        for p in lst:
            if _basename(p) == b:
                out.append((key, p))
                break
    return out


def load_ides(fetch: Fetcher, ide_paths: list[str], rep: Report) -> dict[str, str]:
    texts, n_multi = {}, 0
    for gp in ide_paths:
        srcs = ide_source_paths(gp)
        if not srcs:
            raise RuntimeError(f"no source for {gp}")
        versions = {k: norm_text(fetch.github(k, p)) for k, p in srcs}
        primary = versions[srcs[0][0]]
        diff = [k for k, v in versions.items() if v != primary]
        if diff:
            rep(f"WARNING IDE {gp}: copies differ from primary: {diff}")
        if len(versions) >= 2 and not diff:
            n_multi += 1
        texts[gp] = primary
    single = [gp for gp in ide_paths if len(ide_source_paths(gp)) < 2]
    rep(f"IDE files: {len(ide_paths)} loaded; {n_multi} identical in >=2 independent copies "
        f"(53 in 3 copies); single-source: {', '.join(single) or '-'}")
    return texts


def load_text_ipls(fetch: Fetcher, rep: Report) -> dict[str, str]:
    """basename(lower) -> normalised text, verified across 3 repos."""
    out, ndiff = {}, 0
    for i, rel in enumerate(_TEXT_IPL_REL):
        versions = {k: norm_text(fetch.github(k, lst[i])) for k, lst in TEXT_IPL_SOURCES.items()}
        primary = versions["gtastories"]
        diff = [k for k, v in versions.items() if v != primary]
        if diff:
            ndiff += 1
            rep(f"WARNING text IPL {rel}: differs in {diff}")
        out[_basename(rel)] = primary
    rep(f"text IPLs: {len(_TEXT_IPL_REL)} files, identical in all 3 copies: "
        f"{len(_TEXT_IPL_REL) - ndiff}")
    return out


def load_binary_ipls(fetch: Fetcher, rep: Report) -> dict[str, list[tuple]]:
    slots = (REPO_ROOT / "data" / "sa" / "sa_ipl_slots.txt").read_text().split("\n")
    names = [ln.split("=")[0].strip() for ln in slots if "=" in ln]
    names = [n for n in names if n != "generic"]
    out, n_ok = {}, 0
    for nm in names:
        a, _ = parse_ipl_insts(norm_text(fetch.github("fern", STREAM_IPL_SOURCES["fern"].format(name=nm))))
        b, _ = parse_ipl_insts(norm_text(fetch.github("sof", STREAM_IPL_SOURCES["sof"].format(name=nm))))
        ok = len(a) == len(b) and all(
            x[0] == y[0] and x[2] == y[2] and x[10] == y[10]
            and max(abs(p - q) for p, q in zip(x[3:10], y[3:10])) < 0.011
            for x, y in zip(a, b))
        if ok:
            n_ok += 1
        else:
            rep(f"WARNING binary IPL {nm}: the two conversions disagree")
        out[nm] = a
    rep(f"binary IPLs (gta3.img, converted to text): {len(names)}; both conversions agree "
        f"(id, flags, LOD index, position/rotation within 0.01) for {n_ok}; "
        f"{sum(len(v) for v in out.values())} instances")
    return out


# --------------------------------------------------------------------------------------
# Classification helpers
# --------------------------------------------------------------------------------------
def ide_region(gta_path: str) -> str:
    p = gta_path.replace("\\", "/").lower()
    b = _basename(p)
    if "/maps/la/" in p:
        return "LA"
    if "/maps/sf/" in p:
        return "SF"
    if "/maps/vegas/" in p:
        return "LV"
    if "/maps/country/" in p:
        return "country"
    if "/maps/interior/" in p:
        # propext.ide holds exterior props (generators, crates, aircon; ~74% of its vanilla
        # placements are outside); props/props2 are furniture placed only inside interiors
        return "generic" if b == "propext.ide" else "interior"
    if "/maps/leveldes/" in p:
        return {"levelmap.ide": "interior", "seabed.ide": "country"}.get(b, "generic")
    if "/maps/veh_mods/" in p:
        return "vehicle_upgrade"
    if b == "vehicles.ide":
        return "vehicle"
    if b == "peds.ide":
        return "ped"
    return "generic"  # generic/*, default.ide, txdcut.ide


def ipl_group(ipl_name: str) -> str:
    n = ipl_name.lower()
    if n.startswith(("int_", "gen_int", "savehous", "stadint", "levelmap")):
        return "interior"
    if n.startswith(("lae", "lan", "las", "law", "lahills")):
        return "LA"
    if n.startswith("sf"):
        return "SF"
    if n.startswith("vegas"):
        return "LV"
    if n.startswith(("count", "seabed", "truthsfarm")):
        return "country"
    if n.startswith(("barriers", "carter", "crack")):
        return "scripted"
    return "other"


# --------------------------------------------------------------------------------------
# Assemble the model table
# --------------------------------------------------------------------------------------
def build_models(fetch: Fetcher, rep: Report, scan_gtarev: bool = False):
    gta_raw, gta_txt = load_vanilla_dat(fetch, GTA_DAT_COPIES, "gta.dat", rep)
    _, def_txt = load_vanilla_dat(fetch, DEFAULT_DAT_COPIES, "default.dat", rep)
    gta_entries = parse_dat(gta_txt)
    def_entries = parse_dat(def_txt)
    gta_ides = [a for k, a in gta_entries if k == "IDE"]
    def_ides = [a for k, a in def_entries if k == "IDE"]
    gta_ipls = [a for k, a in gta_entries if k == "IPL"]
    all_ides = def_ides + gta_ides  # engine order: default.dat first, then gta.dat
    ide_texts = load_ides(fetch, all_ides, rep)

    # ---- IDE definitions -------------------------------------------------------------
    ide_info = collections.OrderedDict()
    models = {}  # id -> row dict
    txd_parent = {}
    duplicates = []
    for order, gp in enumerate(all_ides):
        r = parse_ide(ide_texts[gp])
        st = collections.Counter()
        for d in r["defs"]:
            store = STORE_OF_SECTION[d["section"]]
            if store == "atomic" and (d["flags"] or 0) & FLAG_IS_DAMAGEABLE:
                store = "damage_atomic"
            st[store] += 1
            if d["id"] in models:
                duplicates.append((d["id"], models[d["id"]]["ide_file"], gp))
            models[d["id"]] = dict(
                id=d["id"], name=d["name"], type=store, ide_file=gp, section=d["section"],
                txd=d["txd"], draw_distance=d.get("draw_distance"), flags=d.get("flags"),
                time_on=d.get("time_on"), time_off=d.get("time_off"), anim=d.get("anim"),
                mesh_count=d.get("mesh_count"), region=ide_region(gp),
                ide_malformed_line=bool(d.get("malformed")))
        for a, b in r["txdp"]:
            txd_parent[a.lower()] = b
        ids = [d["id"] for d in r["defs"]]
        ide_info[gp] = dict(order=order, loaded_by="default.dat" if gp in def_ides else "gta.dat",
                            counts=r["counts"], stores=st, n_txdp=len(r["txdp"]),
                            id_min=min(ids) if ids else None, id_max=max(ids) if ids else None,
                            anomalies=r["anomalies"], region=ide_region(gp))
        for no, s in r["anomalies"]:
            rep(f"WARNING {gp}:{no}: unparsed line {s!r}")
    if duplicates:
        rep(f"WARNING duplicate model ids across IDEs: {duplicates[:10]}")
    tot = collections.Counter()
    for inf in ide_info.values():
        tot.update(inf["stores"])
    rep("model-info store usage (vanilla, default.dat+gta.dat): " + ", ".join(
        f"{k} {tot[k]}/{STORE_CAPACITY[k]}" for k in STORE_CAPACITY))
    for m in models.values():
        m["txd_parent"] = txd_parent.get(m["txd"].lower(), "")

    # ---- eModelID cross check --------------------------------------------------------
    em = fetch.github(*EMODELID[:2])
    if sha256(em) != EMODELID[2]:
        rep("note: eModelID.h content changed upstream (sha256 differs from the pinned value)")
    name2id, id2names = parse_emodelid(em.decode("utf-8", "replace"))
    ok = sum(1 for m in models.values() if name2id.get(emodelid_name(m["name"])) == m["id"])
    only_enum = sorted(i for i in id2names if i >= 0 and i not in models and i not in (2, 3)
                       and not any(n in ("MODEL_VEHICLE_FIRST",) for n in id2names[i]))
    rep(f"eModelID.h: {len(name2id)} names; {ok}/{len(models)} IDE (id, name) pairs match "
        f"MODEL_<NAME> exactly; enum ids without IDE definition: {only_enum[:20]}"
        f"{' ...' if len(only_enum) > 20 else ''}")

    # ---- collision bounds ------------------------------------------------------------
    ms = fetch.github(*MODELSIZES[:2])
    if sha256(ms) != MODELSIZES[2]:
        rep("note: modelsizes.inc content changed upstream (sha256 differs from the pinned value)")
    sizes = parse_modelsizes(ms.decode("latin-1"))
    for m in models.values():
        s = sizes.get(m["id"])
        has = bool(s) and (s["radius"] > 0 or any(abs(v) > 0 for v in s["min"] + s["max"]))
        m["has_col_bounds"] = has
        if has:
            m.update(aabb_min_x=s["min"][0], aabb_min_y=s["min"][1], aabb_min_z=s["min"][2],
                     aabb_max_x=s["max"][0], aabb_max_y=s["max"][1], aabb_max_z=s["max"][2],
                     sphere_radius=s["radius"], sphere_x=s["cx"], sphere_y=s["cy"], sphere_z=s["cz"])
            m["footprint_x"] = s["max"][0] - s["min"][0]
            m["footprint_y"] = s["max"][1] - s["min"][1]
            m["height"] = s["max"][2] - s["min"][2]
            m["z_min"] = s["min"][2]
    n_obj = [m for m in models.values() if m["type"] in ("atomic", "damage_atomic", "time", "clump")]
    rep(f"modelsizes.inc: collision bounds present for "
        f"{sum(m['has_col_bounds'] for m in n_obj)}/{len(n_obj)} map/object models")

    # ---- placements ------------------------------------------------------------------
    text_ipls = load_text_ipls(fetch, rep)
    bin_ipls = load_binary_ipls(fetch, rep)
    text_insts = {}
    loaded_text = []  # basenames (no ext) of text IPLs loaded by gta.dat with inst data
    for gp in gta_ipls:
        b = _basename(gp)
        if b.endswith(".zon"):
            continue
        if b not in text_ipls:
            rep(f"note: gta.dat IPL {gp} not available in the source repos (no inst data expected)")
            continue
        insts, other = parse_ipl_insts(text_ipls[b])
        stem = b.rsplit(".", 1)[0]
        text_insts[stem] = insts
        loaded_text.append(stem)
    for b, t in text_ipls.items():  # IPL files present in the game folder but not in gta.dat
        stem = b.rsplit(".", 1)[0]
        if stem not in text_insts:
            insts, _ = parse_ipl_insts(t)
            rep(f"note: {b} is not referenced by gta.dat ({len(insts)} inst lines ignored)")

    pl = collections.defaultdict(lambda: dict(text=0, stream=0, scripted=0, ipls=collections.Counter(),
                                              lods=collections.Counter(), area=0, same_pivot=0))
    lod_children = collections.defaultdict(collections.Counter)
    bad_lod = 0
    unknown_ids = collections.Counter()

    def add(inst, ipl, kind, lod_inst):
        mid = inst[0]
        lod_model = lod_inst[0] if lod_inst else -1
        if mid not in models:
            unknown_ids[mid] += 1
        p = pl[mid]
        p[kind] += 1
        p["ipls"][ipl] += 1
        if inst[2] & 0xFF:
            p["area"] += 1
        p["lods"][lod_model] += 1
        if lod_inst:
            lod_children[lod_model][mid] += 1
            # LOD instance at the same pivot (position <= 0.1, same quaternion up to sign)
            dpos = max(abs(a - b) for a, b in zip(inst[3:6], lod_inst[3:6]))
            dq = min(max(abs(a - b) for a, b in zip(inst[6:10], lod_inst[6:10])),
                     max(abs(a + b) for a, b in zip(inst[6:10], lod_inst[6:10])))
            if dpos <= 0.1 and dq <= 0.01:
                p["same_pivot"] += 1

    for stem in loaded_text:
        insts = text_insts[stem]
        for inst in insts:
            lod = inst[10]
            li = None
            if lod >= 0:
                if lod < len(insts):
                    li = insts[lod]
                else:
                    bad_lod += 1
            add(inst, stem, "text", li)
    related_count = collections.Counter()
    for nm, insts in bin_ipls.items():
        rel = next((t for t in loaded_text if nm.lower().startswith(t + "_stream")), None)
        related_count[rel] += 1
        for inst in insts:
            lod = inst[10]
            li = None
            if lod >= 0:
                if rel is not None and lod < len(text_insts[rel]):
                    li = text_insts[rel][lod]
                else:
                    bad_lod += 1
            add(inst, nm, "stream" if rel else "scripted", li)
    rep(f"binary IPLs without a related gta.dat text IPL (script-toggled): "
        f"{sorted(n for n in bin_ipls if not any(n.lower().startswith(t + '_stream') for t in loaded_text))}")
    rep(f"placements: {sum(len(text_insts[s]) for s in loaded_text)} text + "
        f"{sum(len(v) for v in bin_ipls.values())} binary instances; unresolvable LOD indices: "
        f"{bad_lod}; instances of undefined model ids: {sum(unknown_ids.values())}")

    lod_by_name = lambda n: n.lower().startswith("lod")
    n_lod_named_targets = sum(1 for t in lod_children if t in models and lod_by_name(models[t]["name"]))
    rep(f"LOD targets: {len(lod_children)} distinct models used as LOD by vanilla placements, "
        f"{n_lod_named_targets} of them named 'lod*'")
    for m in models.values():
        p = pl.get(m["id"])
        m["num_placements_text"] = p["text"] if p else 0
        m["num_placements_stream"] = p["stream"] if p else 0
        m["num_placements_scripted"] = p["scripted"] if p else 0
        m["num_vanilla_placements"] = (p["text"] + p["stream"] + p["scripted"]) if p else 0
        m["placement_ipls"] = ";".join(f"{k}:{v}" for k, v in p["ipls"].most_common(4)) if p else ""
        if p:
            grp = collections.Counter()
            for k, v in p["ipls"].items():
                grp[ipl_group(k)] += v
            m["placement_region"] = grp.most_common(1)[0][0]
            m["placed_in_interior_share"] = round(p["area"] / max(1, m["num_vanilla_placements"]), 3)
            with_lod = {k: v for k, v in p["lods"].items() if k >= 0}
            if with_lod:
                lm, cnt = max(with_lod.items(), key=lambda kv: (kv[1], -kv[0]))
                m["lod_model_id"] = lm
                m["lod_model_name"] = models[lm]["name"] if lm in models else ""
                m["lod_share"] = round(cnt / m["num_vanilla_placements"], 3)
                m["lod_alternatives"] = len(with_lod) - 1
                m["lod_same_pivot_share"] = round(p["same_pivot"] / sum(with_lod.values()), 3)
        ch = lod_children.get(m["id"])
        m["lod_target_uses"] = sum(ch.values()) if ch else 0
        m["lod_n_children"] = len(ch) if ch else 0
        m["lod_parent_of"] = ";".join(str(k) for k, _ in ch.most_common()) if ch else ""
        m["lod_by_name"] = lod_by_name(m["name"])
        m["is_lod"] = bool(ch) or m["lod_by_name"]

    # ---- co-located companions (same pivot within 1 cm, e.g. alpha / night layers) ---
    at_pos = collections.defaultdict(set)
    for stem in loaded_text:
        for inst in text_insts[stem]:
            at_pos[(round(inst[3], 2), round(inst[4], 2), round(inst[5], 2))].add(inst[0])
    for insts in bin_ipls.values():
        for inst in insts:
            at_pos[(round(inst[3], 2), round(inst[4], 2), round(inst[5], 2))].add(inst[0])
    comp = collections.defaultdict(collections.Counter)
    for ids in at_pos.values():
        if len(ids) > 1:
            for a in ids:
                for b in ids:
                    if a != b and b in models and not models[b]["is_lod"]:
                        comp[a][b] += 1
    for m in models.values():
        n = m["num_vanilla_placements"]
        c = comp.get(m["id"])
        m["companions"] = ";".join(f"{b}:{k}" for b, k in c.most_common(6) if n and k / n >= 0.5) if c else ""
        lm = m.get("lod_model_id")
        m["lod_ide_file"] = models[lm]["ide_file"] if lm is not None and lm in models else ""
    for m in models.values():
        lm = m.get("lod_model_id")
        if lm is not None and lm in models:
            # reusable for a single placement elsewhere: the LOD serves only this HD model
            # and sits on the same pivot (merged multi-building LODs are not reusable)
            m["lod_exclusive"] = models[lm]["lod_n_children"] == 1
            m["lod_reusable"] = m["lod_exclusive"] and m.get("lod_same_pivot_share", 0) >= 0.9

    data = dict(models=models, ide_info=ide_info, gta_raw=gta_raw, gta_txt=gta_txt,
                gta_entries=gta_entries, def_entries=def_entries, text_insts=text_insts,
                bin_ipls=bin_ipls, loaded_text=loaded_text, txd_parent=txd_parent,
                name2id=name2id)
    return data


# --------------------------------------------------------------------------------------
# prineside categories
# --------------------------------------------------------------------------------------
def load_prineside(fetch: Fetcher, rep: Report) -> tuple[dict, dict]:
    """-> (id -> {'cats': [..], 'title', 'radius', 'width', 'length', 'height'},
           category -> count)"""
    info, counts = {}, collections.Counter()
    for cat, parent in PRINESIDE_CATEGORIES:
        url = PRINESIDE_BASE.format(cat=cat)
        first = fetch.get(url).decode("utf-8", "replace")
        last = prineside_last_page(first)
        pages = [first] + [fetch.get(url + f"?page={p}").decode("utf-8", "replace")
                           for p in range(2, last + 1)]
        seen = set()
        for page in pages:
            for it in parse_prineside_listing(page):
                if it["id"] in seen:
                    continue
                seen.add(it["id"])
                d = info.setdefault(it["id"], dict(cats=[], title="", name=it["name"]))
                d["cats"].append(cat)
                if it["title"] and not d["title"]:
                    d["title"] = it["title"]
                for k in ("radius", "width", "length", "height"):
                    if it[k] not in (None, ""):
                        d[k] = float(it[k])
        counts[cat] = len(seen)
    rep(f"prineside: {len(PRINESIDE_CATEGORIES)} categories scraped, {len(info)} distinct model "
        f"ids categorised")
    return info, counts


# --------------------------------------------------------------------------------------
# Engine / data-file dependencies on models (for the keep/drop decision)
# --------------------------------------------------------------------------------------
# Object models referenced by *numeric* MODEL_* constant in gta-reversed game code
# (source/game_sa, commit afbc26d7, excluding debug tool menus). Derived with
# --scan-gtarev (code search for files containing "MODEL" -> download -> regex). Peds,
# vehicles and weapons (default.dat IDEs) are not listed here.
HARDCODED_OBJECT_MODEL_REFS = {
    1: "CutsceneMgr.cpp, CutsceneObject.cpp", 300: "CutsceneMgr.cpp, TempColModels.h",
    319: "CutsceneMgr.cpp, TempColModels.h", 384: "ClothesBuilder.cpp",
    394: "TaskSimplePlayHandSignalAnim.cpp", 395: "TaskSimplePlayHandSignalAnim.cpp",
    396: "TaskSimplePlayHandSignalAnim.cpp", 397: "TaskSimplePlayHandSignalAnim.cpp",
    630: "TheCarGenerators.cpp", 1211: "Physical.cpp",
}


def scan_gtarev_model_refs(fetch: Fetcher, name2id: dict, models: dict, rep: Report) -> dict:
    """Optional: recompute HARDCODED_OBJECT_MODEL_REFS (needs the `gh` CLI)."""
    import subprocess
    paths, page = [], 1
    while page <= 10:
        out = subprocess.run(["gh", "api", "-X", "GET", "search/code", "-f",
                              "q=repo:gta-reversed/gta-reversed MODEL", "-f", "per_page=100",
                              "-f", f"page={page}"], capture_output=True, text=True, check=True)
        items = json.loads(out.stdout).get("items", [])
        paths += [it["path"] for it in items]
        if len(items) < 100:
            break
        page += 1
        time.sleep(7)  # code search rate limit
    refs = collections.defaultdict(set)
    for p in sorted(set(paths)):
        if p.endswith("eModelID.h") or "toolsmenu" in p or "DebugModules" in p:
            continue
        t = (fetch.github("gtarev_pinned", p, required=False) or b"").decode("utf-8", "replace")
        for m in re.finditer(r"\bMODEL_[A-Z0-9_]+\b", t):
            i = name2id.get(m.group())
            if i is not None and i in models and models[i]["type"] not in ("vehicle", "ped", "weapon"):
                refs[i].add(p.rsplit("/", 1)[-1])
    rep(f"--scan-gtarev: {len(paths)} files, {len(refs)} object model ids referenced")
    return {i: ", ".join(sorted(v)) for i, v in sorted(refs.items())}


def data_file_model_names(fetch: Fetcher, rep: Report) -> dict[str, set]:
    """Model names referenced by procobj.dat / carmods.dat / object.dat (lower case)."""
    out = {}
    for name, copies in DATA_FILES.items():
        texts = [norm_text(fetch.github(k, p)) for k, p in copies]
        if any(t != texts[0] for t in texts[1:]):
            rep(f"WARNING {name}: copies differ")
        names = set()
        sec = None
        for _, s in engine_lines(texts[0]):
            if s.startswith(";"):
                continue
            tok = s.split()
            if name == "procobj.dat" and len(tok) >= 2:
                names.add(tok[1].lower())
            elif name == "object.dat" and tok and not s.startswith("*"):
                names.add(tok[0].lower())
            elif name == "carmods.dat":
                if sec is None:
                    sec = tok[0][:4]
                    continue
                if tok[0].startswith("end"):
                    sec = None
                    continue
                if sec == "link":
                    names.update(t.lower() for t in tok[:2])
                elif sec == "mods":
                    names.update(t.lower() for t in tok[1:])  # tok[0] is the vehicle
                elif sec == "whee":
                    names.update(t.lower() for t in tok[1:])  # tok[0] is the wheel set id
        out[name] = names
        rep(f"{name}: {len(names)} model names referenced ({len(copies)} identical copies)")
    return out


# --------------------------------------------------------------------------------------
# Building candidates
# --------------------------------------------------------------------------------------
BUILDING_CATS = {
    "houses-and-apartments": "residential", "offices-and-skyscrapers": "office",
    "factories-and-warehouses": "industrial", "stores-and-shops": "retail",
    "restaurants-and-hotels": "hospitality", "other-buildings": "other",
    "garages-and-petrol-stations": "garage", "sports-and-stadium-objects": "sports",
    "bars-clubs-and-casinos": "hospitality", "graveyard": "other",
}
# categories that are buildings only if name/size say so
MAYBE_BUILDING_CATS = {"car-parks", "general-industrial", "airport-and-aircraft-objects",
                       "farm-objects", "construction-industrial", "miscellaneous", "structures",
                       "industrial"}
TERRAIN_CATS = {"land-masses", "concrete-and-rock", "grass-and-dirt", "roads-bridges-and-tunnels",
                "railroads", "transportation", "beach-and-sea"}
PROP_CATS = {"street-and-road-items", "lights", "signs-billboards-and-statues",
             "fences-walls-gates-and-barriers", "trees", "plants", "rocks", "nature", "trash",
             "crates-drums-and-racks", "wires-and-cables", "alpha-channels-and-non-collidable",
             "nighttime-objects", "ladders-stairs-and-scaffolding", "ramps", "cranes",
             "interior-objects", "more-interiors"}

# Obvious SA branding / LS-SF-LV landmarks. Matched (regex, lower case) against
# "<model name> <txd> <prineside title>". Hand-curated from the vanilla names; see notes.
BRANDING_PATTERNS = [  # (label, regex, also test the TXD name?)
    ("Burger Shot", r"burgsht|burgershot|burger ?shot|boiga|burgerland|walburger|burger0\d|\bbs_sfs|burg_alpha", True),
    ("Cluckin' Bell", r"cluck", True),
    ("Well Stacked / Zip pizza", r"pizz|wellstack|well stacked|zip pizza", True),
    ("Jim's Sticky Ring", r"donut|ringking|sticky ?ring", True),
    ("24/7 / Supa Save", r"\b247\b|24_7|24hr|7_?11|supasave|supa save", True),
    ("Ammu-Nation", r"ammu|amun|gunclub|gunbldg|gunshop", True),
    ("clothes shops", r"binco|prolap|victim|\bzip\b|zip_|suburban|didier|sachs", True),
    ("Pay'n'Spray", r"spray", True),
    ("mod garages / branded fuel", r"transfend|tfender|locolow|loco low|wheelarch|wheel arch|modshop|\bmodlas|xenon|xoomer", True),
    ("tattoo/barber/sex shop", r"tatoo|tattoo|barber|sexshop|sex shop", True),
    ("casino/LV strip themed", r"casino|csrs|caligula|triad|dragon|emerald|starfish|strfsh|visage"
                               r"|pirat|pirte|prtskll|prthotel|excal|exclbr|pyram|pyrmd|luxor|sphinx"
                               r"|casroy|royale|clwn|clown|flamingo|mirage|venetian|vrock|tiki|ballys"
                               r"|fremnt|gambl|camel|highroller|circus|sham_|shamcp|shampark", True),
    ("bars/clubs", r"pigpen|alhambra|jizzy|tenpin|gayclub|stripbar|nightclub|ufoinn|westsaloon", True),
    ("Chinatown themed (Chinese signage)", r"chinatown|chnatwn|chinatwn|chinawn|china_?town", True),
    ("landmark", r"vinewood|hollywood|cityhall|ctiyhall|librtow|libbase|libplaza|libtwr|pershing"
                 r"|fighotbase|ferris|lighhouse|lighthouse|lithus|lighth\b|cathedral|cath_|gwforum"
                 r"|\bcoit|ggbrig|golden ?gate|teleshed|telesc|\ba51|area ?51|area69|chiliad|\bdam_"
                 r"|dambuild|dambit|sherman|bigpointy|transmit|wddng|wdngchpl|wedding|sprunk|observ"
                 r"|griff|\bunity|ferybuild|ferry ?build|filmstud|tvstudio|\bci_|drvin|drivein"
                 r"|vgsbikeschl|bikeschool|drivingsch|cinema|cineblock|cinemark|cine_mark"
                 r"|stadtplaza|artgallery|city ?hall|pointy|wedchap|weddingchap", False),
]
_BRAND_RE = [(lab, re.compile(p), txd) for lab, p, txd in BRANDING_PATTERNS]


def branding_match(name: str, txd: str, title: str):
    """-> (label, matched text) or None."""
    a = (name + " " + title).lower()
    b = txd.lower()
    for lab, rx, use_txd in _BRAND_RE:
        mt = rx.search(a) or (rx.search(b) if use_txd else None)
        if mt:
            return lab, mt.group()
    return None

# overlays / detail layers placed on top of a building (not usable on their own)
OVERLAY_RE = re.compile(r"alpha|alfa|\balp\b|_alp|shad|_dtl|detail|decal|nite|night|neon"
                        r"|sign|glass|_gls|lights?(_|\b|\d)|^lts|wires?\b|_?wins?(_|\b|\d)|windows"
                        r"|stains|dirt|grime|smash|_dam\b|_dmg|broken")
OVERLAY_CASE_RE = re.compile(r"[0-9a-z]Tr(_|$|ans)")  # e.g. venice03Tr_laW2, laehospitalTr

TERRAIN_NAME_RE = re.compile(r"land|lnd|grnd|ground|gnd|hill|cliff|beach|sand|seabed|seafloor"
                             r"|coast|shore|rock|dirt|grass|lawn(?!_)|terrain|road|\brd|rd_|_rd"
                             r"|freeway|fway|hiway|junction|junc|street(?!s?hop)|pave|kerb|curb"
                             r"|sidewalk|walkway|carpark|cpark|parking|\bbit|_bit|bits?\d|lodbit"
                             r"|plaza|slope|border|embank|tunnel|bridge|brij|brig|pier|dock|canal"
                             r"|river|lake|island|field|course|golf|track|trax|rail")
WEAK_GROUND_RE = re.compile(r"park|yard|garden|plot|lot\b|block|blok|base|\bgrd|_gr\b|stuff"
                            r"|area|mound|plinth|platform")

STYLE_ORDER = ["detached_house", "rowhouse/terrace", "low_apartment", "mid_apartment", "tower",
               "office", "shop/retail", "industrial/warehouse", "garage", "church/religious",
               "stadium", "misc"]

_TOK = {
    "house": re.compile(r"hse|house|hous|hos\d|hoos|home|villa|bungal|cabin|shack|shed\b|hut\b"
                        r"|trailer|caravan|mansion|mans\d|farmh|ranch|cottage|pueblo|adobe|hacienda"),
    "row": re.compile(r"row|terr(ace)?|vict|vic_|tnhs|townh|hseblk|barrio|rowhs|terrace"),
    "apartment": re.compile(r"apart|aprt|apts|flat|condo|tenem|tnmnt|proj|blok|block|resid"),
    "office": re.compile(r"offic|offblok|officy|offven|bank|corp|business|sky|scrap|towr|tower"
                         r"|highrise|hirise|civic"),
    "retail": re.compile(r"shop|store|mall|market|mkt|retail|strip|diner|cafe|restau|restrnt"
                         r"|motel|hotel|htl|\binn\b|liquor|launder|super|bar\b|saloon|grill"),
    "industrial": re.compile(r"wrehse|warehouse|wareh|whouse|whse|wrhs|warhus|fact|plant|indust"
                             r"|depot|hangar|hanger|hngr|barn|silo|works|mill|foundry|refin|abat"
                             r"|abbat|dock|cargo|storage|stor\b|freight|frght|frate|crack|powerst"
                             r"|pwr|substa|pumphouse|pumphse|boathouse|shed"),
    "garage": re.compile(r"garage|garag|grge|lockup|lckup|carport|carshade|mstorcp|multistor"
                         r"|multustor|multicar|carprk|cprk|carpark"),
    "fuel": re.compile(r"gas|petrol|fuel|canopy|ptrl"),
    "church": re.compile(r"church|chrch|chapel|chpl|temple|mosque|synag|mission_?church"),
    "stadium": re.compile(r"stadium|stad\b|stadtplaza|arena|forum|bowl|bleacher|baseball|football"),
    "civic": re.compile(r"hosp|police|sherrif|sheriff|jail|prison|court|school|college|station"
                        r"|stat\d|library|museum|post|firest|fire_st|town ?hall|guard|booth"
                        r"|terminal|airport|control"),
}


def _style_and_use(m, cats, text):
    """Return (style, use, reason) for a building candidate."""
    h = m["height"]
    fl, fs = m["footprint_long"], m["footprint_short"]
    aspect = fl / max(fs, 0.1)
    bcats = [c for c in cats if c in BUILDING_CATS]
    use = BUILDING_CATS[bcats[0]] if bcats else None
    tk = {k: bool(r.search(text)) for k, r in _TOK.items()}
    if use is None:
        for k, u in (("garage", "garage"), ("church", "religious"), ("stadium", "sports"),
                     ("industrial", "industrial"), ("office", "office"), ("retail", "retail"),
                     ("house", "residential"), ("apartment", "residential"), ("civic", "other")):
            if tk[k]:
                use = u
                break
    if use == "other":  # 'other-buildings' on prineside: refine with name tokens
        for k, u in (("church", "religious"), ("house", "residential"), ("apartment", "residential"),
                     ("garage", "garage"), ("industrial", "industrial"), ("retail", "retail"),
                     ("office", "office")):
            if tk[k]:
                use = u
                break
    if "car-parks" in cats:
        use = "garage"
    use = use or "other"

    def by_height(low_style):
        if h >= 36:
            return "tower"
        if h >= 19:
            return "mid_apartment"
        return low_style

    if tk["church"] and use in ("other", "residential", "hospitality"):
        return "church/religious", "religious", "name"
    if use == "sports" or tk["stadium"]:
        if fl >= 70 and (tk["stadium"] or use == "sports"):
            return "stadium", "sports", "category/name+size"
        return "misc", "sports", "small sports building"
    if use == "garage":
        if tk["fuel"] and not tk["garage"]:
            return "misc", "fuel_station", "petrol station/canopy"
        return "garage", "garage", "category/name"
    if use == "industrial":
        return ("industrial/warehouse" if h < 60 else "misc"), "industrial", "category/name"
    if use == "office":
        if h >= 36:
            return "tower", "office", "office >= 11 floors"
        return "office", "office", "category/name"
    if use in ("retail", "hospitality"):
        if h >= 15:
            return by_height("low_apartment"), "mixed_" + use, "tall retail/hotel = mixed use block"
        return "shop/retail", use, "category/name"
    if use == "residential":
        if h >= 19:
            return by_height("low_apartment"), "residential", "height"
        small = fl <= 32 and fs <= 26
        if re.search(r"trailer|caravan", text) and h < 8:
            return "detached_house", "residential", "trailer/mobile home"
        if tk["row"] and h < 19 and aspect >= 1.8:
            return "rowhouse/terrace", "residential", "name (row/terrace/victorian/barrio)"
        if small and h < 12:
            if aspect >= 2.6 and fs <= 14:
                return "rowhouse/terrace", "residential", "small elongated footprint"
            return "detached_house", "residential", "small footprint, low"
        if aspect >= 3.0 and fs <= 16 and h < 14:
            return "rowhouse/terrace", "residential", "long narrow footprint"
        if h < 8 and tk["house"] and aspect >= 2.0 and fs <= 20:
            return "rowhouse/terrace", "residential", "row of low houses"
        if h >= 7 or fl > 32:
            return "low_apartment", "residential", "2-5 floors / larger footprint"
        return "detached_house", "residential", "low"
    # other / civic / unknown (towers only for residential/office/mixed use)
    if tk["church"]:
        return "church/religious", "religious", "name"
    if tk["civic"]:
        return "misc", "civic", "civic name (hospital/police/school/station/...)"
    return "misc", use, "no specific signal"


def ground_risk(m, cats, title):
    score, why = 0, []
    nm = (m["name"] + " " + m["txd"]).lower()
    t = TERRAIN_NAME_RE.search(nm)
    if t:
        strong = re.search(r"land|lnd|grnd|ground|gnd|hill|cliff|beach|road|plaza", t.group())
        score += 3 if strong else 2
        why.append(f"name:{t.group()}")
    w = WEAK_GROUND_RE.search(nm)
    if w:
        score += 1
        why.append(f"name~{w.group()}")
    if m["name"].lower().startswith("lod") or "lod" in m["name"].lower():
        score += 2
        why.append("name:lod")
    tc = sorted(set(cats) & (TERRAIN_CATS | {"beach-and-sea", "car-parks"}))
    if tc:
        score += 3
        why.append("prineside:" + "/".join(tc))
    tt = re.search(r"landscape|plot|driveway|yard|garden|grass|ground|road|part of", title.lower())
    if tt:
        score += 2
        why.append(f"title:{tt.group()}")
    fl, h = m["footprint_long"], m["height"]
    area = m["footprint_x"] * m["footprint_y"]
    flat = h / max(fl, 0.1)
    if fl >= 150:
        score += 3
        why.append("footprint>=150m")
    elif fl >= 90:
        score += 1
        why.append("footprint>=90m")
    if fl >= 40 and flat < 0.12:
        score += 2
        why.append(f"flat(h/len={flat:.2f})")
    elif fl >= 25 and flat < 0.2:
        score += 1
        why.append(f"flattish(h/len={flat:.2f})")
    if area >= 8000:
        score += 1
        why.append("area>=8000m2")
    if (m["region"] == "SF" and m.get("use", "").startswith(("residential", "mixed"))
            and h >= 19 and fl >= 40):
        score += 2
        why.append("SF hillside block (height incl. slope?)")
    level = "high" if score >= 4 else "medium" if score >= 2 else "low"
    return level, score, ";".join(why)


def floors_estimate(h):
    # SA buildings usually extend a few metres below their ground line (slope fitting);
    # ~3.3 m per storey.
    return max(1, int(round((h - 1.5) / 3.3)))


SPORTS_TERRAIN_RE = re.compile(r"golf|grounds?|field|course|track|trak|skate|pitch|court")
SLENDER_RE = re.compile(r"pylon|mast|chimney|stack|pole|antenna|aerial|tower_?\d*$|watertank|wtrtank|silo")


def build_candidates(models: dict, prine: dict, rep: Report) -> list[dict]:
    cands, why_out = [], collections.Counter()
    for m in sorted(models.values(), key=lambda r: r["id"]):
        if m["type"] not in ("atomic", "time"):
            continue
        if m["region"] not in ("LA", "SF", "LV", "country"):
            continue
        p = prine.get(m["id"], {})
        cats = p.get("cats", [])
        title = p.get("title", "")
        text = (m["name"] + " " + m["txd"] + " " + title).lower()

        def reject(reason):
            why_out[reason] += 1

        if m["is_lod"]:
            reject("LOD model")
            continue
        if m.get("placed_in_interior_share", 0) >= 0.5:
            reject("placed in interiors")
            continue
        if (m["flags"] or 0) & 0x1:
            reject("IS_ROAD flag (road/ground)")
            continue
        bc_ = [c for c in cats if c in BUILDING_CATS]
        maybe = [c for c in cats if c in MAYBE_BUILDING_CATS]
        tk_building = any(_TOK[k].search(text) for k in
                          ("house", "apartment", "office", "retail", "industrial", "garage",
                           "church", "civic"))
        if not bc_:
            if set(cats) & TERRAIN_CATS and "car-parks" not in cats:
                reject("terrain/road/sea category")
                continue
            if set(cats) & PROP_CATS:
                reject("prop category")
                continue
            if not (maybe or not cats) or not tk_building:
                reject("not a building (category/name)")
                continue
        if OVERLAY_RE.search(m["name"].lower()) or OVERLAY_CASE_RE.search(m["name"]):
            reject("overlay/detail layer")
            continue
        brand = branding_match(m["name"], m["txd"], title)
        if brand:
            reject(f"branding/landmark: {brand[0]}")
            continue
        # sizes: collision AABB, else prineside (DFF based) box
        if m.get("has_col_bounds"):
            fx, fy, h, zmin, src = m["footprint_x"], m["footprint_y"], m["height"], m["z_min"], "col"
        elif "width" in p:
            fx, fy, h, zmin, src = p["width"], p["length"], p["height"], None, "prineside"
        else:
            reject("no size information")
            continue
        fl, fs = max(fx, fy), min(fx, fy)
        if fl < 5 or fs < 2.5 or h < 3:
            reject("too small (prop)")
            continue
        if fl < 12 and (h > 2.0 * fl or SLENDER_RE.search(m["name"].lower())):
            reject("slender structure (pylon/chimney/tank/steeple)")
            continue
        if not bc_ and "car-parks" in cats and h < 6:
            reject("surface car park")
            continue
        if ("sports-and-stadium-objects" in cats and SPORTS_TERRAIN_RE.search(text)
                and (not _TOK["stadium"].search(text) or h < 10)):
            reject("sports ground/terrain")
            continue
        if "graveyard" in cats and not _TOK["church"].search(text):
            reject("graveyard")
            continue
        if re.search(r"pier|jetty|quay|wharf_?struct", m["name"].lower()) and not (
                set(bc_) - {"other-buildings"}):
            reject("waterfront structure (pier/jetty/quay)")
            continue
        c = dict(m)
        c.update(size_source=src, footprint_x=fx, footprint_y=fy, height=h, z_min=zmin,
                 footprint_long=fl, footprint_short=fs)
        style, use, sreason = _style_and_use(c, cats, text)
        if h > 3.0 * fs and h >= 30 and use not in ("office", "residential", "mixed_retail",
                                                     "mixed_hospitality", "hospitality"):
            reject("slender structure (pylon/chimney/tank/steeple)")
            continue
        c["use"] = use
        gr, gs, gwhy = ground_risk(c, cats, title)
        if gr == "high" and not bc_:
            reject("terrain-like (high ground risk, no building category)")
            continue
        block_like = fs >= 45 and use.startswith(("residential", "mixed", "retail"))
        if block_like:
            sreason += "; large footprint: probably several buildings / a city block"
        if re.search(r"hill", m["name"].lower()) and h >= 12:
            sreason += "; hillside model: height includes slope"
        c.update(style=style, use=use, style_reason=sreason, ground_risk=gr, block_like=block_like,
                 ground_risk_score=gs, ground_risk_reasons=gwhy,
                 floors_est=floors_estimate(h), aspect=round(fl / max(fs, 0.1), 2),
                 prineside_category=";".join(cats), prineside_title=title,
                 pivot_height_share=(round(-zmin / h, 2) if zmin is not None and h > 0 else None))
        cands.append(c)
    rep(f"building candidates: {len(cands)}; rejected (exterior HD/LOD models of LA/SF/LV/country "
        f"IDEs): " + ", ".join(f"{k} {v}" for k, v in why_out.most_common()))
    return cands


# --------------------------------------------------------------------------------------
# IDE keep/drop recommendation
# --------------------------------------------------------------------------------------
GENERIC_PROP_CATS = {"trees", "plants", "lights", "street-and-road-items",
                     "signs-billboards-and-statues", "fences-walls-gates-and-barriers", "trash",
                     "rocks"}

# hand-written role notes (verified against the data where a number is quoted)
IDE_NOTES = {
    "default.ide": ("required", "loaded by default.dat: weapons, cutscene objects (hier), clothes/hand models; hardcoded ids"),
    "vehicles.ide": ("required", "loaded by default.dat: all 212 vehicles"),
    "peds.ide": ("required", "loaded by default.dat: peds"),
    "vegepart.ide": ("keep", "generic vegetation (trees, palms, bushes, grass); 4 procobj.dat models; veg_palmkb8 hardcoded"),
    "barriers.ide": ("keep", "generic road barriers / gates; MI_FENCE, MI_FENCE2 looked up by name"),
    "dynamic.ide": ("keep", "pickups (health, armour, info, bribe, ...), hydrant, bins, phone booths, parking meters, cones, lamp posts, traffic lights: >50 names looked up by the exe; fire_hydrant hardcoded"),
    "dynamic2.ide": ("keep", "more street furniture/dynamic props; CJ_TRAFFIC_LIGHT3-5, train crossings, cranes looked up by name"),
    "multiobj.ide": ("keep", "generic misc objects (sea life, beach props, escalator steps, satellite dishes, pallets); ~30 names looked up by the exe"),
    "procobj.ide": ("keep", "procedural surface objects used by data/procobj.dat (a missing name => crash when that surface appears) unless procobj.dat is replaced"),
    "propext.ide": ("keep", "small exterior props (generators, crates, aircon, fruit crates); oyster/horseshoe collectibles"),
    "props.ide": ("drop", "interior furniture/props (vanilla placements 100% inside interiors)"),
    "props2.ide": ("drop", "interior shop/furniture props (vanilla placements ~100% inside interiors)"),
    "veh_mods.ide": ("required", "vehicle upgrades: carmods.dat lookups are not null-checked (CVehicleModelInfo::LoadVehicleUpgrades); nitro/hydraulics/stereo/wheels"),
    "txdcut.ide": ("keep", "only txdp (cutscene TXD parents); uses no model-info slots"),
    "levelmap.ide": ("drop", "Liberty City replica (mission 'The Da Nang Thang'/Saint Mark's) in an interior"),
    "levelxre.ide": ("optional", "mission/script props (garage doors, keypads, ATM, containers for cranes, harvester body parts, pool/basketball minigames); only needed by main.scm features"),
    "seabed.ide": ("drop", "SA ocean floor terrain"),
}


def ide_summary_rows(data, cands, prine, refs):
    models = data["models"]
    by_ide = collections.defaultdict(list)
    for m in models.values():
        by_ide[m["ide_file"]].append(m)
    cand_by_ide = collections.defaultdict(list)
    for c in cands:
        cand_by_ide[c["ide_file"]].append(c)
    rows = []
    for gp, inf in data["ide_info"].items():
        ms = by_ide.get(gp, [])
        st = inf["stores"]
        b = _basename(gp)
        props = [m for m in ms if set(prine.get(m["id"], {}).get("cats", [])) & GENERIC_PROP_CATS
                 and not m["is_lod"]]
        cs = cand_by_ide.get(gp, [])
        styles = collections.Counter(c["style"] for c in cs)
        lowrisk = sum(1 for c in cs if c["ground_risk"] == "low")
        eng = collections.Counter()
        mi_names = []
        for m in ms:
            for k in refs.get(m["id"], ()):
                eng[k] += 1
            if "MI" in refs.get(m["id"], ()):
                mi_names.append(f"{m['id']}:{m['name']}")
        role, note = IDE_NOTES.get(b, (None, ""))
        rows.append(dict(
            ide_file=gp, load_order=inf["order"], loaded_by=inf["loaded_by"], region=inf["region"],
            n_objs=inf["counts"].get("objs", 0), n_objs_damageable=st.get("damage_atomic", 0),
            n_tobj=inf["counts"].get("tobj", 0), n_anim=inf["counts"].get("anim", 0),
            n_weap=inf["counts"].get("weap", 0), n_hier=inf["counts"].get("hier", 0),
            n_cars=inf["counts"].get("cars", 0), n_peds=inf["counts"].get("peds", 0),
            n_txdp=inf["n_txdp"], n_2dfx=inf["counts"].get("2dfx", 0),
            atomic_slots=st.get("atomic", 0), damage_atomic_slots=st.get("damage_atomic", 0),
            time_slots=st.get("time", 0), clump_slots=st.get("clump", 0),
            model_info_slots=sum(v for k, v in st.items() if k in ("atomic", "damage_atomic", "time", "clump")),
            id_min=inf["id_min"], id_max=inf["id_max"],
            n_lod_models=sum(1 for m in ms if m["is_lod"]),
            n_placed_models=sum(1 for m in ms if m["num_vanilla_placements"] > 0),
            n_vanilla_placements=sum(m["num_vanilla_placements"] for m in ms),
            n_building_candidates=len(cs), n_candidates_low_ground_risk=lowrisk,
            candidate_styles=";".join(f"{k}:{styles[k]}" for k in STYLE_ORDER if styles.get(k)),
            n_generic_props=len(props),
            engine_name_lookups=eng.get("MI", 0), engine_name_models=";".join(mi_names),
            procobj_dat_models=eng.get("procobj.dat", 0),
            carmods_dat_models=eng.get("carmods.dat", 0), object_dat_models=eng.get("object.dat", 0),
            hardcoded_ids=eng.get("hardcoded", 0),
            role_note=note, _role=role))
    return rows


STYLE_TARGETS = {  # how many distinct, low-ground-risk SA models we would like per style
    "detached_house": 90, "rowhouse/terrace": 10, "low_apartment": 80, "mid_apartment": 80,
    "tower": 60, "office": 30, "shop/retail": 60, "industrial/warehouse": 50, "garage": 15,
    "church/religious": 4, "stadium": 2, "misc": 15,
}
CITY_SLOT_BUDGET = 4500  # atomic slots we allow for city IDEs in the whole-file option


def recommend(rows, cands, data, rep: Report):
    """Fill 'recommendation'/'reason' per IDE and return a summary dict."""
    cand_by_ide = collections.defaultdict(list)
    for c in cands:
        cand_by_ide[c["ide_file"]].append(c)
    w = {"low": 1.0, "medium": 0.5, "high": 0.1}
    city = [r for r in rows if r["_role"] is None and r["region"] in ("LA", "SF", "LV", "country")]
    for r in rows:
        if r["_role"] in ("required", "keep"):
            r["recommendation"] = "keep"
        elif r["_role"] in ("drop",):
            r["recommendation"] = "drop"
        elif r["_role"] == "optional":
            r["recommendation"] = "drop (optional)"
        elif r["region"] == "interior":
            r["recommendation"] = "drop"
            r["role_note"] = r["role_note"] or "interior models (SA interiors are not used in the Baku map)"
    have = collections.Counter()
    chosen, used = [], 0

    def gain(r):
        add = collections.Counter()
        for c in cand_by_ide[r["ide_file"]]:
            add[c["style"]] += w[c["ground_risk"]]
        g = 0.0
        for st, tgt in STYLE_TARGETS.items():
            before = min(have[st], tgt) / tgt
            after = min(have[st] + add[st], tgt) / tgt
            g += (after - before) * (2.0 if st in ("low_apartment", "mid_apartment", "tower",
                                                    "detached_house") else 1.0)
        return g, add

    pool = list(city)
    while pool:
        scored = []
        for r in pool:
            g, add = gain(r)
            scored.append((g / max(1, r["atomic_slots"] + r["time_slots"]) * 1000, g, r, add))
        scored.sort(key=lambda t: -t[0])
        best = scored[0]
        if best[1] <= 0.05 or used + best[2]["atomic_slots"] > CITY_SLOT_BUDGET:
            # try a smaller IDE that still fits
            fit = [t for t in scored if t[1] > 0.05 and used + t[2]["atomic_slots"] <= CITY_SLOT_BUDGET]
            if not fit:
                break
            best = fit[0]
        _, g, r, add = best
        have.update(add)
        chosen.append(r)
        used += r["atomic_slots"]
        pool.remove(r)
        r["recommendation"] = "keep"
        r["role_note"] = (f"buildings: adds {sum(add.values()):.0f} weighted candidates "
                          f"(greedy rank {len(chosen)})")
    for r in city:
        if r not in chosen:
            r["recommendation"] = "drop"
            r["role_note"] = r["role_note"] or "city IDE not needed for building variety (see notes)"
    # LOD availability of candidates inside kept IDEs
    kept = {r["ide_file"] for r in rows if r["recommendation"] == "keep"}
    kc = [c for c in cands if c["ide_file"] in kept]
    with_lod = [c for c in kc if c.get("lod_model_id") is not None]
    lod_ok = sum(1 for c in with_lod if c["lod_ide_file"] in kept)
    atomic_used = sum(r["atomic_slots"] for r in rows if r["recommendation"] == "keep")
    time_used = sum(r["time_slots"] for r in rows if r["recommendation"] == "keep")
    clump_used = sum(r["clump_slots"] for r in rows if r["recommendation"] == "keep")
    dmg_used = sum(r["damage_atomic_slots"] for r in rows if r["recommendation"] == "keep")
    styles = collections.Counter(c["style"] for c in kc)
    summary = dict(
        kept=[r["ide_file"] for r in rows if r["recommendation"] == "keep"],
        city_kept=[r["ide_file"] for r in chosen], atomic_used=atomic_used,
        atomic_free=STORE_CAPACITY["atomic"] - atomic_used, time_used=time_used,
        clump_used=clump_used, damage_used=dmg_used, n_candidates_kept=len(kc),
        kept_styles=styles, lod_ok=lod_ok, lod_total=len(with_lod),
        kept_low_risk=sum(1 for c in kc if c["ground_risk"] == "low"))
    rep(f"recommendation (whole IDE files): keep {len(summary['kept'])} IDEs -> atomic "
        f"{atomic_used}/14000 used, {summary['atomic_free']} free; {len(kc)} building candidates "
        f"available; LOD of {lod_ok}/{len(with_lod)} LOD-using candidates inside kept IDEs")
    return summary


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------
MODEL_COLUMNS = [
    "id", "name", "type", "section", "ide_file", "region", "txd", "txd_parent", "draw_distance",
    "flags", "flag_names", "timed", "time_on", "time_off", "anim",
    "aabb_min_x", "aabb_min_y", "aabb_min_z", "aabb_max_x", "aabb_max_y", "aabb_max_z",
    "sphere_radius", "sphere_x", "sphere_y", "sphere_z", "has_col_bounds",
    "footprint_x", "footprint_y", "height", "z_min",
    "is_lod", "lod_by_name", "lod_model_id", "lod_model_name", "lod_share", "lod_alternatives",
    "lod_same_pivot_share", "lod_exclusive", "lod_reusable", "lod_ide_file", "lod_parent_of",
    "lod_n_children", "lod_target_uses",
    "num_vanilla_placements", "num_placements_text", "num_placements_stream",
    "num_placements_scripted", "placement_region", "placed_in_interior_share", "placement_ipls",
    "companions", "engine_refs",
    "prineside_category", "prineside_title", "ps_width", "ps_length", "ps_height", "ps_radius",
    "emodelid_name_ok", "ide_malformed_line",
]
CANDIDATE_COLUMNS = [
    "id", "name", "style", "use", "floors_est", "footprint_x", "footprint_y", "height", "z_min",
    "footprint_long", "footprint_short", "aspect", "block_like", "size_source", "pivot_height_share",
    "ground_risk", "ground_risk_score", "ground_risk_reasons", "style_reason",
    "region", "ide_file", "txd", "draw_distance", "flags", "timed", "time_on", "time_off",
    "lod_model_id", "lod_model_name", "lod_ide_file", "lod_share", "lod_same_pivot_share",
    "lod_reusable", "companions",
    "num_vanilla_placements", "placement_ipls", "sphere_radius",
    "aabb_min_x", "aabb_min_y", "aabb_min_z", "aabb_max_x", "aabb_max_y", "aabb_max_z",
    "prineside_category", "prineside_title", "preview_url",
]
IDE_COLUMNS = [
    "ide_file", "load_order", "loaded_by", "region", "recommendation", "role_note",
    "n_objs", "n_objs_damageable", "n_tobj", "n_anim", "n_weap", "n_hier", "n_cars", "n_peds",
    "n_txdp", "n_2dfx", "atomic_slots", "damage_atomic_slots", "time_slots", "clump_slots",
    "model_info_slots", "id_min", "id_max", "n_lod_models", "n_placed_models",
    "n_vanilla_placements", "n_building_candidates", "n_candidates_low_ground_risk",
    "candidate_styles", "n_generic_props", "engine_name_lookups", "procobj_dat_models",
    "carmods_dat_models", "object_dat_models", "hardcoded_ids", "critical", "critical_reason",
    "engine_name_models",
]


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        if math.isnan(v):
            return ""
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    return str(v)


def write_csv(path: Path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(columns)
        for r in rows:
            w.writerow([_fmt(r.get(c)) for c in columns])


def engine_refs(fetch: Fetcher, data, rep: Report, hardcoded: dict) -> dict:
    models = data["models"]
    byname = collections.defaultdict(list)
    for m in models.values():
        byname[m["name"].lower()].append(m["id"])
    refs = collections.defaultdict(set)
    mi = parse_model_indices(fetch.github(*MODELINDICES[:2]).decode("utf-8", "replace"))
    found = 0
    for n in set(x.lower() for x in mi):
        for i in byname.get(n, []):
            refs[i].add("MI")
            found += 1
    rep(f"ModelIndices.cpp MatchAllModelStrings: {len(set(x.lower() for x in mi))} names, {found} "
        f"defined in vanilla IDEs (the rest do not exist in SA and stay -1)")
    for fname, names in data_file_model_names(fetch, rep).items():
        for n in names:
            for i in byname.get(n, []):
                refs[i].add(fname)
    for i in hardcoded:
        refs[int(i)].add("hardcoded")
    for m in models.values():
        tags = sorted(refs.get(m["id"], ()))
        if m["id"] in hardcoded:
            tags = [t if t != "hardcoded" else f"hardcoded({hardcoded[m['id']]})" for t in tags]
        m["engine_refs"] = ";".join(tags)
    return refs


def critical_flags(rows):
    for r in rows:
        why = []
        if r["loaded_by"] == "default.dat":
            why.append("loaded by default.dat (not part of the gta.dat choice)")
        if r["hardcoded_ids"]:
            why.append(f"{r['hardcoded_ids']} model id(s) hardcoded in exe code")
        if r["carmods_dat_models"]:
            why.append(f"{r['carmods_dat_models']} carmods.dat models (lookup not null-checked)")
        if r["procobj_dat_models"]:
            why.append(f"{r['procobj_dat_models']} procobj.dat models (missing -> crash on that surface)")
        if r["engine_name_lookups"]:
            why.append(f"{r['engine_name_lookups']} names looked up by the exe (MatchAllModelStrings; "
                       f"a missing name leaves that MI_* index unresolved)")
        if r["object_dat_models"]:
            why.append(f"{r['object_dat_models']} object.dat entries (skipped if missing)")
        hard = r["loaded_by"] == "default.dat" or r["hardcoded_ids"] or r["carmods_dat_models"] or r["procobj_dat_models"]
        r["critical"] = "yes" if hard else ("soft" if r["engine_name_lookups"] else "no")
        r["critical_reason"] = "; ".join(why)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=CACHE_DEFAULT, help="download cache directory")
    ap.add_argument("--offline", action="store_true", help="fail instead of downloading")
    ap.add_argument("--no-prineside", action="store_true", help="skip prineside categories")
    ap.add_argument("--scan-gtarev", action="store_true",
                    help="recompute hardcoded MODEL_* references (needs gh CLI + network)")
    ap.add_argument("--out", default=str(OUT_DIR), help="output directory (data/sa)")
    ap.add_argument("--report", default=None, help="also write the verification log here")
    args = ap.parse_args(argv)
    out = Path(args.out)
    fetch = Fetcher(Path(args.cache), offline=args.offline)
    rep = Report()

    data = build_models(fetch, rep)
    models = data["models"]
    em_n2i = data["name2id"]
    for m in models.values():
        m["emodelid_name_ok"] = em_n2i.get(emodelid_name(m["name"])) == m["id"]
        m["flag_names"] = flag_names(m["flags"])
        m["timed"] = m["section"] == "tobj"
    hardcoded = HARDCODED_OBJECT_MODEL_REFS
    if args.scan_gtarev:
        hardcoded = scan_gtarev_model_refs(fetch, em_n2i, models, rep)
    refs = engine_refs(fetch, data, rep, hardcoded)

    prine, pcounts = ({}, {}) if args.no_prineside else load_prineside(fetch, rep)
    for m in models.values():
        p = prine.get(m["id"])
        if p:
            m["prineside_category"] = ";".join(p["cats"])
            m["prineside_title"] = p.get("title", "")
            m["ps_width"], m["ps_length"] = p.get("width"), p.get("length")
            m["ps_height"], m["ps_radius"] = p.get("height"), p.get("radius")

    cands = build_candidates(models, prine, rep)
    for c in cands:
        c["preview_url"] = f"https://dev.prineside.com/en/gtasa_samp_model_id/model/{c['id']}-{c['name']}/"
    cands.sort(key=lambda c: (STYLE_ORDER.index(c["style"]), {"low": 0, "medium": 1, "high": 2}[c["ground_risk"]],
                              -c["footprint_long"] * c["footprint_short"], c["id"]))

    rows = ide_summary_rows(data, cands, prine, refs)
    critical_flags(rows)
    summary = recommend(rows, cands, data, rep)

    # ---- write ---------------------------------------------------------------------
    out.mkdir(parents=True, exist_ok=True)
    (out / "gta_dat_vanilla.txt").write_bytes(data["gta_raw"])
    write_csv(out / "models.csv", sorted(models.values(), key=lambda r: r["id"]), MODEL_COLUMNS)
    write_csv(out / "ide_summary.csv", rows, IDE_COLUMNS)
    write_csv(out / "building_candidates.csv", cands, CANDIDATE_COLUMNS)

    styles = collections.Counter(c["style"] for c in cands)
    rep(f"wrote {out}/models.csv ({len(models)} rows), ide_summary.csv ({len(rows)} rows), "
        f"building_candidates.csv ({len(cands)} rows), gta_dat_vanilla.txt "
        f"(md5 {hashlib.md5(data['gta_raw']).hexdigest()})")
    rep("candidates per style: " + ", ".join(f"{k} {styles[k]}" for k in STYLE_ORDER))
    rep("kept city IDEs (greedy): " + ", ".join(summary["city_kept"]))
    rep("kept-set candidates per style: " + ", ".join(f"{k} {summary['kept_styles'][k]}" for k in STYLE_ORDER))
    if args.report:
        Path(args.report).write_text("\n".join(rep.lines) + "\n")
    print(f"network downloads this run: {fetch.n_network}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
