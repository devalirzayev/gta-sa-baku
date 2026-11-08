"""RenderWare binary stream chunk helpers.

Every RW chunk = uint32 type, uint32 size (payload incl. children), uint32 library id stamp.
GTA SA writes RW 3.6.0.3 build 0xFFFF -> stamp 0x1803FFFF.
"""
import struct

RW_VERSION_SA = 0x1803FFFF

# Core section ids
STRUCT = 0x01
STRING = 0x02
EXTENSION = 0x03
TEXTURE = 0x06
MATERIAL = 0x07
MATERIAL_LIST = 0x08
FRAME_LIST = 0x0E
GEOMETRY = 0x0F
CLUMP = 0x10
ATOMIC = 0x14
TEXTURE_NATIVE = 0x15
TEXDICTIONARY = 0x16
GEOMETRY_LIST = 0x1A
BIN_MESH_PLG = 0x50E
# Rockstar plugins
PIPELINE_SET = 0x0253F2F3
SPECULAR_MAT = 0x0253F2F6
EFFECT_2D = 0x0253F2F8
EXTRA_VERT_COLOUR = 0x0253F2F9
COLLISION_PLG = 0x0253F2FA
REFLECTION_MAT = 0x0253F2FC
BREAKABLE = 0x0253F2FD
NODE_NAME = 0x0253F2FE

# SA building pipelines (CCustomBuildingRenderer::IsCBPCPipelineAttached 0x5D7F40)
PIPE_BUILDING_DN = 0x53F20098   # day/night (uses night colours when present)
PIPE_BUILDING = 0x53F2009C


def chunk(ctype: int, payload: bytes, version: int = RW_VERSION_SA) -> bytes:
    return struct.pack("<III", ctype, len(payload), version) + payload


def string_chunk(s: str) -> bytes:
    """RW string chunk: zero terminated, padded to 4 bytes."""
    b = s.encode("ascii") + b"\0"
    b += b"\0" * ((4 - len(b) % 4) % 4)
    return chunk(STRING, b)


def read_header(data: bytes, off: int):
    t, s, v = struct.unpack_from("<III", data, off)
    return t, s, v


def iter_chunks(data: bytes, off: int, end: int):
    """Yield (type, payload_offset, size, version) for consecutive chunks in [off, end)."""
    while off + 12 <= end:
        t, s, v = struct.unpack_from("<III", data, off)
        if off + 12 + s > end:
            raise ValueError(f"chunk 0x{t:X} at {off} overruns parent ({off+12+s} > {end})")
        yield t, off + 12, s, v
        off += 12 + s
    if off != end:
        raise ValueError(f"trailing {end-off} bytes in chunk list")
