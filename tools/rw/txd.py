"""TXD (RenderWare texture dictionary) writer/reader for GTA San Andreas PC (D3D9, platform 9).

Raster layout (Texture Native 0x15 / Struct) as read by the D3D9 native texture reader:
  uint32 platform (9), uint8 filter, uint8 addressing (U | V<<4), uint16 pad, char name[32],
  char mask[32], uint32 rasterFormat, uint32 d3dFormat (FourCC for DXT), uint16 w, uint16 h,
  uint8 depth, uint8 numLevels, uint8 rasterType (4 = texture),
  uint8 flags (1 alpha, 2 cube, 4 auto-mipmaps, 8 compressed), then per level uint32 size + data.
Supported encodings: DXT1 (opaque), DXT5 (alpha), 8888 (BGRA uncompressed).
Compression uses Pillow's BCn encoder (DDS writer) level by level.
"""
from __future__ import annotations

import io
import struct

import numpy as np
from PIL import Image

from . import stream as rs

FORMAT_1555 = 0x0100
FORMAT_565 = 0x0200
FORMAT_4444 = 0x0300
FORMAT_8888 = 0x0500
FORMAT_888 = 0x0600
FORMAT_EXT_MIPMAP = 0x8000

D3DFMT_A8R8G8B8 = 21
FOURCC_DXT1 = 0x31545844
FOURCC_DXT3 = 0x33545844
FOURCC_DXT5 = 0x35545844


def _dxt_encode(img: Image.Image, fmt: str) -> bytes:
    """Encode one level with Pillow; returns raw block data (DDS header stripped)."""
    buf = io.BytesIO()
    img.save(buf, format="DDS", pixel_format=fmt)
    raw = buf.getvalue()
    assert raw[:4] == b"DDS "
    hdr_size = 4 + struct.unpack_from("<I", raw, 4)[0]  # magic + header (124)
    fourcc = raw[84:88]
    if fourcc == b"DX10":
        hdr_size += 20
    w, h = img.size
    bs = 8 if fmt == "DXT1" else 16
    expect = max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * bs
    data = raw[hdr_size:hdr_size + expect]
    assert len(data) == expect, (len(data), expect)
    return data


def _mip_chain(img: Image.Image, min_size: int = 4):
    levels = [img]
    w, h = img.size
    while w > min_size and h > min_size:
        w, h = max(1, w // 2), max(1, h // 2)
        levels.append(levels[-1].resize((w, h), Image.LANCZOS))
    return levels


def texture_native(name: str, img: Image.Image, encoding: str = "auto", mipmaps: bool = True,
                   address=(1, 1), mask: str = "") -> bytes:
    """Build one Texture Native (raster) chunk."""
    assert 0 < len(name) <= 31 and name.isascii(), name
    img = img.convert("RGBA")
    w, h = img.size
    assert w & (w - 1) == 0 and h & (h - 1) == 0, f"{name}: size {w}x{h} not power of two"
    alpha = np.asarray(img)[:, :, 3]
    has_alpha = bool((alpha < 255).any())
    if encoding == "auto":
        encoding = "DXT5" if has_alpha else "DXT1"
    levels = _mip_chain(img) if mipmaps else [img]
    if encoding in ("DXT1", "DXT3", "DXT5"):
        assert w >= 4 and h >= 4
        raster_format = {"DXT1": FORMAT_565 if not has_alpha else FORMAT_1555,
                         "DXT3": FORMAT_4444, "DXT5": FORMAT_4444}[encoding]
        d3dfmt = {"DXT1": FOURCC_DXT1, "DXT3": FOURCC_DXT3, "DXT5": FOURCC_DXT5}[encoding]
        depth = 16
        flags = 0x08 | (0x01 if has_alpha else 0)
        if encoding == "DXT1":
            lv = [im.convert("RGB") if not has_alpha else im for im in levels]
            datas = [_dxt_encode(im, "DXT1") for im in lv]
        else:
            datas = [_dxt_encode(im, encoding) for im in levels]
    elif encoding == "8888":
        raster_format = FORMAT_8888
        d3dfmt = D3DFMT_A8R8G8B8
        depth = 32
        flags = 0x01 if has_alpha else 0
        datas = []
        for im in levels:
            a = np.asarray(im, dtype=np.uint8)
            datas.append(np.ascontiguousarray(a[:, :, [2, 1, 0, 3]]).tobytes())  # BGRA
    else:
        raise ValueError(encoding)
    if len(levels) > 1:
        raster_format |= FORMAT_EXT_MIPMAP
    filt = 6 if len(levels) > 1 else 2
    s = struct.pack("<IBBH", 9, filt, (address[0] & 0xF) | ((address[1] & 0xF) << 4), 0)
    s += name.encode("ascii").ljust(32, b"\0") + mask.encode("ascii").ljust(32, b"\0")
    s += struct.pack("<IIHHBBBB", raster_format, d3dfmt, w, h, depth, len(levels), 4, flags)
    for d in datas:
        s += struct.pack("<I", len(d)) + d
    return rs.chunk(rs.TEXTURE_NATIVE, rs.chunk(rs.STRUCT, s) + rs.chunk(rs.EXTENSION, b""))


def write_txd(textures: list) -> bytes:
    """textures: list of (name, PIL.Image[, encoding]) or prebuilt raster chunks (bytes)."""
    rasters = []
    for t in textures:
        if isinstance(t, (bytes, bytearray)):
            rasters.append(bytes(t))
        else:
            name, img, *rest = t
            enc = rest[0] if rest else "auto"
            rasters.append(texture_native(name, img, enc))
    assert len(rasters) < 65536
    body = rs.chunk(rs.STRUCT, struct.pack("<HH", len(rasters), 2)) + b"".join(rasters)
    body += rs.chunk(rs.EXTENSION, b"")
    return rs.chunk(rs.TEXDICTIONARY, body)


def read_txd(data: bytes, decode: bool = False) -> list:
    """Parse a TXD; returns list of dicts. With decode=True also returns level-0 RGBA via Pillow."""
    t, size, ver = rs.read_header(data, 0)
    assert t == rs.TEXDICTIONARY
    assert 12 + size == len(data)
    chunks = list(rs.iter_chunks(data, 12, 12 + size))
    assert chunks[0][0] == rs.STRUCT
    count, dev = struct.unpack_from("<HH", data, chunks[0][1])
    out = []
    for t, p, s, v in chunks[1:]:
        if t != rs.TEXTURE_NATIVE:
            continue
        rc = list(rs.iter_chunks(data, p, p + s))
        sp, ss = rc[0][1], rc[0][2]
        plat, filt, addr, _ = struct.unpack_from("<IBBH", data, sp)
        assert plat == 9, f"platform {plat}"
        name = data[sp + 8:sp + 40].split(b"\0")[0].decode("ascii")
        mask = data[sp + 40:sp + 72].split(b"\0")[0].decode("ascii")
        rf, d3d, w, h, depth, nl, rtype, flags = struct.unpack_from("<IIHHBBBB", data, sp + 72)
        o = sp + 88
        levels = []
        for i in range(nl):
            n = struct.unpack_from("<I", data, o)[0]
            levels.append(data[o + 4:o + 4 + n])
            o += 4 + n
        assert o == sp + ss, f"{name}: raster struct size mismatch"
        tex = {"name": name, "mask": mask, "raster_format": rf, "d3d_format": d3d, "width": w,
               "height": h, "depth": depth, "levels": nl, "flags": flags, "filter": filt,
               "addressing": addr, "sizes": [len(x) for x in levels]}
        if decode:
            tex["image"] = _decode_level(levels[0], w, h, d3d)
        out.append(tex)
    assert len(out) == count
    return out


def _decode_level(data, w, h, d3d):
    if d3d in (FOURCC_DXT1, FOURCC_DXT3, FOURCC_DXT5):
        fourcc = struct.pack("<I", d3d)
        hdr = bytearray(128)
        hdr[0:4] = b"DDS "
        struct.pack_into("<IIIIIII", hdr, 4, 124, 0x1007 | 0x80000, h, w, len(data), 0, 1)
        struct.pack_into("<II4s", hdr, 76, 32, 0x4, fourcc)
        struct.pack_into("<I", hdr, 108, 0x1000)
        return Image.open(io.BytesIO(bytes(hdr) + data)).convert("RGBA")
    if d3d == D3DFMT_A8R8G8B8:
        a = np.frombuffer(data, np.uint8).reshape(h, w, 4)[:, :, [2, 1, 0, 3]]
        return Image.fromarray(a, "RGBA")
    raise ValueError(f"unsupported d3d format {d3d}")
