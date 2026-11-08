"""Projection from WGS84 lon/lat (and metres of elevation) into SA game units.

Real metres are mapped with a local transverse Mercator centred on the area of the reference
screenshot, then scaled by SCALE so that the whole area (~8.9 x 8.7 km) fits the SA world
(+-3000 units). Elevations are scaled by the same factor (keeps slopes realistic) and shifted so
that the Caspian surface (-28 m) is game Z 0.
"""
import numpy as np
from pyproj import Transformer

# area of the reference screenshot (lon/lat)
AOI = dict(W=49.7877, E=49.8927, S=40.3586, N=40.4369)
LON0 = (AOI["W"] + AOI["E"]) / 2
LAT0 = (AOI["S"] + AOI["N"]) / 2
SCALE = 0.675           # game units per real metre (horizontal and vertical)
SEA_LEVEL_M = -27.5     # Caspian surface (DEM value) -> game z 0
WORLD = 3000.0

_fwd = Transformer.from_crs("EPSG:4326",
                            f"+proj=tmerc +lat_0={LAT0} +lon_0={LON0} +k=1 +x_0=0 +y_0=0 +ellps=WGS84 +units=m",
                            always_xy=True)
_inv = Transformer.from_crs(f"+proj=tmerc +lat_0={LAT0} +lon_0={LON0} +k=1 +x_0=0 +y_0=0 +ellps=WGS84 +units=m",
                            "EPSG:4326", always_xy=True)


def lonlat_to_m(lon, lat):
    return _fwd.transform(lon, lat)


def lonlat_to_game(lon, lat):
    x, y = _fwd.transform(lon, lat)
    return np.asarray(x) * SCALE, np.asarray(y) * SCALE


def game_to_lonlat(x, y):
    return _inv.transform(np.asarray(x) / SCALE, np.asarray(y) / SCALE)


def elev_to_game(e):
    return (np.asarray(e) - SEA_LEVEL_M) * SCALE


def game_to_elev(z):
    return np.asarray(z) / SCALE + SEA_LEVEL_M
