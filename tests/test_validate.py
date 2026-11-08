"""Tests for the engine-limit simulations in tools/validate.py."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np

from tools import validate as V
from tools.gta import ipl as IPL


def test_stream_load_peaks_overlap():
    # three IPL boxes: A and B overlap, C is apart; the peak is the A+B overlap
    stats = {"a": ((0, 0, 100, 100), {"n": 5}), "b": ((50, 50, 200, 200), {"n": 7}),
             "c": ((500, 500, 600, 600), {"n": 9})}
    pk = V.stream_load_peaks(stats)
    n, (x, y), names = pk["n"]
    assert n == 12 and sorted(names) == ["a", "b"]
    assert 50 <= x <= 100 and 50 <= y <= 100


def test_stream_load_peaks_3x3_cells():
    # 500-unit cells inflated by 350 -> the centre of a cell sees its 8 neighbours too
    stats = {}
    for i in range(4):
        for j in range(4):
            x0, y0 = i * 500.0, j * 500.0
            stats[f"c{i}{j}"] = ((x0 - 350, y0 - 350, x0 + 500 + 350, y0 + 500 + 350), {"n": 1})
    assert V.stream_load_peaks(stats)["n"][0] == 9


def test_bound_rects_rotation():
    aabb = lambda mid: ((-1.0, -3.0, 0.0), (1.0, 3.0, 1.0))      # 2 x 6 footprint
    i0 = IPL.Inst(1, "m", 10.0, 20.0, 0.0, heading=0.0)
    i90 = IPL.Inst(1, "m", 10.0, 20.0, 0.0, heading=math.pi / 2)
    r = V._bound_rects([i0, i90], aabb)
    assert np.allclose(r[0], (9, 17, 11, 23))
    assert np.allclose(r[1], (7, 19, 13, 21), atol=1e-9)          # long axis now along x


def test_sectors():
    r = np.array([[0.0, 0.0, 10.0, 10.0], [-10.0, -10.0, 10.0, 10.0], [-5000, -5000, 5000, 5000]])
    assert list(V._sectors(r)) == [1, 4, 120 * 120]


def test_point_triangle_distance():
    rng = np.random.default_rng(3)
    for _ in range(100):
        A, B, C = rng.normal(size=(3, 3))
        p = rng.normal(size=3) * 2
        d = V._point_tri_dist(p, A[None], B[None], C[None])[0]
        u, v = rng.random((2, 100000))
        m = u + v <= 1
        S = A + np.outer(u[m], B - A) + np.outer(v[m], C - A)
        ds = np.sqrt(((S - p) ** 2).sum(1)).min()
        assert d <= ds + 1e-9 and ds - d < 0.03


def test_col_tri_peak_exact_flat_grid():
    # flat grid of 1x1 squares (2 triangles each): within r of a point ~ 2 * pi * r^2 triangles
    n = 60
    xs, ys = np.meshgrid(np.arange(n + 1.0), np.arange(n + 1.0))
    Vt = np.c_[xs.ravel(), ys.ravel(), np.zeros(xs.size)]
    k = np.arange(n * (n + 1)).reshape(n, n + 1)[:, :n].ravel()
    Fc = np.r_[np.c_[k, k + 1, k + n + 2], np.c_[k, k + n + 2, k + n + 1]]
    rec = {"vertices": Vt, "faces": Fc}
    r = 5.0
    got = V.col_tri_peak_exact(rec, r, probes=4000)
    # sphere centre 1 unit above the plane: disk radius sqrt(r^2 - 1) plus boundary triangles
    disk = 2 * math.pi * (r * r - 1)
    assert disk <= got <= 2 * math.pi * (math.sqrt(r * r - 1) + 1.5) ** 2
    assert V.col_tri_peak(rec, radius=r) >= got                    # the fast estimate is an upper bound


def test_visible_peak_wedge():
    # 100 entities in a narrow cluster east of the origin and 100 to the west: one camera
    # heading sees only one cluster with a 90 degree field of view
    rng = np.random.default_rng(0)
    east = np.c_[rng.uniform(80, 120, 100), rng.uniform(-10, 10, 100)]
    west = np.c_[rng.uniform(-120, -80, 100), rng.uniform(-10, 10, 100)]
    xy = np.r_[east, west]
    n, p, h = V.visible_peak(xy, np.full(len(xy), 150.0), step=10.0, half_fov=45.0)
    assert n == 100
    n2, _, _ = V.visible_peak(xy, np.full(len(xy), 150.0), step=10.0, half_fov=180.0)
    assert n2 == 200


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
            print("ok", k)
