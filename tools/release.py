"""Pack dist/Baku_SA into a zip split into < 95 MB parts that fit into the git repository.

    python3 -m tools.release [version]
Writes release/Baku_SA.zip.001, .002, ... (7-Zip opens .001 directly), release/SHA256SUMS.txt,
release/JOIN_PARTS.bat (Windows: copy /b to one zip) and release/README.txt.
"""
from __future__ import annotations

import hashlib
import os
import sys
import zipfile

SRC = "dist/Baku_SA"
OUT = "release"
PART = 95 * 1024 * 1024


def main(version="1.0"):
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        if f.startswith("Baku_SA.zip"):
            os.remove(os.path.join(OUT, f))
    zpath = os.path.join(OUT, "Baku_SA.zip")
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, _, files in os.walk(SRC):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                arc = os.path.join("Baku_SA", os.path.relpath(full, SRC))
                z.write(full, arc)
    data_len = os.path.getsize(zpath)
    parts = []
    with open(zpath, "rb") as f:
        k = 1
        while True:
            chunk = f.read(PART)
            if not chunk:
                break
            name = f"Baku_SA.zip.{k:03d}"
            with open(os.path.join(OUT, name), "wb") as g:
                g.write(chunk)
            parts.append((name, hashlib.sha256(chunk).hexdigest(), len(chunk)))
            k += 1
    full_hash = hashlib.sha256(open(zpath, "rb").read()).hexdigest()
    os.remove(zpath)
    with open(os.path.join(OUT, "SHA256SUMS.txt"), "w", newline="\r\n") as f:
        f.write(f"{full_hash}  Baku_SA.zip (joined, {data_len} bytes)\n")
        for name, h, n in parts:
            f.write(f"{h}  {name}\n")
    with open(os.path.join(OUT, "JOIN_PARTS.bat"), "w", newline="\r\n") as f:
        f.write("@echo off\n")
        f.write("rem Joins the parts into Baku_SA.zip (or open Baku_SA.zip.001 with 7-Zip directly)\n")
        f.write("cd /d \"%~dp0\"\n")
        f.write("copy /b " + "+".join(n for n, _, _ in parts) + " Baku_SA.zip\n")
        f.write("echo Baku_SA.zip hazirdir / is ready.\n")
        f.write("pause\n")
    with open(os.path.join(OUT, "README.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(f"BAKU for GTA San Andreas - v{version}\n\n")
        f.write("AZ: Bütün hissələri (Baku_SA.zip.001, .002 ...) yükləyin, JOIN_PARTS.bat ilə birləşdirin\n")
        f.write("    (və ya .001 faylını 7-Zip ilə açın), Baku_SA qovluğunu çıxarın və INSTALL.bat işlədin.\n\n")
        f.write("EN: Download all parts (Baku_SA.zip.001, .002 ...), join them with JOIN_PARTS.bat\n")
        f.write("    (or open the .001 file with 7-Zip), extract the Baku_SA folder and run INSTALL.bat.\n\n")
        f.write("SHA-256 sums: SHA256SUMS.txt\n")
    print(f"release: {len(parts)} parts, {data_len / 1e6:.1f} MB zip")


if __name__ == "__main__":
    main(*(sys.argv[1:] or []))
