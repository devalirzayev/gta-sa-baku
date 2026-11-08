"""Seamless (periodic) procedural noise helpers for texture generation."""
import numpy as np


def value_noise(size, cells, rng, octaves=5, persistence=0.5):
    """Periodic fractal value noise in [0,1], shape (size,size)."""
    out = np.zeros((size, size))
    amp, tot = 1.0, 0.0
    c = cells
    for _ in range(octaves):
        if c > size:
            break
        lat = rng.random((c, c))
        xs = np.arange(size) * c / size
        i0 = np.floor(xs).astype(int)
        f = xs - i0
        f = f * f * (3 - 2 * f)
        i1 = (i0 + 1) % c
        a = lat[np.ix_(i0, i0)]
        b = lat[np.ix_(i0, i1)]
        cc = lat[np.ix_(i1, i0)]
        d = lat[np.ix_(i1, i1)]
        fy = f[:, None]
        fx = f[None, :]
        layer = (a * (1 - fx) + b * fx) * (1 - fy) + (cc * (1 - fx) + d * fx) * fy
        out += layer * amp
        tot += amp
        amp *= persistence
        c *= 2
    return out / tot


def grain(size, rng, strength=1.0):
    return (rng.random((size, size)) - 0.5) * strength


def blur_periodic(a, sigma):
    from scipy import ndimage
    return ndimage.gaussian_filter(a, sigma, mode="wrap")


def to_img(rgb):
    from PIL import Image
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
