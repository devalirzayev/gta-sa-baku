"""Procedural textures for the Baku landmark models (TXD bk_lmtx)."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter



def _noise(size, scale, seed, amp):
    rng = np.random.default_rng(seed)
    n = rng.random((size // scale + 2, size // scale + 2))
    img = Image.fromarray((n * 255).astype(np.uint8)).resize((size, size), Image.BICUBIC)
    return (np.asarray(img, np.float64) / 255.0 - 0.5) * amp


def white_panels(size=256):
    """Heydar Aliyev Centre shell: white GFRC panels with fine joints."""
    a = np.full((size, size, 3), 236.0)
    a += _noise(size, 16, 1, 10)[:, :, None]
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for k in range(0, size, 64):
        d.line([(0, k), (size, k)], fill=(214, 214, 212), width=1)
    for k in range(0, size, 128):
        d.line([(k, 0), (k, size)], fill=(220, 220, 218), width=1)
    return im


def glass_dark(size=256):
    """Dark glazing with mullions (HAC glass walls, podiums)."""
    a = np.zeros((size, size, 3))
    a[:] = (58, 72, 88)
    g = np.linspace(0, 1, size)[:, None]
    a += (g * 30)[:, :, None] * np.array([0.6, 0.8, 1.0])
    a += _noise(size, 32, 2, 14)[:, :, None]
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for k in range(0, size, 32):
        d.line([(k, 0), (k, size)], fill=(150, 156, 162), width=2)
    for k in range(0, size, 64):
        d.line([(0, k), (size, k)], fill=(150, 156, 162), width=3)
    return im


def flame_glass(size=256):
    """Flame Towers curtain wall: light blue reflective glass, floor bands (one floor = 32 px)."""
    a = np.zeros((size, size, 3))
    a[:] = (168, 196, 222)
    a += _noise(size, 64, 3, 26)[:, :, None] * np.array([0.8, 0.9, 1.0])
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for k in range(0, size, 32):
        d.rectangle([0, k, size, k + 3], fill=(205, 214, 224))      # spandrel band
    for k in range(0, size, 16):
        d.line([(k, 0), (k, size)], fill=(140, 160, 182), width=1)
    return im


def stone(size=256, base=(214, 196, 160), seed=4, block=(32, 16)):
    """Limestone ashlar (Absheron 'gilabi' stone): Maiden Tower, mosque walls."""
    a = np.zeros((size, size, 3))
    a[:] = base
    a += _noise(size, 8, seed, 22)[:, :, None]
    a += _noise(size, 48, seed + 1, 16)[:, :, None]
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    bw, bh = block
    for r, y in enumerate(range(0, size, bh)):
        d.line([(0, y), (size, y)], fill=tuple(int(c * 0.78) for c in base), width=1)
        off = (r % 2) * bw // 2
        for x in range(off, size + bw, bw):
            d.line([(x, y), (x, y + bh)], fill=tuple(int(c * 0.8) for c in base), width=1)
    return im.filter(ImageFilter.SMOOTH)


def dome_metal(size=256):
    """Blue-grey metal dome cladding with vertical seams."""
    a = np.zeros((size, size, 3))
    a[:] = (112, 140, 160)
    a += _noise(size, 32, 5, 12)[:, :, None]
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for k in range(0, size, 16):
        d.line([(k, 0), (k, size)], fill=(92, 118, 138), width=2)
    return im


def gold(size=64):
    a = np.zeros((size, size, 3))
    a[:] = (212, 172, 72)
    a += _noise(size, 8, 6, 18)[:, :, None]
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def arch_windows(size=256):
    """Mosque wall with tall arched windows (2 per tile)."""
    im = stone(size, base=(222, 210, 184), seed=7, block=(64, 32))
    d = ImageDraw.Draw(im)
    for cx in (64, 192):
        d.rectangle([cx - 22, 70, cx + 22, 220], fill=(52, 62, 76))
        d.pieslice([cx - 22, 48, cx + 22, 92], 180, 360, fill=(52, 62, 76))
        d.line([(cx, 70), (cx, 220)], fill=(180, 170, 150), width=2)
    return im


TEXTURES = {
    "lm_white": white_panels, "lm_glass": glass_dark, "lm_flame": flame_glass,
    "lm_stone": lambda: stone(), "lm_dome": dome_metal, "lm_gold": gold, "lm_mosque": arch_windows,
}
