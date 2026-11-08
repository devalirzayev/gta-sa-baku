"""Procedural textures for Azerbaijani road signs (Vienna convention, AZS / Yol hərəkəti haqqında qanun)
and helpers for text rendering (shop boards, street-name plates).

All faces are drawn 4x supersampled and reduced to 256 x 256.  The sign plate geometry has the
sign's outline (octagon, triangle, circle, ...), so the texture only has to be correct inside it.
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFont

S = 1024                      # supersampled canvas
OUT = 256
FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

RED = (200, 16, 32)
BLUE = (16, 72, 160)
WHITE = (250, 250, 248)
BLACK = (18, 18, 20)
YELLOW = (250, 196, 0)
GREY = (160, 164, 168)


def font(name, size):
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


def az_upper(s: str) -> str:
    """Azerbaijani upper case (i -> İ, ı -> I)."""
    return s.replace("i", "İ").replace("ı", "I").upper()


def _new(bg=WHITE):
    im = Image.new("RGB", (S, S), bg)
    return im, ImageDraw.Draw(im)


def _done(im):
    return im.resize((OUT, OUT), Image.LANCZOS)


def _regular_polygon(n, r, rot, cx=S / 2, cy=S / 2):
    return [(cx + r * math.cos(rot + 2 * math.pi * k / n), cy + r * math.sin(rot + 2 * math.pi * k / n))
            for k in range(n)]


def _text_center(d, text, fnt, cx, cy, fill):
    l, t, r, b = d.textbbox((0, 0), text, font=fnt)
    d.text((cx - (l + r) / 2, cy - (t + b) / 2), text, font=fnt, fill=fill)


def _arrow(d, x0, y0, x1, y1, w, head, fill):
    ang = math.atan2(y1 - y0, x1 - x0)
    bx, by = x1 - head * math.cos(ang), y1 - head * math.sin(ang)
    d.line([(x0, y0), (bx, by)], fill=fill, width=w)
    px, py = -math.sin(ang), math.cos(ang)
    d.polygon([(x1, y1), (bx + px * head * 0.62, by + py * head * 0.62), (bx - px * head * 0.62, by - py * head * 0.62)],
              fill=fill)


# ------------------------------------------------------------------------------------------ faces
def face_stop():
    im, d = _new(WHITE)
    d.polygon(_regular_polygon(8, S * 0.5, math.pi / 8), fill=WHITE)
    d.polygon(_regular_polygon(8, S * 0.455, math.pi / 8), fill=RED)
    _text_center(d, "STOP", font("Inter-Black.otf", 285), S / 2, S / 2, WHITE)
    return _done(im)


def face_giveway():
    """Inverted triangle; texture square maps to the triangle's bounding box."""
    im, d = _new(WHITE)
    d.polygon([(0, 0), (S, 0), (S / 2, S)], fill=RED)
    m = 0.17
    d.polygon([(S * m * 1.15, S * m * 0.55), (S * (1 - m * 1.15), S * m * 0.55), (S / 2, S * (1 - m * 0.98))], fill=WHITE)
    return _done(im)


def face_ped():
    """5.19 'Piyada keçidi': blue square, white triangle, black walking figure on a zebra."""
    im, d = _new(BLUE)
    d.rectangle([20, 20, S - 20, S - 20], outline=WHITE, width=26)
    tri = [(S / 2, S * 0.12), (S * 0.9, S * 0.86), (S * 0.1, S * 0.86)]
    d.polygon(tri, fill=WHITE)
    # zebra
    for k in range(5):
        x = S * 0.24 + k * S * 0.11
        d.polygon([(x, S * 0.80), (x + S * 0.06, S * 0.80), (x + S * 0.03, S * 0.73), (x - S * 0.03, S * 0.73)], fill=BLACK)
    # figure
    cx = S * 0.5
    d.ellipse([cx - 52, S * 0.27 - 52, cx + 52, S * 0.27 + 52], fill=BLACK)
    d.line([(cx, S * 0.34), (cx - 22, S * 0.52)], fill=BLACK, width=70)               # torso
    d.line([(cx - 22, S * 0.52), (cx - 120, S * 0.69)], fill=BLACK, width=48)         # back leg
    d.line([(cx - 22, S * 0.52), (cx + 70, S * 0.60), (cx + 90, S * 0.70)], fill=BLACK, width=48, joint="curve")
    d.line([(cx - 4, S * 0.38), (cx + 100, S * 0.47)], fill=BLACK, width=40)          # front arm
    d.line([(cx - 10, S * 0.38), (cx - 110, S * 0.46)], fill=BLACK, width=40)         # back arm
    return _done(im)


def face_noentry():
    im, d = _new(WHITE)
    d.ellipse([0, 0, S, S], fill=WHITE)
    d.ellipse([22, 22, S - 22, S - 22], fill=RED)
    d.rectangle([S * 0.17, S * 0.415, S * 0.83, S * 0.585], fill=WHITE)
    return _done(im)


def face_speed(n):
    im, d = _new(WHITE)
    d.ellipse([0, 0, S, S], fill=RED)
    d.ellipse([S * 0.11, S * 0.11, S * 0.89, S * 0.89], fill=WHITE)
    _text_center(d, str(n), font("Inter-Bold.otf", 400 if n < 100 else 300), S / 2, S / 2 + 6, BLACK)
    return _done(im)


def face_priority():
    """2.1 'Əsas yol': yellow diamond with a white border (texture = diamond's bounding box)."""
    im, d = _new(WHITE)
    c = S / 2
    d.polygon([(c, 0), (S, c), (c, S), (0, c)], fill=BLACK)
    d.polygon([(c, 14), (S - 14, c), (c, S - 14), (14, c)], fill=WHITE)
    d.polygon([(c, S * 0.2), (S * 0.8, c), (c, S * 0.8), (S * 0.2, c)], fill=YELLOW)
    return _done(im)


def face_roundabout():
    im, d = _new(BLUE)
    d.ellipse([0, 0, S, S], fill=WHITE)
    d.ellipse([18, 18, S - 18, S - 18], fill=BLUE)
    r = S * 0.25
    for k in range(3):
        a0 = math.radians(k * 120 + 200)
        a1 = a0 + math.radians(80)
        pts = [(S / 2 + r * math.cos(a0 + (a1 - a0) * t / 12), S / 2 + r * math.sin(a0 + (a1 - a0) * t / 12)) for t in range(13)]
        d.line(pts, fill=WHITE, width=70, joint="curve")
        ex, ey = pts[-1]
        tx, ty = -math.sin(a1), math.cos(a1)
        nx, ny = math.cos(a1), math.sin(a1)
        d.polygon([(ex + tx * 95, ey + ty * 95), (ex + nx * 80, ey + ny * 80), (ex - nx * 80, ey - ny * 80)], fill=WHITE)
    return _done(im)


def face_nostop():
    """3.27 'Dayanmaq qadağandır': blue disc, red ring, red X."""
    im, d = _new(WHITE)
    d.ellipse([0, 0, S, S], fill=RED)
    d.ellipse([S * 0.12, S * 0.12, S * 0.88, S * 0.88], fill=BLUE)
    w = 95
    d.line([(S * 0.24, S * 0.24), (S * 0.76, S * 0.76)], fill=RED, width=w)
    d.line([(S * 0.76, S * 0.24), (S * 0.24, S * 0.76)], fill=RED, width=w)
    return _done(im)


def face_bus():
    """5.16 'Avtobus dayanacağı': blue plate, white bus silhouette, 'AVTOBUS'."""
    im, d = _new(BLUE)
    d.rectangle([16, 16, S - 16, S - 16], outline=WHITE, width=24)
    d.rounded_rectangle([S * 0.16, S * 0.18, S * 0.84, S * 0.64], radius=60, fill=WHITE)
    for k in range(4):
        x = S * 0.21 + k * S * 0.155
        d.rectangle([x, S * 0.25, x + S * 0.12, S * 0.40], fill=BLUE)
    d.rectangle([S * 0.21, S * 0.47, S * 0.79, S * 0.50], fill=BLUE)
    for x in (S * 0.30, S * 0.70):
        d.ellipse([x - 62, S * 0.60, x + 62, S * 0.60 + 124], fill=BLUE)
        d.ellipse([x - 42, S * 0.62, x + 42, S * 0.62 + 84], fill=WHITE)
    _text_center(d, "AVTOBUS", font("Inter-Black.otf", 150), S / 2, S * 0.84, WHITE)
    return _done(im)


def face_oneway():
    im, d = _new(BLUE)
    d.rectangle([16, 16, S - 16, S - 16], outline=WHITE, width=24)
    _arrow(d, S / 2, S * 0.86, S / 2, S * 0.14, 150, 300, WHITE)
    return _done(im)


def face_metro():
    """Baku Metro: red 'M' on white."""
    im, d = _new(WHITE)
    d.ellipse([0, 0, S, S], fill=RED)
    d.ellipse([S * 0.07, S * 0.07, S * 0.93, S * 0.93], fill=WHITE)
    _text_center(d, "M", font("Inter-Black.otf", 640), S / 2, S / 2 + 10, RED)
    return _done(im)


def face_parking():
    im, d = _new(BLUE)
    d.rectangle([16, 16, S - 16, S - 16], outline=WHITE, width=24)
    _text_center(d, "P", font("Inter-Black.otf", 700), S / 2, S / 2 + 10, WHITE)
    return _done(im)


def back_metal():
    im = Image.new("RGB", (64, 64), (138, 142, 146))
    d = ImageDraw.Draw(im)
    for y in range(0, 64, 4):
        d.line([(0, y), (63, y)], fill=(128, 132, 137))
    return im


def pole_metal():
    im = Image.new("RGB", (32, 64), (150, 154, 158))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 7, 63], fill=(120, 124, 128))
    d.rectangle([24, 0, 31, 63], fill=(176, 180, 184))
    return im


FACES = {
    "stop": face_stop, "giveway": face_giveway, "ped": face_ped, "noentry": face_noentry,
    "speed40": lambda: face_speed(40), "speed60": lambda: face_speed(60), "speed80": lambda: face_speed(80),
    "priority": face_priority, "roundabout": face_roundabout, "nostop": face_nostop, "bus": face_bus,
    "oneway": face_oneway, "metro": face_metro, "parking": face_parking,
}


def sheet(path):
    """Contact sheet of all faces (for docs / review)."""
    names = list(FACES)
    cols = 7
    rows = (len(names) + cols - 1) // cols
    out = Image.new("RGB", (cols * (OUT + 8) + 8, rows * (OUT + 8) + 8), (60, 64, 70))
    for k, n in enumerate(names):
        out.paste(FACES[n](), (8 + (k % cols) * (OUT + 8), 8 + (k // cols) * (OUT + 8)))
    out.save(path)


if __name__ == "__main__":
    import sys
    sheet(sys.argv[1] if len(sys.argv) > 1 else "build/tex/road_signs.png")
