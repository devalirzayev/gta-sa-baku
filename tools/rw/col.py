"""COL3 collision writer/reader for GTA San Andreas (PC).

Layout (ColHelpers.h, CFileLoader::LoadCollisionModelVer3):
  FileHeader 32 B: char fourcc[4]="COL3", uint32 size (= total - 8), char name[22], uint16 modelId
  V3 header 88 B : TBounds{min,max,center,radius} 40 B; uint16 nSpheres,nBoxes,nFaces; uint8 nLines;
                   uint8 pad; uint32 flags (2 = not empty, 8 = face groups, 16 = shadow mesh);
                   uint32 offSpheres,offBoxes,offLines,offVerts,offFaces,offPlanes;
                   uint32 nShadowFaces, offShadowVerts, offShadowFaces
  Offsets are relative to (start of model + 4); 0 means "none".
  Vertices are int16[3] fixed point (/128) -> |coord| < 256.
  Face groups (when flag 8): TFaceGroup{min,max,uint16 first,last(inclusive)}[], uint32 count,
  placed immediately before the faces array.
  Face: uint16 a,b,c; uint8 material (eSurfaceType); uint8 light (low nibble day, high nibble night).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

# eSurfaceType subset (Enums/eSurfaceType.h)
SURF = dict(DEFAULT=0, TARMAC=1, TARMAC_FUCKED=2, TARMAC_REALLYFUCKED=3, PAVEMENT=4,
            PAVEMENT_FUCKED=5, GRAVEL=6, FUCKED_CONCRETE=7, PAINTED_GROUND=8,
            GRASS_SHORT_LUSH=9, GRASS_MEDIUM_LUSH=10, GRASS_LONG_LUSH=11, GRASS_SHORT_DRY=12,
            GRASS_MEDIUM_DRY=13, GRASS_LONG_DRY=14, GOLFGRASS_ROUGH=15, GOLFGRASS_SMOOTH=16,
            STEEP_SLIDYGRASS=17, STEEP_CLIFF=18, FLOWERBED=19, MEADOW=20, WASTEGROUND=21,
            WOODLANDGROUND=22, VEGETATION=23, MUD_WET=24, MUD_DRY=25, DIRT=26, DIRTTRACK=27,
            SAND_DEEP=28, SAND_MEDIUM=29, SAND_COMPACT=30, SAND_ARID=31, SAND_MORE=32,
            SAND_BEACH=33, CONCRETE_BEACH=34, ROCK_DRY=35, ROCK_WET=36, ROCK_CLIFF=37,
            WATER_RIVERBED=38, WATER_SHALLOW=39, HEDGE=41, WOOD_SOLID=43, GLASS=45,
            THICK_METAL_PLATE=51, LAMP_POST=53, METAL_GATE=54, METAL_CHAIN_FENCE=55, GIRDER=56,
            P_SAND=74, P_SANDBEACH=79, P_GRASS_SHORT=80, P_GRASS_MEADOW=81, P_GRASS_DRY=82,
            P_WOODLAND=83, P_ROADSIDE=85, P_FLOWERBED=87, P_WASTEGROUND=88, P_CONCRETE=89,
            P_UNDERWATERBARREN=97, P_RUBBLE=101, P_BUSHY=111, P_GRASSLIGHT=146,
            P_GRASSMID1=149, P_GRASSDIRTMIX=153, P_INDUSTRIAL=136, P_CONCRETELITTER=138,
            P_BUILDINGSITE=134, P_DOCKLANDS=135, PARKGRASS=160, STAIRSSTONE=161,
            FLOORCONCRETE=165, RAILTRACK=178)

MAX_FACES_PER_GROUP = 50


def light_byte(day: int, night: int) -> int:
    return (max(0, min(15, day)) & 0xF) | ((max(0, min(15, night)) & 0xF) << 4)


@dataclass
class ColModel:
    name: str
    model_id: int
    vertices: np.ndarray = None          # (N,3) float, relative to model origin
    faces: np.ndarray = None             # (M,3) int
    materials: np.ndarray = None         # (M,) uint8
    lights: np.ndarray = None            # (M,) uint8
    boxes: list = field(default_factory=list)    # [(min3, max3, material)]
    spheres: list = field(default_factory=list)  # [(center3, radius, material)]
    shadow_vertices: np.ndarray = None
    shadow_faces: np.ndarray = None


def _quantize(v: np.ndarray) -> np.ndarray:
    q = np.rint(np.asarray(v, dtype=np.float64) * 128.0)
    if q.size and (q.min() < -32768 or q.max() > 32767):
        raise ValueError("collision vertex outside +-256 units")
    return q.astype("<i2")


def _face_groups(verts_f: np.ndarray, faces: np.ndarray):
    """Spatially order faces into groups of <= 50 (median split on centroids).
    Returns (order, groups[(first,last)])."""
    cent = verts_f[faces].mean(axis=1)
    groups = []
    order = []

    def rec(idx):
        if len(idx) <= MAX_FACES_PER_GROUP:
            first = len(order)
            order.extend(idx.tolist())
            groups.append((first, len(order) - 1))
            return
        c = cent[idx]
        ext = c.max(axis=0) - c.min(axis=0)
        ax = int(np.argmax(ext[:2])) if ext[:2].max() > 0 else 2
        srt = idx[np.argsort(c[:, ax], kind="stable")]
        mid = len(srt) // 2
        rec(srt[:mid])
        rec(srt[mid:])

    rec(np.arange(len(faces)))
    return np.array(order, dtype=np.int64), groups


def write_col3(m: ColModel) -> bytes:
    name = m.name.encode("ascii")
    assert 0 < len(name) <= 21, m.name
    has_mesh = m.faces is not None and len(m.faces) > 0
    verts_q = _quantize(m.vertices) if has_mesh else np.zeros((0, 3), "<i2")
    verts_f = verts_q.astype(np.float64) / 128.0
    faces = np.asarray(m.faces, dtype=np.int64) if has_mesh else np.zeros((0, 3), np.int64)
    mats = np.asarray(m.materials, dtype=np.uint8) if has_mesh else np.zeros(0, np.uint8)
    lights = np.asarray(m.lights, dtype=np.uint8) if has_mesh else np.zeros(0, np.uint8)
    if has_mesh:
        assert len(verts_q) <= 65535 and len(faces) <= 32767, (len(verts_q), len(faces))
        assert faces.max() < len(verts_q)
        # drop degenerate faces (zero area after quantisation)
        a, b, c = verts_f[faces[:, 0]], verts_f[faces[:, 1]], verts_f[faces[:, 2]]
        area = np.linalg.norm(np.cross(b - a, c - a), axis=1)
        keep = area > 1e-6
        faces, mats, lights = faces[keep], mats[keep], lights[keep]
        has_mesh = len(faces) > 0
    groups = []
    if has_mesh and len(faces) > 80:
        order, groups = _face_groups(verts_f, faces)
        faces, mats, lights = faces[order], mats[order], lights[order]
        assert len(groups) <= 1200, f"{m.name}: too many face groups ({len(groups)})"

    # bounds
    pts = [verts_f] if has_mesh else []
    for bmin, bmax, _ in m.boxes:
        pts.append(np.array([bmin, bmax], dtype=np.float64))
    for c, r, _ in m.spheres:
        c = np.asarray(c, dtype=np.float64)
        pts.append(np.array([c - r, c + r]))
    shadow = m.shadow_faces is not None and len(m.shadow_faces) > 0
    if shadow:
        sv_q = _quantize(m.shadow_vertices)
        pts.append(sv_q.astype(np.float64) / 128.0)
    if pts:
        allp = np.concatenate(pts)
        bmin, bmax = allp.min(axis=0), allp.max(axis=0)
        center = (bmin + bmax) / 2
        radius = float(np.sqrt(((allp - center) ** 2).sum(axis=1)).max()) + 0.01
    else:
        bmin = bmax = center = np.zeros(3)
        radius = 0.0

    body = bytearray()
    base = 120 - 4  # offsets are relative to start+4

    def off():
        return base + len(body)

    off_spheres = off_boxes = off_verts = off_faces = 0
    if m.spheres:
        off_spheres = off()
        for c, r, mat in m.spheres:
            body += struct.pack("<4f4B", *map(float, c), float(r), mat, 0, 0, light_byte(15, 8))
    if m.boxes:
        off_boxes = off()
        for b0, b1, mat in m.boxes:
            body += struct.pack("<6f4B", *map(float, b0), *map(float, b1), mat, 0, 0, light_byte(15, 8))
    if has_mesh:
        off_verts = off()
        body += verts_q.tobytes()
        if len(body) % 4:
            body += b"\0" * (4 - len(body) % 4)
        if groups:
            for first, last in groups:
                fv = verts_f[faces[first:last + 1].reshape(-1)]
                gmin, gmax = fv.min(axis=0), fv.max(axis=0)
                body += struct.pack("<6fHH", *map(float, gmin), *map(float, gmax), first, last)
            body += struct.pack("<I", len(groups))
        off_faces = off()
        fa = np.empty(len(faces), dtype=[("a", "<u2"), ("b", "<u2"), ("c", "<u2"), ("m", "u1"), ("l", "u1")])
        fa["a"], fa["b"], fa["c"] = faces[:, 0], faces[:, 1], faces[:, 2]
        fa["m"], fa["l"] = mats, lights
        body += fa.tobytes()
    off_sv = off_sf = 0
    n_sf = 0
    if shadow:
        if len(body) % 4:
            body += b"\0" * (4 - len(body) % 4)
        off_sv = off()
        body += sv_q.tobytes()
        if len(body) % 4:
            body += b"\0" * (4 - len(body) % 4)
        off_sf = off()
        sf = np.asarray(m.shadow_faces, dtype=np.int64)
        fa = np.empty(len(sf), dtype=[("a", "<u2"), ("b", "<u2"), ("c", "<u2"), ("m", "u1"), ("l", "u1")])
        fa["a"], fa["b"], fa["c"] = sf[:, 0], sf[:, 1], sf[:, 2]
        fa["m"], fa["l"] = 0, light_byte(15, 8)
        body += fa.tobytes()
        n_sf = len(sf)

    flags = 0
    if has_mesh or m.boxes or m.spheres:
        flags |= 2
    if groups:
        flags |= 8
    if shadow:
        flags |= 16
    hdr = struct.pack("<6f4f", *bmin, *bmax, *center, radius)
    hdr += struct.pack("<HHHBBI", len(m.spheres), len(m.boxes), len(faces) if has_mesh else 0, 0, 0, flags)
    hdr += struct.pack("<6I", off_spheres, off_boxes, 0, off_verts, off_faces, 0)
    hdr += struct.pack("<3I", n_sf, off_sv, off_sf)
    assert len(hdr) == 88
    total = 32 + len(hdr) + len(body)
    fh = b"COL3" + struct.pack("<I", total - 8) + name.ljust(22, b"\0") + struct.pack("<H", m.model_id & 0xFFFF)
    return fh + hdr + bytes(body)


def read_col(data: bytes) -> list:
    """Parse a .col file (sequence of COL2/COL3 models). Returns list of dicts."""
    out = []
    o = 0
    while o + 8 <= len(data):
        fourcc = data[o:o + 4]
        if fourcc not in (b"COL2", b"COL3"):
            break
        size = struct.unpack_from("<I", data, o + 4)[0]
        name = data[o + 8:o + 30].split(b"\0")[0].decode("ascii")
        mid = struct.unpack_from("<H", data, o + 30)[0]
        h = o + 32
        vals = struct.unpack_from("<10f", data, h)
        ns, nb, nf, nl, _p, flags = struct.unpack_from("<HHHBBI", data, h + 40)
        offs = struct.unpack_from("<6I", data, h + 52)
        rec = {"name": name, "model_id": mid, "min": vals[0:3], "max": vals[3:6], "center": vals[6:9],
               "radius": vals[9], "flags": flags, "n_spheres": ns, "n_boxes": nb, "n_faces": nf}
        base = o + 4
        if nf:
            faces = np.frombuffer(data, dtype=[("a", "<u2"), ("b", "<u2"), ("c", "<u2"), ("m", "u1"), ("l", "u1")],
                                  count=nf, offset=base + offs[4])
            nv = int(max(faces["a"].max(), faces["b"].max(), faces["c"].max())) + 1
            verts = np.frombuffer(data, "<i2", nv * 3, base + offs[3]).reshape(nv, 3).astype(np.float64) / 128
            rec["vertices"] = verts
            rec["faces"] = np.stack([faces["a"], faces["b"], faces["c"]], 1).astype(np.int64)
            rec["materials"] = faces["m"].copy()
            rec["lights"] = faces["l"].copy()
            if flags & 8:
                ng = struct.unpack_from("<I", data, base + offs[4] - 4)[0]
                g0 = base + offs[4] - 4 - ng * 28
                rec["groups"] = [struct.unpack_from("<6fHH", data, g0 + i * 28) for i in range(ng)]
        if fourcc == b"COL3":
            nsf, osv, osf = struct.unpack_from("<3I", data, h + 76)
            rec["n_shadow_faces"] = nsf
        out.append(rec)
        o += 8 + size
    assert o == len(data) or all(b == 0 for b in data[o:]), "trailing garbage in col file"
    return out
