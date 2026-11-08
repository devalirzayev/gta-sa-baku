"""Terrain height field in game units.

Pipeline:
  1. Terrarium DEM (WebMercator z14 mosaic) resampled onto a game grid (cell = 2 units).
  2. Spike removal, morphological opening (removes building bumps in the SRTM-like DSM) and
     Gaussian smoothing.
  3. Water: Caspian sea, lakes (rasterised polygons) with seabed shaping; land near the sea
     is kept >= COAST_MIN above the water.
  4. Road integration (`integrate_roads`): ground road surfaces become Dirichlet constraints
     of a membrane problem  min |grad H|^2 + lam (H - DEM)^2 , solved with a multigrid-style
     Jacobi relaxation, so terrain blends smoothly into the road profiles.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import shapely
from scipy import ndimage

from .proj import game_to_lonlat, elev_to_game, WORLD

CELL = 2.0
N = int(2 * WORLD / CELL)
COAST_MIN = 1.2


@dataclass
class Terrain:
    h: np.ndarray            # (N,N) ground height, row = y index (south->north), col = x index
    sea: np.ndarray          # bool mask
    lake: np.ndarray         # int mask (0 = none, k = lake id)
    lake_levels: dict        # id -> water z

    @staticmethod
    def xy_to_ij(x, y):
        return (np.asarray(y) + WORLD) / CELL - 0.5, (np.asarray(x) + WORLD) / CELL - 0.5

    def sample(self, x, y, arr=None):
        arr = self.h if arr is None else arr
        fi, fj = self.xy_to_ij(x, y)
        return ndimage.map_coordinates(arr, [np.atleast_1d(fi), np.atleast_1d(fj)], order=1, mode="nearest")


def grid_xy():
    c = -WORLD + CELL * (np.arange(N) + 0.5)
    return c  # same for x and y


def load_dem_grid(path="data/raw/dem/terrarium_z14.npz") -> np.ndarray:
    d = np.load(path)
    e = d["elev"].astype(np.float64)
    z, tx0, ty0 = int(d["z"]), int(d["tx0"]), int(d["ty0"])
    # spike removal
    med = ndimage.median_filter(e, size=5)
    bad = np.abs(e - med) > 25
    e[bad] = med[bad]
    c = grid_xy()
    X, Y = np.meshgrid(c, c)  # rows = y
    lon, lat = game_to_lonlat(X.ravel(), Y.ravel())
    n = 2 ** z
    px = ((np.asarray(lon) + 180) / 360 * n - tx0) * 256 - 0.5
    py = ((1 - np.arcsinh(np.tan(np.radians(lat))) / math.pi) / 2 * n - ty0) * 256 - 0.5
    v = ndimage.map_coordinates(e, [py, px], order=1, mode="nearest")
    return v.reshape(N, N)


def rasterize(geoms, shape=(N, N), value=1, out=None):
    """Rasterise shapely polygons onto the game grid (cell centres)."""
    from PIL import Image, ImageDraw
    img = Image.new("I", (shape[1], shape[0]), 0) if out is None else Image.fromarray(out.astype(np.int32), "I")
    dr = ImageDraw.Draw(img)

    def to_px(coords):
        a = np.asarray(coords)
        return [((x + WORLD) / CELL - 0.5, (y + WORLD) / CELL - 0.5) for x, y in a]

    for g, val in (geoms if isinstance(geoms, list) and geoms and isinstance(geoms[0], tuple) else [(geoms, value)]):
        for p in getattr(g, "geoms", [g]):
            if p.is_empty or p.geom_type != "Polygon":
                continue
            dr.polygon(to_px(p.exterior.coords), fill=int(val))
            for hole in p.interiors:
                dr.polygon(to_px(hole.coords), fill=0)
    return np.asarray(img, dtype=np.int32)  # row 0 = y index 0 (south) because we used y index directly


def build_base(sea_poly, lakes) -> Terrain:
    """lakes: list of (polygon, water_z)."""
    raw = elev_to_game(load_dem_grid())
    sea = rasterize(sea_poly) > 0
    lake = np.zeros((N, N), np.int32)
    levels = {}
    for k, (poly, lvl) in enumerate(lakes, start=1):
        m = rasterize(poly) > 0
        lake[m & ~sea] = k
        levels[k] = lvl
    land = ~sea & (lake == 0)
    # remove building bumps: grey opening on land, then smooth
    h = raw.copy()
    h[~land] = np.nan
    fill = ndimage.distance_transform_edt(np.isnan(h), return_distances=False, return_indices=True)
    h = h[tuple(fill)]
    h = ndimage.grey_opening(h, size=(25, 25))
    h = ndimage.gaussian_filter(h, 8)
    # coast: keep land above water
    h[land] = np.maximum(h[land], COAST_MIN)
    # seabed: depth grows with distance from shore
    dsea = ndimage.distance_transform_edt(sea) * CELL
    h[sea] = -np.minimum(1.5 + dsea[sea] * 0.06, 22.0)
    for k, lvl in levels.items():
        m = lake == k
        dl = ndimage.distance_transform_edt(m) * CELL
        h[m] = lvl - np.minimum(0.8 + dl[m] * 0.05, 6.0)
        # lake shores at least 0.6 above the water
        ring = ndimage.binary_dilation(m, iterations=4) & ~m & land
        h[ring] = np.maximum(h[ring], lvl + 0.6)
    return Terrain(h, sea, lake, levels)


def membrane(h0, fixed_mask, fixed_val, lam=0.004, iters=(60, 80, 120, 200)):
    """Solve min |grad H|^2 + lam (H - h0)^2 with H = fixed_val on fixed_mask.
    Coarse-to-fine Jacobi relaxation (levels: /8, /4, /2, /1)."""
    def relax(H, F, M, V, lam_l, n):
        for _ in range(n):
            P = np.pad(H, 1, mode="edge")
            nb = P[:-2, 1:-1] + P[2:, 1:-1] + P[1:-1, :-2] + P[1:-1, 2:]
            H = (nb + lam_l * F) / (4 + lam_l)
            H[M] = V[M]
        return H

    def down(a, f, mode="mean"):
        s = (a.shape[0] // f, a.shape[1] // f)
        a = a[:s[0] * f, :s[1] * f].reshape(s[0], f, s[1], f)
        return a.mean(axis=(1, 3)) if mode == "mean" else a.any(axis=(1, 3))

    H = None
    for lvl, n in zip((8, 4, 2, 1), iters):
        F = down(h0, lvl) if lvl > 1 else h0
        if lvl > 1:
            Mf = down(fixed_mask.astype(float), lvl)
            M = Mf > 0.5
            Vs = down(np.where(fixed_mask, fixed_val, 0.0), lvl)
            V = np.where(Mf > 0, Vs / np.maximum(Mf, 1e-9), 0)
        else:
            M, V = fixed_mask, fixed_val
        lam_l = lam * lvl * lvl
        if H is None:
            H = F.copy()
        else:
            H = np.kron(H, np.ones((2, 2)))[:F.shape[0], :F.shape[1]]
            if H.shape != F.shape:
                H = np.pad(H, ((0, F.shape[0] - H.shape[0]), (0, F.shape[1] - H.shape[1])), mode="edge")
        H = relax(H, F, M, V, lam_l, n)
    return H
