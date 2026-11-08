"""IPL (item placement) and IDE (item definition) writers/readers for GTA San Andreas.

Text IPL inst line: id, name, interior, x, y, z, qx, qy, qz, qw, lod
Binary IPL ("bnry", streamed from IMG): header 0x4C (tBinaryIplFile), CFileObjectInstance 0x28 each
(pos xyz, quat xyzw, model id, flags/areacode, lod index), CFileCarGenerator 0x30 each.

Rotation convention (CFileLoader::LoadObjectInstance): for a pure heading h (radians, CCW seen
from above) the stored quaternion is (0, 0, -sin(h/2), cos(h/2)) (the engine negates the
imaginary part / uses acos(w) * (z < 0 ? 2 : -2)).
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

FLAG_UNIMPORTANT = 0x100
FLAG_UNDERWATER = 0x400
FLAG_TUNNEL = 0x800
FLAG_TUNNEL_TRANSITION = 0x1000


def heading_quat(h: float):
    return (0.0, 0.0, -math.sin(h / 2.0), math.cos(h / 2.0))


def quat_heading(q):
    """Inverse of heading_quat (as the engine computes it)."""
    x, y, z, w = q
    w = max(-1.0, min(1.0, w))
    return math.acos(w) * (2.0 if z < 0 else -2.0)


@dataclass
class Inst:
    model_id: int
    name: str
    x: float
    y: float
    z: float
    heading: float = 0.0
    quat: tuple | None = None
    flags: int = 0               # low byte = interior / area code
    lod: int = -1

    def q(self):
        return self.quat if self.quat is not None else heading_quat(self.heading)


@dataclass
class CarGen:
    x: float
    y: float
    z: float
    angle: float               # degrees
    model_id: int = -1         # -1 = random from cargrp
    col1: int = -1
    col2: int = -1
    force_spawn: int = 0
    alarm: int = 0
    locked: int = 0
    min_delay: int = 0
    max_delay: int = 0


def _fmt(v):
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def text_ipl(insts, cars=(), comment=None) -> str:
    out = []
    if comment:
        out += ["# " + c for c in comment.splitlines()]
    out.append("inst")
    for i in insts:
        qx, qy, qz, qw = i.q()
        out.append(f"{i.model_id}, {i.name}, {i.flags}, {_fmt(i.x)}, {_fmt(i.y)}, {_fmt(i.z)}, "
                   f"{_fmt(qx)}, {_fmt(qy)}, {_fmt(qz)}, {_fmt(qw)}, {i.lod}")
    out.append("end")
    for sec in ("cull", "pick", "path", "occl", "mult", "grge", "enex", "jump", "tcyc", "auzo"):
        out += [sec, "end"]
    out.append("cars")
    for c in cars:
        out.append(f"{_fmt(c.x)}, {_fmt(c.y)}, {_fmt(c.z)}, {_fmt(c.angle)}, {c.model_id}, {c.col1}, {c.col2}, "
                   f"{c.force_spawn}, {c.alarm}, {c.locked}, {c.min_delay}, {c.max_delay}")
    out.append("end")
    return "\r\n".join(out) + "\r\n"


def binary_ipl(insts, cars=()) -> bytes:
    n_i, n_c = len(insts), len(cars)
    off_i = 0x4C
    off_c = off_i + 0x28 * n_i
    hdr = b"bnry" + struct.pack("<6I", n_i, 0, 0, 0, n_c, 0)
    hdr += struct.pack("<12I", off_i, 0, 0, 0, 0, 0, 0, 0, off_c if n_c else 0, 0, 0, 0)
    assert len(hdr) == 0x4C
    body = bytearray()
    for i in insts:
        qx, qy, qz, qw = i.q()
        body += struct.pack("<3f4fiIi", i.x, i.y, i.z, qx, qy, qz, qw, i.model_id, i.flags & 0xFFFFFFFF, i.lod)
    for c in cars:
        body += struct.pack("<4f8i", c.x, c.y, c.z, c.angle, c.model_id, c.col1, c.col2, c.force_spawn,
                            c.alarm, c.locked, c.min_delay, c.max_delay)
    return hdr + bytes(body)


def read_binary_ipl(data: bytes):
    assert data[:4] == b"bnry"
    n = struct.unpack_from("<6I", data, 4)
    offs = struct.unpack_from("<12I", data, 28)
    insts = []
    for k in range(n[0]):
        v = struct.unpack_from("<3f4fiIi", data, offs[0] + 0x28 * k)
        insts.append(Inst(v[7], "", v[0], v[1], v[2], quat=v[3:7], flags=v[8], lod=v[9]))
    cars = []
    for k in range(n[4]):
        v = struct.unpack_from("<4f8i", data, offs[8] + 0x30 * k)
        cars.append(CarGen(*v))
    return insts, cars


def read_text_ipl(text: str):
    insts = []
    sec = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if sec is None:
            sec = line.lower()
            continue
        if line.lower() == "end":
            sec = None
            continue
        if sec == "inst":
            p = [t.strip() for t in line.replace(",", " ").split()]
            insts.append(Inst(int(p[0]), p[1], float(p[3]), float(p[4]), float(p[5]),
                              quat=tuple(map(float, p[6:10])), flags=int(p[2]), lod=int(p[10])))
    return insts


# ---------------------------------------------------------------------------- IDE
@dataclass
class ObjDef:
    model_id: int
    name: str
    txd: str
    draw_distance: float
    flags: int = 0


def ide_text(objs, comment=None, txdp=()) -> str:
    out = []
    if comment:
        out += ["# " + c for c in comment.splitlines()]
    out.append("objs")
    for o in objs:
        assert len(o.name) <= 23 and len(o.txd) <= 23, (o.name, o.txd)
        out.append(f"{o.model_id}, {o.name}, {o.txd}, {_fmt(o.draw_distance)}, {o.flags}")
    out.append("end")
    out += ["tobj", "end", "anim", "end"]
    if txdp:
        out.append("txdp")
        for child, parent in txdp:
            out.append(f"{child}, {parent}")
        out.append("end")
    return "\r\n".join(out) + "\r\n"
