"""IMG archive (VER2, GTA San Andreas) writer/reader.

Header: "VER2", uint32 count; entries 32 B: uint32 offset (sectors), uint16 streaming size
(sectors), uint16 size in archive (0), char name[24] (NUL-terminated). Sector = 2048 bytes.
Files are stored in directory order without gaps (the streamer may read neighbours in one go).
"""
from __future__ import annotations

import struct

SECTOR = 2048


def write_img(path: str, files: list):
    """files: list of (name, bytes). Names are unique case-insensitively, <= 23 chars."""
    seen = set()
    for name, _ in files:
        assert 0 < len(name) <= 23 and name.isascii(), name
        k = name.lower()
        assert k not in seen, f"duplicate IMG entry {name}"
        seen.add(k)
    n = len(files)
    dir_bytes = 8 + 32 * n
    first = (dir_bytes + SECTOR - 1) // SECTOR
    entries = []
    cur = first
    for name, data in files:
        sectors = max(1, (len(data) + SECTOR - 1) // SECTOR)
        assert sectors < 65536
        entries.append((cur, sectors, name))
        cur += sectors
    with open(path, "wb") as f:
        f.write(b"VER2" + struct.pack("<I", n))
        for off, sec, name in entries:
            f.write(struct.pack("<IHH", off, sec, 0) + name.encode("ascii").ljust(24, b"\0"))
        f.write(b"\0" * (first * SECTOR - dir_bytes))
        for (off, sec, name), (_, data) in zip(entries, files):
            assert f.tell() == off * SECTOR
            f.write(data)
            f.write(b"\0" * (sec * SECTOR - len(data)))


def read_img(path: str) -> list:
    """Returns list of (name, bytes)."""
    with open(path, "rb") as f:
        data = f.read()
    assert data[:4] == b"VER2", "not a VER2 archive"
    n = struct.unpack_from("<I", data, 4)[0]
    out = []
    for i in range(n):
        off, ssz, asz = struct.unpack_from("<IHH", data, 8 + 32 * i)
        name = data[16 + 32 * i:40 + 32 * i].split(b"\0")[0].decode("ascii")
        size = (ssz or asz) * SECTOR
        out.append((name, data[off * SECTOR:off * SECTOR + size]))
    return out
