"""DFF (RenderWare clump) writer/reader for GTA San Andreas map objects.

Writes exactly what gta_sa.exe expects for an `objs` model:
  * one clump, one root frame (identity, NodeName = model name), one geometry, ONE atomic
    (CFileLoader::SetRelatedModelInfoCB keeps only one atomic per simple model);
  * geometry: positions + 1 UV set + prelit (day) colours, no normals, triangle list,
    Bin Mesh PLG (one mesh per material), optional night colours (Extra Vert Colour plugin
    0x253F2F9) which select the day/night building pipeline
    (CCustomBuildingRenderer::IsCBPCPipelineAttached / AtomicSetup, 0x5D7F40 / 0x5D7F00);
  * every object carries an EXTENSION chunk (RW requires it);
  * triangle file order (v1, v0, material, v2) as vanilla; logical (v0,v1,v2) is
    counter-clockwise seen from the front (right-hand normal), same as DragonFF;
  * UV convention: D3D (v = 0 at the top of the image).
Limits enforced: < 65536 vertices, texture names <= 31 chars, frame name <= 23 chars,
bounding sphere encloses all vertices.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from . import stream as rs

rpGEOMETRYTRISTRIP = 0x01
rpGEOMETRYPOSITIONS = 0x02
rpGEOMETRYTEXTURED = 0x04
rpGEOMETRYPRELIT = 0x08
rpGEOMETRYNORMALS = 0x10
rpGEOMETRYLIGHT = 0x20
rpGEOMETRYMODULATEMATERIALCOLOR = 0x40
rpGEOMETRYTEXTURED2 = 0x80

FILTER_LINEAR = 2
FILTER_LINEAR_MIP_LINEAR = 6
ADDR_WRAP, ADDR_MIRROR, ADDR_CLAMP = 1, 2, 3


@dataclass
class Material:
    texture: str | None
    color: tuple = (255, 255, 255, 255)
    mask: str = ""
    filter_mode: int = FILTER_LINEAR_MIP_LINEAR
    address_u: int = ADDR_WRAP
    address_v: int = ADDR_WRAP
    ambient: float = 1.0
    specular: float = 1.0
    diffuse: float = 1.0


@dataclass
class Effect2D:
    """Raw 2dfx entry (geometry plugin 0x253F2F8)."""
    pos: tuple
    type: int
    data: bytes


def light_2dfx(pos, color=(255, 230, 180, 200), corona_far_clip=100.0, pointlight_range=18.0,
               corona_size=1.5, shadow_size=0.0, flash=0, reflection=0, flare=0,
               shadow_mult=40, flags1=0x40 | 0x02, corona_tex="coronastar", shadow_tex="",
               shadow_z_dist=0, flags2=0, look_dir=(0, 0, 100)) -> Effect2D:
    """Light effect (type 0, 0x50 bytes layout, see Rwt2dEffectPluginDataChunkReadCallBack).
    flags1: 0x02 = no corona through walls?, 0x40 = at night (LIGHT_AT_NIGHT), 0x20 = at day.
    The corona texture is looked up in particle.txd ("coronastar", "shad_exp", ...)."""
    d = bytes(color[:4])
    d += struct.pack("<ffff", corona_far_clip, pointlight_range, corona_size, shadow_size)
    d += struct.pack("<BBBBB", flash, reflection, flare, shadow_mult, flags1 & 0xFF)
    d += corona_tex.encode("ascii")[:23].ljust(24, b"\0")
    d += shadow_tex.encode("ascii")[:23].ljust(24, b"\0")
    d += struct.pack("<BB", shadow_z_dist, flags2)
    d += struct.pack("<bbb", *look_dir)
    d += b"\0" * 2  # padding to 0x50
    assert len(d) == 0x50, len(d)
    return Effect2D(tuple(pos), 0, d)


@dataclass
class Geometry:
    vertices: np.ndarray            # (N,3) float
    uvs: np.ndarray                 # (N,2) float
    triangles: np.ndarray           # (M,3) int  (CCW = front)
    mat_ids: np.ndarray             # (M,) int
    materials: list
    prelit: np.ndarray | None = None   # (N,4) uint8 RGBA day colours
    night: np.ndarray | None = None    # (N,4) uint8 RGBA night colours
    effects: list = field(default_factory=list)

    def validate(self):
        n = len(self.vertices)
        assert 0 < n < 65536, f"vertex count {n} out of range"
        assert self.uvs.shape == (n, 2)
        assert self.triangles.ndim == 2 and self.triangles.shape[1] == 3
        assert len(self.mat_ids) == len(self.triangles)
        assert self.triangles.min() >= 0 and self.triangles.max() < n
        assert len(self.materials) > 0
        assert self.mat_ids.min() >= 0 and self.mat_ids.max() < len(self.materials)
        if self.prelit is not None:
            assert self.prelit.shape == (n, 4)
        if self.night is not None:
            assert self.prelit is not None, "night colours require prelit"
            assert self.night.shape == (n, 4)
        for m in self.materials:
            if m.texture is not None:
                assert 0 < len(m.texture) <= 31 and m.texture.isascii(), m.texture
        assert np.isfinite(self.vertices).all() and np.isfinite(self.uvs).all()


def bounding_sphere(v: np.ndarray):
    lo, hi = v.min(axis=0), v.max(axis=0)
    c = (lo + hi) / 2.0
    r = float(np.sqrt(((v - c) ** 2).sum(axis=1)).max())
    return c, r * 1.0005 + 1e-3


def _frame_list(name: str) -> bytes:
    s = struct.pack("<i", 1)
    s += struct.pack("<9f", 1, 0, 0, 0, 1, 0, 0, 0, 1)  # right, up, at
    s += struct.pack("<3f", 0, 0, 0)
    s += struct.pack("<ii", -1, 0x00020003)
    nm = name.encode("ascii")
    assert 0 < len(nm) <= 23, name
    ext = rs.chunk(rs.EXTENSION, rs.chunk(rs.NODE_NAME, nm))
    return rs.chunk(rs.FRAME_LIST, rs.chunk(rs.STRUCT, s) + ext)


def _material(m: Material) -> bytes:
    textured = 1 if m.texture else 0
    s = struct.pack("<i", 0) + bytes(m.color) + struct.pack("<ii", 1, textured)
    s += struct.pack("<fff", m.ambient, m.specular, m.diffuse)
    body = rs.chunk(rs.STRUCT, s)
    if textured:
        tflags = (m.filter_mode & 0xFF) | ((m.address_u & 0xF) << 8) | ((m.address_v & 0xF) << 12)
        tex = rs.chunk(rs.STRUCT, struct.pack("<I", tflags))
        tex += rs.string_chunk(m.texture) + rs.string_chunk(m.mask or "")
        tex += rs.chunk(rs.EXTENSION, b"")
        body += rs.chunk(rs.TEXTURE, tex)
    body += rs.chunk(rs.EXTENSION, b"")
    return rs.chunk(rs.MATERIAL, body)


def _bin_mesh(tris: np.ndarray, mat_ids: np.ndarray, nmat: int) -> bytes:
    meshes = []
    total = 0
    for mi in range(nmat):
        sel = tris[mat_ids == mi]
        if len(sel) == 0:
            continue
        idx = sel.reshape(-1).astype("<u4")
        meshes.append(struct.pack("<II", len(idx), mi) + idx.tobytes())
        total += len(idx)
    assert len(meshes) < 65536
    return rs.chunk(rs.BIN_MESH_PLG, struct.pack("<III", 0, len(meshes), total) + b"".join(meshes))


def _effects_2d(effects) -> bytes:
    if not effects:
        return b""
    d = struct.pack("<I", len(effects))
    for e in effects:
        d += struct.pack("<3fII", *e.pos, e.type, len(e.data)) + e.data
    return rs.chunk(rs.EFFECT_2D, d)


def _geometry(g: Geometry) -> bytes:
    g.validate()
    n, m = len(g.vertices), len(g.triangles)
    flags = rpGEOMETRYPOSITIONS | rpGEOMETRYTEXTURED | rpGEOMETRYLIGHT | rpGEOMETRYMODULATEMATERIALCOLOR
    if g.prelit is not None:
        flags |= rpGEOMETRYPRELIT
    flags |= 1 << 16  # one texcoord set
    s = struct.pack("<Iiii", flags, m, n, 1)
    if g.prelit is not None:
        s += np.ascontiguousarray(g.prelit, dtype=np.uint8).tobytes()
    s += np.ascontiguousarray(g.uvs, dtype="<f4").tobytes()
    tri = np.empty((m, 4), dtype="<u2")
    tri[:, 0] = g.triangles[:, 1]
    tri[:, 1] = g.triangles[:, 0]
    tri[:, 2] = g.mat_ids
    tri[:, 3] = g.triangles[:, 2]
    s += tri.tobytes()
    c, r = bounding_sphere(g.vertices.astype(np.float64))
    s += struct.pack("<4f", float(c[0]), float(c[1]), float(c[2]), r)
    s += struct.pack("<ii", 1, 0)
    s += np.ascontiguousarray(g.vertices, dtype="<f4").tobytes()
    body = rs.chunk(rs.STRUCT, s)
    ml = struct.pack("<i", len(g.materials)) + struct.pack("<%di" % len(g.materials), *([-1] * len(g.materials)))
    ml = rs.chunk(rs.STRUCT, ml) + b"".join(_material(mt) for mt in g.materials)
    body += rs.chunk(rs.MATERIAL_LIST, ml)
    ext = _bin_mesh(g.triangles, g.mat_ids, len(g.materials))
    ext += _effects_2d(g.effects)
    if g.night is not None:
        ext += rs.chunk(rs.EXTRA_VERT_COLOUR,
                        struct.pack("<I", 1) + np.ascontiguousarray(g.night, dtype=np.uint8).tobytes())
    body += rs.chunk(rs.EXTENSION, ext)
    return rs.chunk(rs.GEOMETRY, body)


def write_dff(name: str, geom: Geometry, pipeline: int | None = rs.PIPE_BUILDING_DN) -> bytes:
    """Serialise a single-atomic SA map model."""
    clump = rs.chunk(rs.STRUCT, struct.pack("<iii", 1, 0, 0))
    clump += _frame_list(name)
    clump += rs.chunk(rs.GEOMETRY_LIST, rs.chunk(rs.STRUCT, struct.pack("<i", 1)) + _geometry(geom))
    aext = b""
    if pipeline is not None:
        aext = rs.chunk(rs.PIPELINE_SET, struct.pack("<I", pipeline))
    atomic = rs.chunk(rs.STRUCT, struct.pack("<iiii", 0, 0, 5, 0)) + rs.chunk(rs.EXTENSION, aext)
    clump += rs.chunk(rs.ATOMIC, atomic)
    clump += rs.chunk(rs.EXTENSION, b"")
    return rs.chunk(rs.CLUMP, clump)


# ----------------------------------------------------------------------------------------
# Reader (independent re-parse used by validators/tests; mirrors RpClumpStreamRead order)
# ----------------------------------------------------------------------------------------

def _find(data, off, end, ctype):
    for t, p, s, v in rs.iter_chunks(data, off, end):
        if t == ctype:
            return p, s, v
    raise ValueError(f"chunk 0x{ctype:X} not found")


def read_dff(data: bytes) -> dict:
    t, size, ver = rs.read_header(data, 0)
    assert t == rs.CLUMP, f"not a clump (0x{t:X})"
    assert 0x34000 <= ((ver >> 14 & 0x3FF00) + 0x30000 | (ver >> 16 & 0x3F)) <= 0x36003 or ver == rs.RW_VERSION_SA
    end = 12 + size
    assert end == len(data), f"clump size {end} != file size {len(data)}"
    chunks = list(rs.iter_chunks(data, 12, end))
    types = [c[0] for c in chunks]
    assert types[0] == rs.STRUCT
    na, nl, nc = struct.unpack_from("<iii", data, chunks[0][1])
    out = {"atomics": [], "frames": [], "geometries": []}
    # frame list
    fp, fs, _ = [(p, s, v) for t, p, s, v in chunks if t == rs.FRAME_LIST][0]
    fl = list(rs.iter_chunks(data, fp, fp + fs))
    nf = struct.unpack_from("<i", data, fl[0][1])[0]
    for i in range(nf):
        vals = struct.unpack_from("<12fii", data, fl[0][1] + 4 + i * 56)
        out["frames"].append({"matrix": vals[:12], "parent": vals[12]})
    exts = [c for c in fl[1:] if c[0] == rs.EXTENSION]
    assert len(exts) == nf
    for i, (t, p, s, v) in enumerate(exts):
        for t2, p2, s2, v2 in rs.iter_chunks(data, p, p + s):
            if t2 == rs.NODE_NAME:
                out["frames"][i]["name"] = data[p2:p2 + s2].decode("ascii")
    # geometry list
    gp, gs, _ = [(p, s, v) for t, p, s, v in chunks if t == rs.GEOMETRY_LIST][0]
    gl = list(rs.iter_chunks(data, gp, gp + gs))
    ng = struct.unpack_from("<i", data, gl[0][1])[0]
    geoms = [c for c in gl[1:] if c[0] == rs.GEOMETRY]
    assert len(geoms) == ng
    for t, p, s, v in geoms:
        out["geometries"].append(_read_geometry(data, p, s))
    for t, p, s, v in chunks:
        if t == rs.ATOMIC:
            ac = list(rs.iter_chunks(data, p, p + s))
            assert ac[0][0] == rs.STRUCT and ac[0][2] == 16
            fi, gi, fl_, _u = struct.unpack_from("<iiii", data, ac[0][1])
            atom = {"frame": fi, "geometry": gi, "flags": fl_, "pipeline": None}
            assert ac[-1][0] == rs.EXTENSION
            for t2, p2, s2, v2 in rs.iter_chunks(data, ac[-1][1], ac[-1][1] + ac[-1][2]):
                if t2 == rs.PIPELINE_SET:
                    assert s2 == 4
                    atom["pipeline"] = struct.unpack_from("<I", data, p2)[0]
            out["atomics"].append(atom)
    assert len(out["atomics"]) == na
    assert types[-1] == rs.EXTENSION
    return out


def _read_geometry(data, p, s):
    gc = list(rs.iter_chunks(data, p, p + s))
    assert gc[0][0] == rs.STRUCT
    sp = gc[0][1]
    flags, nt, nv, nmorph = struct.unpack_from("<Iiii", data, sp)
    o = sp + 16
    g = {"flags": flags}
    if flags & rpGEOMETRYPRELIT:
        g["prelit"] = np.frombuffer(data, np.uint8, nv * 4, o).reshape(nv, 4)
        o += nv * 4
    ntc = (flags >> 16) & 0xFF or (2 if flags & rpGEOMETRYTEXTURED2 else 1 if flags & rpGEOMETRYTEXTURED else 0)
    g["uvs"] = []
    for _ in range(ntc):
        g["uvs"].append(np.frombuffer(data, "<f4", nv * 2, o).reshape(nv, 2))
        o += nv * 8
    tri = np.frombuffer(data, "<u2", nt * 4, o).reshape(nt, 4)
    o += nt * 8
    g["triangles"] = np.stack([tri[:, 1], tri[:, 0], tri[:, 3]], axis=1)
    g["mat_ids"] = tri[:, 2]
    sphere = struct.unpack_from("<4f", data, o)
    hasv, hasn = struct.unpack_from("<ii", data, o + 16)
    o += 24
    g["sphere"] = sphere
    g["vertices"] = np.frombuffer(data, "<f4", nv * 3, o).reshape(nv, 3)
    o += nv * 12
    if hasn:
        o += nv * 12
    assert o == sp + gc[0][2], f"geometry struct size mismatch {o - sp} vs {gc[0][2]}"
    # material list
    mlp, mls = [(pp, ss) for t, pp, ss, v in gc if t == rs.MATERIAL_LIST][0]
    mlc = list(rs.iter_chunks(data, mlp, mlp + mls))
    nmat = struct.unpack_from("<i", data, mlc[0][1])[0]
    g["materials"] = []
    for t, pp, ss, v in mlc[1:]:
        assert t == rs.MATERIAL
        mc = list(rs.iter_chunks(data, pp, pp + ss))
        assert mc[0][2] == 28
        _f, r, gg, b, a, _u, textured = struct.unpack_from("<i4Bii", data, mc[0][1])
        mat = {"color": (r, gg, b, a), "texture": None}
        if textured:
            tc = [c for c in mc if c[0] == rs.TEXTURE][0]
            tcc = list(rs.iter_chunks(data, tc[1], tc[1] + tc[2]))
            strings = [data[c[1]:c[1] + c[2]].split(b"\0")[0].decode("ascii") for c in tcc if c[0] == rs.STRING]
            mat["texture"], mat["mask"] = strings[0], strings[1]
            mat["filter"] = struct.unpack_from("<I", data, tcc[0][1])[0]
            assert tcc[-1][0] == rs.EXTENSION
        assert mc[-1][0] == rs.EXTENSION
        g["materials"].append(mat)
    assert len(g["materials"]) == nmat
    # extensions
    ep, es = [(pp, ss) for t, pp, ss, v in gc if t == rs.EXTENSION][0]
    for t, pp, ss, v in rs.iter_chunks(data, ep, ep + es):
        if t == rs.BIN_MESH_PLG:
            fl, nmesh, total = struct.unpack_from("<III", data, pp)
            q = pp + 12
            meshes = []
            for _ in range(nmesh):
                ni, mi = struct.unpack_from("<II", data, q)
                q += 8
                meshes.append((mi, np.frombuffer(data, "<u4", ni, q)))
                q += 4 * ni
            assert q == pp + ss
            g["binmesh"] = (fl, meshes, total)
        elif t == rs.EXTRA_VERT_COLOUR:
            has = struct.unpack_from("<I", data, pp)[0]
            if has:
                assert ss == 4 + nv * 4
                g["night"] = np.frombuffer(data, np.uint8, nv * 4, pp + 4).reshape(nv, 4)
        elif t == rs.EFFECT_2D:
            cnt = struct.unpack_from("<I", data, pp)[0]
            q = pp + 4
            effs = []
            for _ in range(cnt):
                x, y, z, et, ds = struct.unpack_from("<3fII", data, q)
                q += 20
                effs.append(((x, y, z), et, data[q:q + ds]))
                q += ds
            assert q == pp + ss
            g["effects"] = effs
    return g
