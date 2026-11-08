"""Round-trip tests for tools/rw writers (own reader + optional DragonFF reader)."""
import os, sys, io
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from PIL import Image
from tools.rw import dff, txd, col, img

def box_geometry():
    # unit box 4x4x3 with 2 materials
    v = np.array([[x, y, z] for z in (0, 3) for y in (-2, 2) for x in (-2, 2)], dtype=np.float64)
    # faces CCW from outside
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    tris = []
    for a, b, c, d in quads:
        tris += [(a, b, c), (a, c, d)]
    tris = np.array(tris)
    uv = np.c_[v[:, 0] / 4 + 0.5, 1 - v[:, 2] / 3]
    mats = [dff.Material("bk_wall"), dff.Material("bk_roof")]
    mid = np.array([1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0][:len(tris)])
    pre = np.full((len(v), 4), 200, np.uint8); pre[:, 3] = 255
    night = (pre * 0.4).astype(np.uint8); night[:, 3] = 255
    return dff.Geometry(v, uv, tris, mid, mats, pre, night,
                        effects=[dff.light_2dfx((0, 0, 3.2))])

def test_dff():
    g = box_geometry()
    b = dff.write_dff("bk_testbox", g)
    r = dff.read_dff(b)
    assert r["frames"][0]["name"] == "bk_testbox"
    assert len(r["atomics"]) == 1 and r["atomics"][0]["pipeline"] == 0x53F20098
    gg = r["geometries"][0]
    assert np.allclose(gg["vertices"], g.vertices)
    assert (gg["triangles"] == g.triangles).all()
    assert (gg["night"] == g.night).all() and (gg["prelit"] == g.prelit).all()
    assert [m["texture"] for m in gg["materials"]] == ["bk_wall", "bk_roof"]
    assert len(gg["effects"]) == 1 and len(gg["effects"][0][2]) == 0x50
    fl, meshes, total = gg["binmesh"]
    assert fl == 0 and total == 3 * len(g.triangles)
    return b

def test_txd():
    a = (np.random.RandomState(1).rand(64, 128, 4) * 255).astype(np.uint8); a[..., 3] = 255
    im = Image.fromarray(a, "RGBA")
    b2 = np.array(im); b2[:32, :, 3] = 0
    t = txd.write_txd([("bk_wall", im), ("bk_sign", Image.fromarray(b2, "RGBA")), ("bk_raw", im, "8888")])
    r = txd.read_txd(t, decode=True)
    assert [x["name"] for x in r] == ["bk_wall", "bk_sign", "bk_raw"]
    assert r[0]["d3d_format"] == txd.FOURCC_DXT1 and r[1]["d3d_format"] == txd.FOURCC_DXT5
    assert r[0]["levels"] == 5  # 128x64 -> 64x32 -> 32x16 -> 16x8 -> 8x4
    err = np.abs(np.asarray(r[2]["image"], np.int16) - a.astype(np.int16)).max()
    assert err == 0, err
    return t

def test_col():
    g = box_geometry()
    rs = np.random.RandomState(2)
    # big terrain-like mesh to trigger face groups
    n = 40
    xs, ys = np.meshgrid(np.linspace(-100, 100, n), np.linspace(-100, 100, n))
    zs = rs.rand(n, n) * 5
    v = np.c_[xs.ravel(), ys.ravel(), zs.ravel()]
    f = []
    for j in range(n - 1):
        for i in range(n - 1):
            a = j * n + i
            f += [(a, a + 1, a + n + 1), (a, a + n + 1, a + n)]
    f = np.array(f)
    m = col.ColModel("bk_testbox", 15100, v, f, np.full(len(f), col.SURF["GRASS_SHORT_LUSH"]),
                     np.full(len(f), col.light_byte(15, 8)), boxes=[((-1, -1, 0), (1, 1, 2), 4)],
                     shadow_vertices=g.vertices, shadow_faces=g.triangles)
    b = col.write_col3(m)
    r = col.read_col(b + b)
    assert len(r) == 2 and r[0]["name"] == "bk_testbox" and r[0]["model_id"] == 15100
    assert r[0]["flags"] == 2 | 8 | 16
    assert len(r[0]["faces"]) == len(f)
    gr = r[0]["groups"]
    assert gr[0][6] == 0 and gr[-1][7] == len(f) - 1
    for i in range(1, len(gr)):
        assert gr[i][6] == gr[i - 1][7] + 1
    # every face inside its group bbox
    vq = r[0]["vertices"]
    for g6 in gr:
        fv = vq[r[0]["faces"][g6[6]:g6[7] + 1].ravel()]
        assert (fv >= np.array(g6[0:3]) - 1e-4).all() and (fv <= np.array(g6[3:6]) + 1e-4).all()
    return b

def test_img(tmp="/tmp/claude-0/-home-user-ila/5b6b71aa-4af7-540b-9036-4dec5b516689/scratchpad/test.img"):
    files = [("bk_testbox.dff", test_dff()), ("bk_test.txd", test_txd()), ("bk_test.col", test_col())]
    img.write_img(tmp, files)
    back = img.read_img(tmp)
    for (n1, d1), (n2, d2) in zip(files, back):
        assert n1 == n2 and d2[:len(d1)] == d1
    return tmp

if __name__ == "__main__":
    for fn in (test_dff, test_txd, test_col, test_img):
        fn(); print("ok", fn.__name__)
