#!/usr/bin/env python3
"""Baku for GTA San Andreas - installer / uninstaller (Python 3, standard library only).

    python baku_install.py install   "C:\\Games\\GTA San Andreas"
    python baku_install.py uninstall "C:\\Games\\GTA San Andreas"
    python baku_install.py status    "C:\\Games\\GTA San Andreas"

What install does (everything is backed up into <game>\\baku_backup first):
  1. copies files\\*  into the game folder (models\\baku.img, data\\gta.dat, data\\maps\\baku\\...,
     data\\water.dat, data\\script\\main.scm, ...);
  2. patches models\\gta3.img: every file in gta3img\\ (stream IPLs, COL, nodes*.dat, radar*.txd)
     is appended to the end of the archive and the directory entry with the same name is
     pointed to it (the original data stays untouched inside the file);
  3. adds the Azerbaijani district names to text\\american.gxt (key BKZ000...).
uninstall restores every backed-up file, the original gta3.img directory and its length.
The PowerShell installer (install.ps1) implements exactly the same steps.
"""
import json
import os
import shutil
import struct
import sys
import zlib

SECTOR = 2048
HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.dirname(HERE)
BACKUP = "baku_backup"


def find_ci(base, rel):
    """Case-insensitive path resolution (works on any OS)."""
    cur = base
    for part in rel.replace("\\", "/").split("/"):
        if not part:
            continue
        if os.path.isdir(cur):
            match = [x for x in os.listdir(cur) if x.lower() == part.lower()]
            cur = os.path.join(cur, match[0] if match else part)
        else:
            cur = os.path.join(cur, part)
    return cur


def read_dir(path):
    with open(path, "rb") as f:
        head = f.read(8)
        if head[:4] != b"VER2":
            raise SystemExit("gta3.img is not a VER2 archive (GTA San Andreas PC)")
        n = struct.unpack("<I", head[4:])[0]
        d = f.read(32 * n)
    return head, d, n


def gxt_hash(key):
    up = "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in key)
    return zlib.crc32(up.encode("latin-1")) ^ 0xFFFFFFFF


def read_gxt(data):
    if struct.unpack_from("<HH", data, 0) != (4, 8) or data[4:8] != b"TABL":
        raise ValueError("not an SA GXT")
    n = struct.unpack_from("<I", data, 8)[0] // 12
    tables = []
    for i in range(n):
        name = data[12 + 12 * i:20 + 12 * i].split(b"\0")[0].decode("ascii")
        off = struct.unpack_from("<I", data, 20 + 12 * i)[0] + (0 if name == "MAIN" else 8)
        ks = struct.unpack_from("<I", data, off + 4)[0]
        keys = [struct.unpack_from("<II", data, off + 8 + 8 * j) for j in range(ks // 8)]
        ds = off + 8 + ks + 8
        tab = {}
        for o, h in keys:
            e = data.index(b"\0", ds + o)
            tab[h] = data[ds + o:e]
        tables.append((name, tab))
    return tables


def build_gxt(tables):
    def block(tab):
        items = sorted(tab.items())
        tdat, tkey = bytearray(), bytearray()
        for h, b in items:
            tkey += struct.pack("<II", len(tdat), h)
            tdat += b + b"\0"
        return b"TKEY" + struct.pack("<I", len(tkey)) + tkey + b"TDAT" + struct.pack("<I", len(tdat)) + tdat
    tables = sorted(tables, key=lambda t: (t[0] != "MAIN", t[0]))
    head = struct.pack("<HH", 4, 8)
    off = len(head) + 8 + 12 * len(tables)
    tabl, body = bytearray(), bytearray()
    for name, tab in tables:
        b = block(tab)
        tabl += name.encode().ljust(8, b"\0") + struct.pack("<I", off)
        if name != "MAIN":
            b = name.encode().ljust(8, b"\0") + b
        body += b
        off += len(b)
    return head + b"TABL" + struct.pack("<I", 12 * len(tables)) + bytes(tabl) + bytes(body)


def install(game):
    game = os.path.abspath(game)
    img_path = find_ci(game, "models/gta3.img")
    if not os.path.isfile(img_path):
        raise SystemExit(f"{img_path} not found - is this the GTA San Andreas folder?")
    bdir = os.path.join(game, BACKUP)
    if os.path.exists(os.path.join(bdir, "installed.json")):
        raise SystemExit("Baku is already installed (baku_backup\\installed.json exists). Uninstall first.")
    os.makedirs(bdir, exist_ok=True)
    state = {"copied": [], "backed_up": [], "img_size": os.path.getsize(img_path), "gxt": None}
    # 1. gta3.img directory backup + patch
    head, d, n = read_dir(img_path)
    with open(os.path.join(bdir, "gta3img_directory.bin"), "wb") as f:
        f.write(head + d)
    names = {}
    for i in range(n):
        nm = d[32 * i + 8:32 * i + 32].split(b"\0")[0].decode("latin-1").lower()
        names.setdefault(nm, i)
    repl_dir = os.path.join(MOD, "gta3img")
    d = bytearray(d)
    missing = []
    with open(img_path, "r+b") as f:
        f.seek(0, 2)
        end = f.tell()
        pad = (-end) % SECTOR
        f.write(b"\0" * pad)
        pos = end + pad
        for fn in sorted(os.listdir(repl_dir)):
            i = names.get(fn.lower())
            if i is None:
                missing.append(fn)
                continue
            data = open(os.path.join(repl_dir, fn), "rb").read()
            sectors = max(1, (len(data) + SECTOR - 1) // SECTOR)
            f.seek(pos)
            f.write(data + b"\0" * (sectors * SECTOR - len(data)))
            struct.pack_into("<IHH", d, 32 * i, pos // SECTOR, sectors, 0)
            pos += sectors * SECTOR
        f.seek(8)
        f.write(bytes(d))
    state["img_missing"] = missing
    # 2. copy files (backup originals)
    src_root = os.path.join(MOD, "files")
    for root, _, files in os.walk(src_root):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), src_root)
            dst = find_ci(game, rel)
            if os.path.exists(dst):
                bk = os.path.join(bdir, "files", rel)
                os.makedirs(os.path.dirname(bk), exist_ok=True)
                shutil.copy2(dst, bk)
                state["backed_up"].append(rel)
            else:
                state["copied"].append(rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(root, fn), dst)
    # 3. district names into every language GXT (american, french, german, italian, spanish)
    texts = json.load(open(os.path.join(MOD, "text", "zone_texts.json"), encoding="utf-8"))
    state["gxt"] = []
    for lang in ("american", "french", "german", "italian", "spanish"):
        gxt = find_ci(game, f"text/{lang}.gxt")
        if not os.path.isfile(gxt):
            continue
        raw = open(gxt, "rb").read()
        try:
            tables = read_gxt(raw)
        except Exception as ex:
            print(f"warning: {lang}.gxt not patched ({ex})")
            continue
        shutil.copy2(gxt, os.path.join(bdir, f"{lang}.gxt"))
        main = dict(tables[[t[0] for t in tables].index("MAIN")][1])
        for k, v in texts.items():
            main[gxt_hash(k)] = bytes.fromhex(v)
        tables = [(nm, main if nm == "MAIN" else tab) for nm, tab in tables]
        with open(gxt, "wb") as f:
            f.write(build_gxt(tables))
        state["gxt"].append(lang)
    with open(os.path.join(bdir, "installed.json"), "w") as f:
        json.dump(state, f, indent=1)
    print("Baku installed.")
    if missing:
        print("Note: these entries were not found in gta3.img (non-standard game copy):", ", ".join(missing[:20]))


def uninstall(game):
    game = os.path.abspath(game)
    bdir = os.path.join(game, BACKUP)
    st = os.path.join(bdir, "installed.json")
    if not os.path.exists(st):
        raise SystemExit("Baku is not installed (no baku_backup\\installed.json).")
    state = json.load(open(st, encoding="utf-8-sig"))
    img_path = find_ci(game, "models/gta3.img")
    hd = open(os.path.join(bdir, "gta3img_directory.bin"), "rb").read()
    with open(img_path, "r+b") as f:
        f.seek(0)
        f.write(hd)
        f.truncate(state["img_size"])
    for rel in state["backed_up"]:
        shutil.copy2(os.path.join(bdir, "files", rel), find_ci(game, rel))
    for rel in state["copied"]:
        p = find_ci(game, rel)
        if os.path.exists(p):
            os.remove(p)
    for lang in state.get("gxt") or []:
        shutil.copy2(os.path.join(bdir, f"{lang}.gxt"), find_ci(game, f"text/{lang}.gxt"))
    baku_maps = find_ci(game, "data/maps/baku")
    if os.path.isdir(baku_maps) and not os.listdir(baku_maps):
        os.rmdir(baku_maps)
    shutil.rmtree(bdir)
    print("Baku removed, original San Andreas restored.")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("install", "uninstall", "status"):
        print(__doc__)
        sys.exit(1)
    cmd, game = sys.argv[1], sys.argv[2]
    if cmd == "install":
        install(game)
    elif cmd == "uninstall":
        uninstall(game)
    else:
        print("installed" if os.path.exists(os.path.join(game, BACKUP, "installed.json")) else "not installed")
