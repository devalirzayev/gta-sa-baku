"""Per-tile merged signage models: shop / market boards on building facades (real names from the
Overture places data), blue street-name plates at junction corners and zebra crossings.

One static model per 250 x 250 ground tile that has signage: bk_sgXXYY (TXD bk_sgtXXYY with one
atlas texture).  Draw distance 290 from the tile centre (< 300: stays a normal building, every
point of the tile is within 177 units of the centre).  The model has a bounds-only collision entry
(no volumes): CEntity::GetBoundRect needs a CColModel, the engine then disables collision.

Boards are lit at night (night vertex colours), like real illuminated shop signs.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pyarrow.parquet as pq
import shapely
from PIL import Image, ImageDraw

from tools.build import ground
from tools.build.mesh import MeshBuilder
from tools.geo import profiles
from tools.geo.proj import lonlat_to_game, WORLD
from tools.geo.roadmesh import polyline_normals
from tools.rw import col as COL, dff as DFF, txd as TXD
from tools.tex import signs_tex as ST
from tools.build.signs import _sample

TILE = ground.TILE
NT = ground.NT
SG_BASE = 11682               # free vanilla id range 11682..12799
SG_DD = 290.0
PX = 44                       # board texture pixels per unit
PX_PLATE = 84                 # street-name plates are small: more pixels per unit
ATLAS_W = 1024


def sg_name(ix, iy):
    return f"bk_sg{ix:02d}{iy:02d}"


def sg_txd(ix, iy):
    return f"bk_sgt{ix:02d}{iy:02d}"


def sg_tex(ix, iy):
    return f"bk_sga{ix:02d}{iy:02d}"


def sg_id(ix, iy):
    return SG_BASE + ix * NT + iy


assert sg_id(NT - 1, NT - 1) <= 12799

# ------------------------------------------------------------------------------- categories
# taxonomy.primary -> (board kind, Azerbaijani label or None)
CATS = {
    "restaurant": ("restaurant", "RESTORAN"), "turkish_restaurant": ("restaurant", "TÜRK MƏTBƏXİ"),
    "pizza_restaurant": ("fastfood", "PİZZA"), "sushi_restaurant": ("restaurant", "SUŞİ"),
    "italian_restaurant": ("restaurant", "RESTORAN"), "steakhouse": ("restaurant", "STEYK HAUS"),
    "asian_restaurant": ("restaurant", "RESTORAN"), "eastern_european_restaurant": ("restaurant", "RESTORAN"),
    "theme_restaurant": ("restaurant", "RESTORAN"), "seafood_restaurant": ("restaurant", "BALIQ RESTORANI"),
    "middle_eastern_restaurant": ("restaurant", "RESTORAN"), "kebab_shop": ("fastfood", "KABAB"),
    "cafe": ("cafe", "KAFE"), "coffee_shop": ("cafe", "COFFEE"), "tea_room": ("cafe", "ÇAYXANA"),
    "dessert_shop": ("cafe", "ŞİRNİYYAT"), "candy_store": ("cafe", "ŞİRNİYYAT"), "ice_cream_shop": ("cafe", "DONDURMA"),
    "fast_food_restaurant": ("fastfood", None), "burger_restaurant": ("fastfood", None),
    "bakery": ("bakery", "ÇÖRƏKXANA"),
    "grocery_store": ("market", "MARKET"), "convenience_store": ("market", "MARKET"),
    "supermarket": ("market", "SUPERMARKET"), "warehouse_club_store": ("market", "HİPERMARKET"),
    "butcher_shop": ("market", "ƏT DÜKANI"), "food_and_beverage_store": ("market", "ƏRZAQ"),
    "shopping_mall": ("mall", "TİCARƏT MƏRKƏZİ"), "shopping": ("mall", None),
    "pharmacy": ("pharmacy", "APTEK"), "pharmacy_and_drug_store": ("pharmacy", "APTEK"),
    "bank_or_credit_union": ("bank", None), "bank": ("bank", None),
    "clothing_store": ("fashion", "GEYİM"), "womens_clothing_store": ("fashion", "QADIN GEYİMLƏRİ"),
    "mens_clothing_store": ("fashion", "KİŞİ GEYİMLƏRİ"), "childrens_clothing_store": ("fashion", "UŞAQ GEYİMLƏRİ"),
    "fashion_and_apparel_store": ("fashion", None), "shoe_store": ("fashion", "AYAQQABI"),
    "fashion_accessories_store": ("fashion", "AKSESUAR"), "bridal_shop": ("fashion", "GƏLİNLİK"),
    "jewelry_store": ("jewelry", "ZƏRGƏRLİK"), "flowers_and_gifts_store": ("flowers", "GÜLLƏR"),
    "electronics_store": ("tech", "ELEKTRONİKA"), "computer_store": ("tech", "KOMPÜTER"),
    "mobile_phone_store": ("tech", "MOBİL TELEFONLAR"), "furniture_store": ("home", "MEBEL"),
    "home_goods_store": ("home", "EV ƏŞYALARI"), "linen_store": ("home", "TEKSTİL"), "carpet_store": ("home", "XALÇA"),
    "beauty_salon": ("beauty", "GÖZƏLLİK SALONU"), "barber": ("beauty", "BƏRBƏR"),
    "beauty_supply_store": ("beauty", "KOSMETİKA"), "personal_care_and_beauty_store": ("beauty", "KOSMETİKA"),
    "hotel": ("hotel", None), "bookstore": ("books", "KİTAB"), "auto_parts_store": ("auto", "AVTO EHTİYAT HİSSƏLƏRİ"),
    "gas_station": ("fuel", None), "sporting_goods_store": ("misc", "İDMAN"), "eyewear_store": ("misc", "OPTİKA"),
    "toy_store": ("misc", "OYUNCAQLAR"), "pet_store": ("misc", "ZOO MAĞAZA"), "hardware_store": ("home", "TİKİNTİ MALLARI"),
    "building_supply_store": ("home", "TİKİNTİ MALLARI"), "home_improvement_store": ("home", "TİKİNTİ MALLARI"),
    "arts_crafts_and_hobby_store": ("misc", None), "dental_clinic": ("misc", "STOMATOLOGİYA"),
}

PALETTES = {
    "restaurant": [((120, 20, 25), (250, 228, 170)), ((22, 22, 25), (238, 198, 92)), ((70, 40, 25), (255, 240, 212))],
    "cafe": [((88, 54, 34), (255, 244, 224)), ((244, 238, 226), (84, 52, 30)), ((20, 88, 70), (255, 255, 255))],
    "fastfood": [((214, 30, 30), (255, 214, 0)), ((255, 196, 0), (196, 20, 20))],
    "bakery": [((236, 214, 168), (118, 60, 22)), ((128, 66, 30), (255, 236, 200))],
    "market": [((200, 22, 32), (255, 255, 255)), ((0, 118, 60), (255, 255, 255)), ((255, 206, 0), (176, 18, 18))],
    "mall": [((24, 24, 30), (255, 255, 255)), ((0, 58, 118), (255, 255, 255))],
    "pharmacy": [((0, 138, 70), (255, 255, 255))],
    "bank": [((10, 50, 120), (255, 255, 255)), ((0, 88, 88), (255, 255, 255)), ((146, 12, 32), (255, 255, 255))],
    "fashion": [((14, 14, 14), (255, 255, 255)), ((248, 248, 248), (16, 16, 16)), ((176, 138, 88), (255, 255, 255))],
    "jewelry": [((18, 18, 18), (228, 188, 90))],
    "flowers": [((226, 88, 138), (255, 255, 255)), ((38, 108, 50), (255, 255, 255))],
    "tech": [((0, 88, 178), (255, 255, 255)), ((238, 238, 238), (0, 70, 160))],
    "home": [((88, 58, 40), (255, 255, 255)), ((40, 40, 58), (255, 218, 150))],
    "beauty": [((196, 60, 138), (255, 255, 255)), ((28, 28, 28), (255, 178, 220))],
    "hotel": [((20, 30, 58), (230, 200, 120))],
    "books": [((30, 70, 40), (250, 240, 210))],
    "auto": [((20, 20, 20), (255, 198, 0))],
    "fuel": [((0, 80, 160), (255, 255, 255))],
    "misc": [((40, 60, 90), (255, 255, 255)), ((240, 240, 235), (30, 30, 30))],
}
# local chains and banks: their house colours (names stay plain text, no logos)
BRANDS = [
    ("bravo", (236, 108, 0), (255, 255, 255)), ("araz", (206, 18, 30), (255, 255, 255)),
    ("bazarstore", (222, 26, 36), (255, 255, 255)), ("oba", (0, 146, 70), (255, 255, 255)),
    ("neptun", (0, 98, 178), (255, 255, 255)), ("grandmart", (198, 20, 38), (255, 255, 255)),
    ("al market", (0, 110, 60), (255, 255, 255)), ("kapital", (206, 18, 48), (255, 255, 255)),
    ("abb", (0, 50, 138), (255, 255, 255)), ("pasha", (0, 78, 88), (222, 192, 122)),
    ("rabita", (0, 108, 70), (255, 255, 255)), ("azercell", (98, 40, 148), (255, 255, 255)),
    ("bakcell", (226, 0, 48), (255, 255, 255)), ("nar ", (226, 40, 70), (255, 255, 255)),
    ("azpetrol", (0, 88, 168), (255, 218, 0)), ("socar", (0, 58, 138), (255, 255, 255)),
    ("lukoil", (218, 20, 40), (255, 255, 255)), ("zeytun", (68, 148, 58), (255, 255, 255)),
    ("aloe", (40, 148, 88), (255, 255, 255)), ("turan", (0, 88, 158), (255, 255, 255)),
    ("unibank", (108, 30, 128), (255, 255, 255)), ("xalq", (0, 118, 178), (255, 255, 255)),
    ("respublika", (178, 20, 40), (255, 255, 255)), ("expressbank", (0, 88, 148), (255, 255, 255)),
    ("kristal", (0, 118, 198), (255, 255, 255)), ("afb", (0, 70, 130), (255, 255, 255)),
    ("pizza mizza", (228, 40, 40), (255, 255, 255)), ("bir iki", (230, 30, 30), (255, 255, 255)),
]
FONTS_SANS = ["Inter-ExtraBold.otf", "Inter-Black.otf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
              "Carlito-Bold.ttf", "Inter-Bold.otf"]
FONTS_SERIF = ["DejaVuSerif-Bold.ttf"]
_OK_CHARS = re.compile(r"[^0-9A-Za-zÀ-ɏƏəЀ-ӿ &'\-.,!+/№\"«»()]")


def _h(s):
    return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:8], 16)


def clean_name(s):
    s = _OK_CHARS.sub("", s or "").strip(" -,.")
    s = re.sub(r"\s+", " ", s)
    return s


@dataclass
class Board:
    text: str
    sub: str | None
    kind: str
    bg: tuple
    fg: tuple
    font: str
    P: np.ndarray            # centre (x, y) on the wall plane
    nrm: np.ndarray          # outward unit normal
    w: float
    h: float
    z0: float                # bottom
    plate: bool = False      # street-name plate style
    cross: bool = False      # pharmacy cross
    px: float = 34.0         # texture pixels per unit
    lines: list = None       # direction sign rows [(arrow 'L'|'S'|'R', text)]
    pole_z: float = None     # free-standing board: pole from this ground height (+ back face)
    img: Image.Image = None
    uv: tuple = None


# ------------------------------------------------------------------------------- selection
def select_places():
    rows = pq.read_table("data/raw/place.parquet", columns=["names", "taxonomy", "confidence", "brand", "geometry",
                                                            "operating_status"]).to_pylist()
    out = []
    for r in rows:
        tax = (r["taxonomy"] or {}).get("primary")
        if tax not in CATS:
            continue
        if (r.get("operating_status") or "open") not in ("open", None):
            continue
        brand = ((r["brand"] or {}).get("names") or {}).get("primary")
        conf = r["confidence"] or 0
        if conf < 0.5 and not brand:
            continue
        name = clean_name((r["names"] or {}).get("primary"))
        if len(name) < 2:
            continue
        if len(name) > 30:
            name = name[:30].rsplit(" ", 1)[0]
        g = shapely.from_wkb(r["geometry"])
        if g.geom_type != "Point":
            continue
        x, y = lonlat_to_game(g.x, g.y)
        if abs(x) > WORLD - 6 or abs(y) > WORLD - 6:
            continue
        out.append({"name": name, "tax": tax, "brand": brand or "", "conf": conf, "x": x, "y": y})
    # dedupe identical names within 40 units (chains have several POIs per shop)
    out.sort(key=lambda p: -p["conf"])
    keep = []
    grid = defaultdict(list)
    for p in out:
        k = (int(p["x"] // 40), int(p["y"] // 40))
        dup = False
        for a in (k[0] - 1, k[0], k[0] + 1):
            for b in (k[1] - 1, k[1], k[1] + 1):
                for q in grid[(a, b)]:
                    if q["name"].lower() == p["name"].lower() and (q["x"] - p["x"]) ** 2 + (q["y"] - p["y"]) ** 2 < 1600:
                        dup = True
        if not dup:
            grid[k].append(p)
            keep.append(p)
    return keep


def style_for(p):
    kind, label = CATS[p["tax"]]
    key = (p["brand"] + " " + p["name"]).lower()
    hh = _h(p["name"])
    for sub, bg, fg in BRANDS:
        if sub in key:
            return kind, label if kind in ("pharmacy", "market") else None, bg, fg
    pal = PALETTES.get(kind, PALETTES["misc"])
    bg, fg = pal[hh % len(pal)]
    # avoid repeating the label when the name already says it
    if label and any(w in ST.az_upper(p["name"]) for w in ST.az_upper(label).split()):
        label = None
    return kind, label, bg, fg


# ------------------------------------------------------------------------------- facades
class Facades:
    """Street-facing edges of the placed SA building footprints."""

    def __init__(self, ctx, blds, exclude=None):
        self.ctx = ctx
        self.blds = [b for b in blds if b.get("poly") is not None and not b["poly"].is_empty
                     and (exclude is None or not b["poly"].intersects(exclude))]
        self.polys = [b["poly"] for b in self.blds]
        self.tree = shapely.STRtree(self.polys)
        self.used = defaultdict(list)       # (building index, edge index) -> [(a, b, row)]

    def edges(self, bi):
        poly = self.polys[bi]
        c = np.asarray(poly.exterior.coords)[:-1]
        ccw = shapely.LinearRing(c).is_ccw
        out = []
        for k in range(len(c)):
            a, b = c[k], c[(k + 1) % len(c)]
            d = b - a
            L = float(np.hypot(*d))
            if L < 1.0:
                continue
            t = d / L
            nrm = np.array([t[1], -t[0]]) if ccw else np.array([-t[1], t[0]])   # outward
            out.append((k, a, t, L, nrm))
        return out

    def road_dist(self, P, nrm, max_d=28.0):
        ks = np.arange(1.0, max_d, 1.0)
        pts = P[None, :] + nrm[None, :] * ks[:, None]
        hit = self.ctx.on_road(pts)
        blocked = self.ctx.in_building(pts[:4])
        if blocked.any():
            return None
        return float(ks[np.argmax(hit)]) if hit.any() else None

    def best_edge(self, xy, max_poi_dist=18.0):
        p = shapely.Point(*xy)
        idx = self.tree.query(p, predicate="dwithin", distance=max_poi_dist)
        best = None
        for bi in idx:
            for k, a, t, L, nrm in self.edges(int(bi)):
                s = float(np.clip((np.asarray(xy) - a) @ t, 0, L))
                foot = a + t * s
                dpoi = float(np.hypot(*(np.asarray(xy) - foot)))
                rd = self.road_dist(foot + nrm * 0.3, nrm)
                if rd is None:
                    continue
                score = dpoi + 0.35 * rd
                if best is None or score < best[0]:
                    best = (score, int(bi), k, a, t, L, nrm, s, rd)
        return best

    def reserve(self, bi, k, L, s_want, w, rows=2):
        """Find room for a board of width w near s_want on edge k; returns (s_centre, row) or None."""
        lst = self.used[(bi, k)]
        lo, hi = w / 2 + 0.35, L - w / 2 - 0.35
        if hi < lo:
            return None
        cand = [s_want] + [s_want + d * sg for d in np.arange(0.5, L, 0.5) for sg in (1, -1)]
        for row in range(rows):
            for s in cand:
                s = min(max(s, lo), hi)
                if all(not (r == row and s - w / 2 < b1 + 0.25 and s + w / 2 > b0 - 0.25) for b0, b1, r in lst):
                    lst.append((s - w / 2, s + w / 2, row))
                    return s, row
        return None


def _measure(text, sub, font_name, h):
    """Board width for a given height from the text aspect ratio."""
    f = ST.font(font_name, 100)
    l, t, r, b = f.getbbox(text)
    asp = (r - l) / max(b - t, 1)
    main_h = h * (0.52 if sub else 0.64)
    w = asp * main_h * 0.84 + h * 0.55
    if sub:
        fs = ST.font("Inter-Bold.otf", 100)
        l2, t2, r2, b2 = fs.getbbox(sub)
        w = max(w, (r2 - l2) / max(b2 - t2, 1) * h * 0.2 + h * 0.55)
    return w


def collect_boards(ctx, blds, verbose=True, exclude=None):
    fac = Facades(ctx, blds, exclude)
    places = select_places()
    boards = []
    rng = np.random.default_rng(5)
    for p in places:
        kind, label, bg, fg = style_for(p)
        text = p["name"]
        hh = _h(text)
        if len(text) <= 14 and hh % 3 != 0:
            # Azerbaijani names: İ/I rules; transliterated / foreign names keep the plain I
            text = ST.az_upper(text) if re.search("[əğıöüşçƏĞİÖÜŞÇ]", text) else text.upper()
        fnt = FONTS_SERIF[0] if (kind in ("restaurant", "hotel", "jewelry") and hh % 2 == 0) else FONTS_SANS[hh % len(FONTS_SANS)]
        big = kind in ("mall", "hotel") or (kind == "market" and p["brand"])
        h = 1.35 if big else (1.0 if label else 0.85)
        w = float(np.clip(_measure(text, label, fnt, h), 1.6, 12.0 if big else 8.0))
        be = fac.best_edge((p["x"], p["y"]))
        if be is None:
            continue
        _, bi, k, a, t, L, nrm, s, rd = be
        if L < 2.2:
            continue
        if w > L - 0.7:
            w = L - 0.7
            if w < 1.5:
                continue
        b = fac.blds[bi]
        crown = kind in ("hotel", "mall") and b["height"] >= 16 and L >= 8
        if crown:
            # crown sign near the roof line (hotels, malls)
            if (bi, k, "crown") in fac.used:
                continue
            fac.used[(bi, k, "crown")] = [(0, 0, 0)]
            hc = 1.8 if b["height"] < 40 else 2.6
            wc = float(np.clip(_measure(text, None, fnt, hc), 3.0, L - 1.0))
            P = a + t * (L / 2) + nrm * 0.12
            zb = float(b["z"]) + b["height"] - hc - 1.2
            boards.append(Board(text, None, kind, bg, fg, fnt, P, nrm, wc, hc, zb, px=26.0))
        res = fac.reserve(bi, k, L, s, w)
        if res is None:
            continue
        sc, row = res
        P = a + t * sc + nrm * 0.12
        zg = ctx.ground_z(float(P[0]), float(P[1]))
        z0 = zg + 2.95 + row * (h + 0.3)
        boards.append(Board(text, label, kind, bg, fg, fnt, P, nrm, w, h, z0, cross=(kind == "pharmacy")))
    if verbose:
        print(f"shop boards: {len(boards)} from {len(places)} places")
    return boards


def short_street(name):
    return name


def collect_plates(ctx, blds, verbose=True, exclude=None):
    """Blue street-name plates on the corner buildings of junctions (one per street and corner)."""
    W = ctx.W
    fac = Facades(ctx, blds, exclude)
    lay = W.lay
    plates = []
    last = defaultdict(list)
    for j in lay.junctions.values():
        if len(j.arms) < 3:
            continue
        jc = np.array(j.polygon.centroid.coords[0])
        named = []
        for eid, end in j.arms:
            r = lay.ribbons.get(eid)
            e = r.edge if r is not None else W.g.edges.get(eid)
            if e is None or not e.name or r is None:
                continue
            nm = clean_name(e.name)
            if len(nm) < 3 or any(nm == n for n, *_ in named):
                continue
            c = r.center[-1] if end == "v" else r.center[0]
            t = (r.center[-1] - r.center[-2]) if end == "v" else (r.center[0] - r.center[1])
            t = t / max(np.hypot(*t), 1e-9)
            named.append((nm, c, t, r))
        for nm, c, t, r in named[:3]:
            if any(np.hypot(*(q - jc)) < 120 for q in last[nm]):
                continue
            # corner building edge parallel to the street, close to the junction
            idx = fac.tree.query(shapely.Point(*c), predicate="dwithin", distance=22.0)
            best = None
            for bi in idx:
                for k, a, et, L, nrm in fac.edges(int(bi)):
                    if abs(float(et @ t)) < 0.88 or L < 3.0:
                        continue
                    # the edge must face the street centre line
                    mid = a + et * L / 2
                    if float((c - mid) @ nrm) <= 0:
                        continue
                    ends = [a + et * 1.2, a + et * (L - 1.2)]
                    q = min(ends, key=lambda p: np.hypot(*(p - jc)))
                    dj = float(np.hypot(*(q - jc)))
                    if dj > 32:
                        continue
                    if best is None or dj < best[0]:
                        best = (dj, int(bi), k, a, et, L, nrm, q)
            if best is None:
                continue
            _, bi, k, a, et, L, nrm, q = best
            s = float((q - a) @ et)
            w = float(np.clip(_measure(nm, None, "Inter-Bold.otf", 0.42) + 0.1, 1.2, 3.8))
            res = fac.reserve(bi, k, L, s, w, rows=1)
            if res is None:
                continue
            P = a + et * res[0] + nrm * 0.1
            zg = ctx.ground_z(float(P[0]), float(P[1]))
            plates.append(Board(nm, None, "plate", (18, 54, 130), (255, 255, 255), "Inter-Bold.otf", P, nrm, w, 0.42,
                                zg + 2.5, plate=True))
            last[nm].append(jc)
    if verbose:
        print(f"street-name plates: {len(plates)}")
    return plates


def collect_zebras(ctx, lights, crossings):
    """Stripe quads (4 x 3 corners) for OSM crossings on major roads and signalised junction mouths."""
    W = ctx.W
    lay = W.lay
    out = []
    items = list(crossings)
    sig_nodes = set()
    from scipy.spatial import cKDTree
    nid = list(W.g.nodes.keys())
    tree = cKDTree(np.array([W.g.nodes[n] for n in nid]))
    for L in lights:
        dd, k = tree.query([L["jx"], L["jy"]])
        if dd < 1.0:
            sig_nodes.add(nid[k])
    for r in lay.ribbons.values():
        e = r.edge
        Lr = float(profiles.arclen(r.center)[-1])
        if Lr < 8:
            continue
        if e.v in sig_nodes:
            items.append((r, Lr - 2.6))
        if e.u in sig_nodes:
            items.append((r, 2.6))
    for r, s in items:
        e = r.edge
        s_full = profiles.arclen(e.pts)
        if abs(float(np.interp(r.s0 + s, s_full, e.lift))) > 0.3:
            continue
        z = float(np.interp(r.s0 + s, s_full, e.z)) + 0.04
        (c,), (t,) = _sample(r.center, [s])
        n = np.array([-t[1], t[0]])
        for o in np.arange(-r.right + 0.55, r.left - 0.35, 1.1):
            q = [c - t * 1.4 + n * (o - 0.3), c + t * 1.4 + n * (o - 0.3), c + t * 1.4 + n * (o + 0.3), c - t * 1.4 + n * (o + 0.3)]
            out.append((np.array(q), z))
    return out


SHORT = [("küçəsi", "küç."), ("prospekti", "pr."), ("şossesi", "şos."), ("meydanı", "meyd."), ("döngəsi", "dön."),
         ("bulvarı", "bulv.")]
CLASS_RANK = {"motorway": 5, "trunk": 5, "primary": 4, "secondary": 3, "tertiary": 2}


def short_name(n):
    n = clean_name(n)
    for a, b in SHORT:
        n = re.sub(a + r"\b", b, n)
    return n


def collect_direction_signs(ctx, lights, verbose=True):
    """Blue direction signs (arrows + the names of the roads leaving the junction) on the
    approaches of signalised junctions of main roads, 26 units before the junction."""
    from scipy.spatial import cKDTree
    W = ctx.W
    lay = W.lay
    g = W.g
    nid = list(g.nodes.keys())
    tree = cKDTree(np.array([g.nodes[n] for n in nid]))
    out = []
    done = set()
    for L in lights:
        J = np.array([L["jx"], L["jy"]])
        O = np.array([L["ox"], L["oy"]])
        d = J - O
        d = d / max(np.hypot(*d), 1e-9)
        dist, k = tree.query(J)
        if dist > 1.0:
            continue
        node = nid[k]
        jid = lay.node_junction.get(node)
        if jid is None:
            continue
        key = (jid, int(round(math.degrees(math.atan2(d[1], d[0])) / 20.0)))
        if key in done:
            continue
        j = lay.junctions[jid]
        # approach arm: the ribbon arriving at this node against d
        app = None
        for eid, end in j.arms:
            r = lay.ribbons.get(eid)
            if r is None:
                continue
            e = r.edge
            if (e.v if end == "v" else e.u) != node:
                continue
            c = r.center
            t = (c[-1] - c[-2]) if end == "v" else (c[0] - c[1])
            t = t / max(np.hypot(*t), 1e-9)
            if float(t @ d) > 0.8:
                app = (r, end, t)
                break
        if app is None:
            continue
        r, end, t = app
        if r.edge.cls not in ("trunk", "primary", "secondary"):
            continue
        Lr = float(profiles.arclen(r.center)[-1])
        if Lr < 45:
            continue
        exits = {}
        for eid, e_end in j.arms:
            rr = lay.ribbons.get(eid)
            e = rr.edge if rr is not None else g.edges.get(eid)
            if e is None or not e.name:
                continue
            if e.oneway and e_end == "v":
                continue                             # traffic comes in here, cannot leave this way
            pts = e.pts if e_end == "u" else e.pts[::-1]
            a = pts[min(len(pts) - 1, 4)] - pts[0]
            a = a / max(np.hypot(*a), 1e-9)
            if float(a @ -d) > 0.75:
                continue                             # back where we came from
            th = math.atan2(d[0] * a[1] - d[1] * a[0], float(d @ a))
            k2 = "S" if abs(th) < 0.6 else ("L" if th > 0 else "R")
            score = CLASS_RANK.get(e.cls, 1) + 0.001 * e.length
            if k2 not in exits or score > exits[k2][0]:
                exits[k2] = (score, short_name(e.name))
        rows = [(k2, exits[k2][1]) for k2 in ("L", "S", "R") if k2 in exits]
        if len(rows) < 2 or len({n for _, n in rows}) < 2:
            continue
        # position: right verge, 26 units before the end of the approach ribbon
        s_end = Lr if end == "v" else 0.0
        s_at = s_end - 26.0 if end == "v" else 26.0
        (pc,), (tt,) = _sample(r.center, [s_at])
        nrm = np.array([-tt[1], tt[0]])
        right = -nrm if end == "v" else nrm
        half = r.right if end == "v" else r.left
        placed = None
        for extra in (1.1, 1.8, 2.6):
            P = pc + right * (half + extra)
            if not ctx.on_road(P[None])[0] and not ctx.in_building(P[None], 1.6)[0] and not ctx.in_water(P[None])[0]:
                placed = P
                break
        if placed is None:
            continue
        done.add(key)
        zg = ctx.ground_z(float(placed[0]), float(placed[1]))
        h = 0.36 * len(rows) + 0.3
        out.append(Board(" / ".join(n for _, n in rows), None, "direction", (16, 72, 160), (255, 255, 255),
                         "Inter-Bold.otf", placed, -d, 3.9, h, zg + 2.35, px=64.0, lines=rows, pole_z=zg - 0.2))
    if verbose:
        print(f"direction signs: {len(out)}")
    return out


# ------------------------------------------------------------------------------- rendering
def _render_direction(b: Board, wpx, hpx):
    sc = 3
    W_, H_ = wpx * sc, hpx * sc
    im = Image.new("RGB", (W_, H_), b.bg)
    d = ImageDraw.Draw(im)
    d.rectangle([4 * sc, 4 * sc, W_ - 4 * sc - 1, H_ - 4 * sc - 1], outline=(255, 255, 255), width=2 * sc)
    n = len(b.lines)
    row_h = (H_ - 12 * sc) / n
    for i, (arrow, text) in enumerate(b.lines):
        cy = 6 * sc + row_h * (i + 0.5)
        ax = 10 * sc + row_h * 0.45
        s_ = row_h * 0.34
        if arrow == "S":
            d.polygon([(ax, cy - s_), (ax - s_ * 0.8, cy), (ax + s_ * 0.8, cy)], fill=(255, 255, 255))
            d.rectangle([ax - s_ * 0.22, cy, ax + s_ * 0.22, cy + s_], fill=(255, 255, 255))
        else:
            sg = -1 if arrow == "L" else 1
            d.polygon([(ax + sg * s_, cy), (ax, cy - s_ * 0.8), (ax, cy + s_ * 0.8)], fill=(255, 255, 255))
            d.rectangle([min(ax, ax - sg * s_), cy - s_ * 0.22, max(ax, ax - sg * s_), cy + s_ * 0.22], fill=(255, 255, 255))
        tx = ax + row_h * 0.75
        size = int(row_h * 0.62)
        while size > 8:
            f = ST.font(b.font, size)
            l, t, r_, bt = d.textbbox((0, 0), text, font=f)
            if r_ - l <= W_ - tx - 8 * sc:
                break
            size = int(size * 0.92)
        f = ST.font(b.font, max(size, 8))
        l, t, r_, bt = d.textbbox((0, 0), text, font=f)
        d.text((tx - l, cy - (t + bt) / 2), text, font=f, fill=(255, 255, 255))
    return im.resize((wpx, hpx), Image.LANCZOS)


def render_board(b: Board):
    px = PX_PLATE if b.plate else b.px
    wpx = max(24, int(round(b.w * px)))
    hpx = max(14, int(round(b.h * px)))
    if b.lines:
        return _render_direction(b, wpx, hpx)
    sc = 3
    im = Image.new("RGB", (wpx * sc, hpx * sc), b.bg)
    d = ImageDraw.Draw(im)
    W_, H_ = wpx * sc, hpx * sc
    if b.plate:
        d.rectangle([3 * sc, 3 * sc, W_ - 3 * sc - 1, H_ - 3 * sc - 1], outline=(255, 255, 255), width=2 * sc)
    else:
        edge = tuple(int(c * 0.75) for c in b.bg)
        d.rectangle([0, 0, W_ - 1, H_ - 1], outline=edge, width=max(2, H_ // 18))
    x0 = 0
    if b.cross:
        cs = H_ * 0.62
        cx, cy = H_ * 0.55, H_ / 2
        d.rectangle([cx - cs / 2, cy - cs / 6, cx + cs / 2, cy + cs / 6], fill=(255, 255, 255))
        d.rectangle([cx - cs / 6, cy - cs / 2, cx + cs / 6, cy + cs / 2], fill=(255, 255, 255))
        x0 = H_ * 1.05
    avail_w = (W_ - x0) * 0.9
    main_h = H_ * (0.5 if b.sub else 0.62)
    size = int(main_h * 1.25)
    f = ST.font(b.font, max(8, size))
    while size > 8:
        f = ST.font(b.font, size)
        l, t, r, bt = d.textbbox((0, 0), b.text, font=f)
        if r - l <= avail_w and bt - t <= main_h * 1.1:
            break
        size = int(size * 0.92)
    l, t, r, bt = d.textbbox((0, 0), b.text, font=f)
    cx = x0 + (W_ - x0) / 2
    cy = H_ * (0.40 if b.sub else 0.5)
    d.text((cx - (l + r) / 2, cy - (t + bt) / 2), b.text, font=f, fill=b.fg)
    if b.sub:
        ss = int(H_ * 0.24)
        fs = ST.font("Inter-Bold.otf", max(8, ss))
        while ss > 8:
            fs = ST.font("Inter-Bold.otf", ss)
            l, t, r, bt = d.textbbox((0, 0), b.sub, font=fs)
            if r - l <= avail_w:
                break
            ss = int(ss * 0.9)
        l, t, r, bt = d.textbbox((0, 0), b.sub, font=fs)
        d.text((cx - (l + r) / 2, H_ * 0.79 - (t + bt) / 2), b.sub, font=fs, fill=b.fg)
    return im.resize((wpx, hpx), Image.LANCZOS)


def _shelf(items, width, pad, reserve):
    x, y, shelf = reserve, 0, 34
    pos = []
    for b in items:
        w, h = b.img.size
        if w + pad > width:
            return None
        if x + w + pad > width:
            x, y = 0, y + shelf
            shelf = 0
        pos.append((b, x, y))
        x += w + pad
        shelf = max(shelf, h + pad)
    return pos, y + shelf


ATLAS_MAX = 1024        # px; a 1024 x 1024 DXT1 atlas with mip maps is 683 KB of streaming memory


def pack_atlas(boards):
    """Shelf packing into the smallest power-of-two atlas (at most ATLAS_MAX square: boards of
    dense tiles are scaled down until they fit); returns (atlas, white uv, dark uv).
    Each board gets b.uv = (u0, v0, u1, v1).  Reserved: white block (zebras) and dark block
    (frames) at the top-left."""
    PAD = 3
    best = None
    while best is None:
        items = sorted(boards, key=lambda b: -b.img.size[1])
        for width in (128, 256, 512, 1024, 2048):
            if width > ATLAS_MAX:
                break
            r = _shelf(items, width, PAD, 68)
            if r is None:
                continue
            pos, used = r
            Hp = 64
            while Hp < used:
                Hp *= 2
            if Hp > ATLAS_MAX:
                continue
            key = (width * Hp, abs(math.log2(width / Hp)))
            if best is None or key < best[0]:
                best = (key, width, Hp, pos)
        if best is None:
            for b in boards:
                w, h = b.img.size
                b.img = b.img.resize((max(8, int(w * 0.85)), max(8, int(h * 0.85))), Image.LANCZOS)
    _, AW, Hp, placed = best
    atlas = Image.new("RGB", (AW, Hp), (40, 40, 44))
    atlas.paste((246, 246, 240), (1, 1, 32, 32))
    atlas.paste((52, 54, 58), (35, 1, 66, 32))
    for b, x, y in placed:
        w, h = b.img.size
        ext = b.img.resize((w + 2, h + 2), Image.NEAREST)
        atlas.paste(ext, (max(0, x - 1), max(0, y - 1)))
        atlas.paste(b.img, (x, y))
        b.uv = ((x + 0.5) / AW, (y + 0.5) / Hp, (x + w - 0.5) / AW, (y + h - 0.5) / Hp)
    white = (16 / AW, 16 / Hp)
    dark = (50 / AW, 16 / Hp)
    return atlas, white, dark


def tile_mesh(ix, iy, boards, zebras, atlas_name, white, dark, boxes=None):
    mb = MeshBuilder()
    for b in boards:
        t = np.array([-b.nrm[1], b.nrm[0]])           # along the wall
        n = b.nrm
        P = b.P
        dep = 0.05 if b.plate else 0.16
        hw = b.w / 2
        # front face (outward normal n): corners (left, right as seen from outside)
        Lft = P - t * hw
        Rgt = P + t * hw
        fz0, fz1 = b.z0, b.z0 + b.h
        F = np.array([[*(Lft + n * dep), fz0], [*(Rgt + n * dep), fz0], [*(Rgt + n * dep), fz1], [*(Lft + n * dep), fz1]])
        u0, v0, u1, v1 = b.uv
        # seen from outside, image left is at the wall's -t side when we look along -n
        UV = np.array([[u0, v1], [u1, v1], [u1, v0], [u0, v0]])
        Tq = np.array([[0, 1, 2], [0, 2, 3]])
        mb.add(atlas_name, F, UV, _orient(F, Tq, np.r_[n, 0]), shade=1.25, night=3.2, collide=False)
        # frame: sides and top/bottom (dark)
        B = F.copy()
        B[:, :2] -= n * dep
        quads = [(F[0], F[1], B[1], B[0]), (F[3], F[2], B[2], B[3]), (F[0], F[3], B[3], B[0]), (F[1], F[2], B[2], B[1])]
        for q in quads:
            V = np.array(q)
            cen = V.mean(0)
            out = cen - np.r_[P + n * dep / 2, (fz0 + fz1) / 2]
            mb.add(atlas_name, V, np.tile(dark, (4, 1)), _orient(V, Tq, out), shade=0.8, night=0.6, collide=False)
        if b.pole_z is not None:
            # free-standing sign: back face and a pole (with collision) behind the board
            Bk = B[[1, 0, 3, 2]]
            mb.add(atlas_name, Bk, np.tile(dark, (4, 1)), _orient(Bk, Tq, np.r_[-n, 0]), shade=0.8, night=0.5, collide=False)
            r = 0.06
            pc = P - n * 0.08
            for k in range(4):
                a0 = np.array([[1, 1], [-1, 1], [-1, -1], [1, -1]][k]) * r
                a1 = np.array([[1, 1], [-1, 1], [-1, -1], [1, -1]][(k + 1) % 4]) * r
                Vp = np.array([[*(pc + a0), b.pole_z], [*(pc + a1), b.pole_z], [*(pc + a1), fz1], [*(pc + a0), fz1]])
                mb.add(atlas_name, Vp, np.tile(dark, (4, 1)), _orient(Vp, Tq, np.r_[(a0 + a1) / 2, 0]), shade=0.9,
                       night=0.5, collide=False)
            if boxes is not None:
                boxes.append(((pc[0] - 0.08, pc[1] - 0.08, b.pole_z), (pc[0] + 0.08, pc[1] + 0.08, fz1)))
    for q, z in zebras:
        V = np.c_[q, np.full(4, z)]
        Tq = np.array([[0, 1, 2], [0, 2, 3]])
        mb.add(atlas_name, V, np.tile(white, (4, 1)), _orient(V, Tq, (0, 0, 1)), shade=1.12, night=0.95, collide=False)
    return mb


def _orient(V, T, normal):
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    nrm = np.cross(b - a, c - a)
    flip = (nrm @ np.asarray(normal, float)) < 0
    T = T.copy()
    T[flip] = T[flip][:, [0, 2, 1]]
    return T


def build_tiles(ctx, blds, lights, crossings, verbose=True, exclude=None):
    """-> list of dicts(name, id, txd, ix, iy, origin, dff, col, txd_bytes, n_boards).
    exclude: area whose buildings get no boards (landmark footprints: their SA stand-ins are
    replaced by custom models)."""
    boards = collect_boards(ctx, blds, verbose, exclude) + collect_plates(ctx, blds, verbose, exclude) + \
        collect_direction_signs(ctx, lights, verbose)
    zebras = collect_zebras(ctx, lights, crossings)
    per = defaultdict(lambda: ([], []))
    for b in boards:
        ix = int((b.P[0] + WORLD) // TILE)
        iy = int((b.P[1] + WORLD) // TILE)
        if 0 <= ix < NT and 0 <= iy < NT:
            per[(ix, iy)][0].append(b)
    for q, z in zebras:
        c = q.mean(0)
        ix = int((c[0] + WORLD) // TILE)
        iy = int((c[1] + WORLD) // TILE)
        if 0 <= ix < NT and 0 <= iy < NT:
            per[(ix, iy)][1].append((q, z))
    out = []
    total_px = 0
    for (ix, iy), (bs, zs) in sorted(per.items()):
        for b in bs:
            b.img = render_board(b)
        atlas, white, dark = pack_atlas(bs)
        total_px += atlas.size[0] * atlas.size[1]
        name, tname, aname = sg_name(ix, iy), sg_txd(ix, iy), sg_tex(ix, iy)
        boxes = []
        mb = tile_mesh(ix, iy, bs, zs, aname, white, dark, boxes)
        if mb.empty():
            continue
        x0 = -WORLD + ix * TILE
        y0 = -WORLD + iy * TILE
        allz = np.concatenate([p[0][:, 2] for parts in mb.parts.values() for p in parts])
        origin = (x0 + TILE / 2, y0 + TILE / 2, float(np.round(np.median(allz))))
        geo, _ = mb.build(origin)
        V = np.asarray(geo.vertices, float)
        data = DFF.write_dff(name, geo)
        o = np.asarray(origin)
        cm = COL.ColModel(name, sg_id(ix, iy), bounds=(V.min(0), V.max(0)),
                          boxes=[(np.asarray(b0) - o, np.asarray(b1) - o, COL.SURF["LAMP_POST"]) for b0, b1 in boxes])
        tx = TXD.write_txd([TXD.texture_native(aname, atlas, encoding="DXT1")])
        out.append({"name": name, "id": sg_id(ix, iy), "txd": tname, "ix": ix, "iy": iy, "origin": origin,
                    "dff": data, "col": COL.write_col3(cm), "txd_bytes": tx, "n_boards": len(bs), "n_zebra": len(zs),
                    "atlas": atlas})
    if verbose:
        print(f"sign tiles: {len(out)}, boards {sum(t['n_boards'] for t in out)}, zebra stripes "
              f"{sum(t['n_zebra'] for t in out)}, atlas pixels {total_px / 1e6:.1f} M")
    return out
