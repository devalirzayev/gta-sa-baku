"""Tests for tools/gta data-file writers: water.dat, info.zon/map.zon (+ text), tracks*.dat, radar.

Run: python3 tests/test_data_files.py
Optional vanilla round-trip checks: set SA_DATA_REF to a directory holding vanilla water.dat,
info.zon, map.zon and tracks.dat/tracks2.dat/tracks3.dat/tracks4.dat.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PIL import Image

from tools.gta import water as W
from tools.gta import zones as Z
from tools.gta import tracks as T
from tools.gta import radar as R


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        return str(e)
    raise AssertionError(f"{fn.__name__} did not raise {exc.__name__}")


# --- water.dat -------------------------------------------------------------------------------
def sample_water():
    sea = W.quad(-3000, -3000, 3000, -1000, 0.0, big_waves=0.4, small_waves=0.24)   # touches 3 edges
    bay = W.quad(-1000, -1000, 1000, 0, 0.0, big_waves=0.4, small_waves=0.24)       # shares 2 verts
    lake = W.quad(200, 400, 400, 600, 22.0, big_waves=0.05, small_waves=0.1, limited_depth=True)
    tri = W.triangle(1000, 0, -200, 200, 0.0, big_waves=0.4, small_waves=0.24)      # 45 deg corner
    hidden = W.quad(-100, 1000, 100, 1200, 5.0, visible=False)
    return [sea, bay, lake, tri, hidden]


def test_water_roundtrip():
    polys = sample_water()
    text = W.format_water_dat(polys)
    lines = text.splitlines()
    assert lines[0] == "processed" and len(lines) == 6
    assert lines[1].startswith("-3000.0 -3000.0 0.00000 0.00000 0.00000 0.40000 0.24000    3000.0 ")
    assert lines[1].endswith("  1") and lines[3].endswith("  3") and lines[5].endswith("  0")
    back = W.parse_water_dat(text)
    assert [p.flags for p in back] == [1, 1, 3, 1, 0]
    assert [[(v.x, v.y, v.z) for v in p.verts] for p in back] == \
        [[(v.x, v.y, W.f32(v.z)) for v in p.verts] for p in polys]
    assert [[(v.big_waves, v.small_waves) for v in p.verts] for p in back] == \
        [[(W.f32(v.big_waves), W.f32(v.small_waves)) for v in p.verts] for p in polys]
    assert W.format_water_dat(back) == text                       # stable re-format
    st = W.validate_water(back)
    gw = W.GameWater(back)
    # sea 4 (all clamped edge vertices) + bay 4 + lake 4 + triangle 2 (shares (1000,0)) + hidden 4
    assert (st.quads, st.triangles, st.vertices) == (4, 1, 18) == (len(gw.quads), len(gw.triangles), len(gw.vertices))
    assert gw.vertices[gw.quads[0].idx[0]].big_waves == 1.0      # edge vertex forced to bigWaves 1
    # in-water tests
    assert gw.level_at(0, -2000, 0) == 0.0                 # sea
    assert gw.level_at(500, 500, 0) is None                 # land (lake is 22 m higher, z far below)
    assert abs(gw.level_at(300, 500, 21) - 22.0) < 1e-6     # in the lake
    assert gw.level_at(300, 500, 10) is None                # limited depth: > 6 m below surface
    assert gw.level_at(300, 500, 43) is None                # > 20 m above the surface
    assert gw.level_at(990, 5, 0) == 0.0                    # inside triangle near right angle
    assert gw.level_at(850, 190, 0) is None                 # beyond the hypotenuse
    assert gw.level_at(0, 1100, 4) == 5.0                   # invisible water still is water
    assert gw.level_at(5000, 5000, 0) == 0.0                # endless sea outside +-3000
    assert gw.level_at(2999, 2999, 0) is None               # inside the grid, no poly


def test_water_rules():
    ok = W.quad(0, 0, 100, 100)
    raises(W.WaterError, W.validate_water, [W.quad(0, 0, 101, 100)])               # odd coordinate
    raises(W.WaterError, W.validate_water, [W.quad(2000, 2000, 3000, 2400, 5.0)])  # edge with z != 0
    raises(W.WaterError, W.validate_water, [W.quad(0, 0, 0, 100)])                 # degenerate
    raises(W.WaterError, W.validate_water, [W.WaterPoly((W.WaterVertex(0, 0), W.WaterVertex(10, 0),
                                                         W.WaterVertex(0, 10), W.WaterVertex(10, 12)))])
    raises(W.WaterError, W.validate_water, [W.triangle(0, 0, 100, 60)])            # not 45 degrees
    bad_tri = W.WaterPoly((W.WaterVertex(0, 0), W.WaterVertex(100, 0), W.WaterVertex(50, 100)))
    raises(W.WaterError, W.validate_water, [bad_tri])                              # no right angle
    raises(W.WaterError, W.validate_water, [W.quad(0, 0, 100, 100, flow=(3.0, 0))])
    # limits
    many = [W.quad(-3000 + 20 * (i % 290), -2000 + 20 * (i // 290), -2990 + 20 * (i % 290),
                   -1990 + 20 * (i // 290)) for i in range(302)]
    msg = raises(W.WaterError, W.validate_water, many)
    assert "302 quads > 301" in msg, msg
    tris = [W.triangle(-2000 + 300 * i, 0, 100, 100) for i in range(7)]
    assert "7 triangles > 6" in raises(W.WaterError, W.validate_water, tris)
    # 300 separate quads in one corner -> 1200 vertices
    sep = [W.quad(-2990 + 20 * (i % 20), -2990 + 20 * (i // 20), -2980 + 20 * (i % 20),
                  -2980 + 20 * (i // 20)) for i in range(300)]
    msg = raises(W.WaterError, W.validate_water, sep)
    assert "1200 unique vertices > 1021" in msg, msg
    # block list: 150 adjacent strips across the whole width, all in block row 0 -> 12 x (150+1)
    strips = [W.quad(-2998, -2998 + 2 * i, 2998, -2996 + 2 * i) for i in range(150)]
    st = W.validate_water(strips, raise_on_error=False)
    assert st.block_list_words == 12 * (150 + 1), st.block_list_words
    assert any("block-list" in e for e in st.errors)
    st = W.validate_water([ok])
    assert (st.quads, st.vertices, st.block_list_words) == (1, 4, 0)


def test_water_parser_rules():
    v = "0.0 0.0 0.0 0.0 0.0 0.0 0.0 100.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 100.0 0.0 0.0 0.0 0.0 0.0"
    quad = v + " 100.0 100.0 0.0 0.0 0.0 0.0 0.0"
    text = "\n".join(["processed", "; comment", "* star", "", "   ", quad.replace(" ", ","), v + " 3", v])
    polys = W.parse_water_dat(text)
    assert [len(p.verts) for p in polys] == [4, 3, 3]
    assert [p.flags for p in polys] == [1, 3, 1]          # missing flag -> 1
    raises(W.WaterError, W.parse_water_dat, "# comment\n" + quad)   # '#' is not a comment
    # vertices merge on exact (x, y, float32 z); first poly's waves win
    a = W.quad(0, 0, 100, 100, 0.0, big_waves=0.5)
    b = W.quad(100, 0, 200, 100, 0.0, big_waves=0.1)
    st = W.validate_water([a, b])
    assert st.vertices == 6 and any("different waves" in w for w in st.warnings)
    assert W.snap_even(101) in (100, 102) and W.snap_even(101, "down") == 100 and W.snap_even(101, "up") == 102


def test_water_mask():
    mask = [[1, 1, 0, 0],
            [1, 1, 1, 0],
            [0, 1, 1, 1]]
    rects = W.rects_from_mask(mask, -100, -100, 50)
    area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
    assert area == sum(map(sum, mask)) * 50 * 50
    polys = [W.quad(*r) for r in rects]
    W.validate_water(polys)
    gw = W.GameWater(polys)
    for j, row in enumerate(mask):
        for i, m in enumerate(row):
            cx, cy = -100 + i * 50 + 25, -100 + j * 50 + 25
            assert (gw.level_at(cx, cy, 0) is not None) == bool(m), (i, j)
    raises(W.WaterError, W.rects_from_mask, mask, -99, 0, 50)


# --- zones + text ------------------------------------------------------------------------------
def sample_zones():
    info = [Z.navi_zone("BAKU1", -2900, -2900, 2900, 2900, gxt="BAKU"),
            Z.navi_zone("NASIMI", -500, -500, 500, 500),
            Z.navi_zone("NASIMI", 500, -500, 900, 0),               # same info label -> one ZoneInfo
            Z.navi_zone("YASAM", -400, -400, -100, -100, gxt="YASAM")]
    maps = [Z.map_zone("BAKU", -3000, -3000, 3000, 3000, Z.LEVEL_LOS_SANTOS)]
    return info, maps


def test_zones():
    info, maps = sample_zones()
    t = Z.format_zon(info, comment="test")
    assert t.splitlines()[:2] == ["# test", "zone"] and t.splitlines()[-1] == "end"
    assert t.splitlines()[2] == "BAKU1, 0, -2900.0, -2900.0, -100.0, 2900.0, 2900.0, 900.0, 1, BAKU"
    back = Z.parse_zon(t)
    assert [(z.name, z.gxt, z.stored_box()) for z in back] == [(z.name, z.gxt, z.stored_box()) for z in info]
    assert Z.parse_zon(Z.format_zon(maps))[0].gxt == "UNUSED"
    counts = Z.validate_zones(info, maps)
    assert counts == {"navigation_zones": 5, "zone_infos": 4, "map_zones": 2}
    assert Z.zone_at(info, -200, -200, 10).name == "YASAM"     # smallest wins
    assert Z.zone_at(info, 700, -100, 10).name == "NASIMI"
    assert Z.zone_at(info, 2000, 2000, 10).name == "BAKU1"
    assert Z.zone_at(info, 2950, 0, 10).name == "SAN_AND"       # outside every zone
    full = [Z.navi_zone("ALL", -3000, -3000, 3000, 3000)]        # same size as SAN_AND never wins
    assert Z.zone_at(full, 0, 0, 10).name == "SAN_AND"
    assert Z.zone_at(info, 0, 0, 1500).name == "SAN_AND"         # above every z range
    assert Z.level_at(maps, 0, 0, 0) == 1 and Z.level_at([], 0, 0, 0) == 0
    # truncation toward zero (C cast) of the stored box
    assert Z.Zone("A", 0, -10.7, 5.9, 0, 20.2, 30.99, 1, 0, "A").stored_box() == (-10, 5, 0, 20, 30, 1)
    raises(Z.ZoneError, Z.validate_zone_file, [Z.navi_zone("TOOLONGNAME", 0, 0, 10, 10)])
    raises(Z.ZoneError, Z.validate_zone_file, [Z.Zone("X", 2, 0, 0, 0, 10, 10, 10, 1, "X")])
    raises(Z.ZoneError, Z.validate_zone_file, [Z.navi_zone("SAN_AND", 0, 0, 10, 10)])
    big = [Z.navi_zone(f"Z{i}", i, 0, i + 1, 1) for i in range(380)]
    assert "380 navigation zones > 379" in raises(Z.ZoneError, Z.validate_zones, big, [])
    many_maps = [Z.map_zone(f"M{i}", i, 0, i + 1, 1, 1) for i in range(39)]
    assert "39 map zones > 38" in raises(Z.ZoneError, Z.validate_zones, [], many_maps)


def test_text():
    assert Z.encode_sa("Ç ç Ö ö Ü ü ä Ä") == bytes([0x85, 0x20, 0x9C, 0x20, 0x91, 0x20, 0xA8, 0x20,
                                                     0x95, 0x20, 0xAC, 0x20, 0x9A, 0x20, 0x83])
    assert Z.to_sa_text("Dərnəgül") == "Därnägül"
    assert Z.to_sa_text("Ağ Şəhər") == "Agh Shähär"
    assert Z.to_sa_text("Biləcəri İçərişəhər", "ascii") == "Bileceri Icherisheher"
    assert Z.encode_sa("Bakı") == b"Baki"
    raises(Z.ZoneError, Z.encode_sa, "Москва")
    raises(Z.ZoneError, Z.encode_sa, "a#b")
    assert Z.decode_sa(Z.encode_sa("Nəsimi")) == "Näsimi"
    # CKeyGen::GetUppercaseKey == CRC32 without final xor, case-insensitive
    assert Z.gxt_key_hash("SAN_AND") == 0xAE14F34B == Z.gxt_key_hash("san_and")
    fxt = Z.format_fxt({"SAN_AND": "Bakı", "NASIMI": "Nəsimi"})
    assert fxt == b"SAN_AND Baki\r\nNASIMI N\x9asimi\r\n"
    assert Z.parse_fxt(fxt + b"# comment\r\nKEY  two spaces\r\n") == {
        "SAN_AND": "Baki", "NASIMI": "Näsimi", "KEY": " two spaces"}
    raises(Z.ZoneError, Z.format_fxt, {"TOOLONGKEY": "x"})
    taken = set()
    labels = [Z.make_label(n, taken) for n in ("Nəsimi", "Nəsimi", "28 May", "Heydər Əliyev Mərkəzi")]
    assert labels == ["NESIMI", "NESIMI1", "Z28MAY", "HEYDERE"]
    g = Z.build_gxt({"MAIN": {"SAN_AND": "Bakı", "YASAM": "Yasamal"}, "BK1": {"BK1_A": "Salam"}})
    r = Z.read_gxt(g)
    assert r["MAIN"][Z.gxt_key_hash("YASAM")] == b"Yasamal" and r["BK1"][Z.gxt_key_hash("BK1_A")] == b"Salam"
    assert Z.build_gxt(r) == g                                    # hash-keyed round trip


# --- tracks ------------------------------------------------------------------------------------
def test_tracks():
    loop = [(-1000, -1000, 5), (1000, -1000, 5), (1000, 1000, 15), (-1000, 1000, 15)]
    stations = [(0, -1000), (1000, 0), (0, 1000), (-1000, 0), (-1000, -800), (800, 1000)]
    nodes = T.nodes_from_polyline(loop, 20.0, stations=stations)
    st = T.validate_track(nodes)
    assert st.stations == 6 and abs(st.length - 8000) < 1 and st.max_segment <= 20.001
    assert T.station_order_ok(nodes)
    text = T.format_tracks_dat(nodes)
    assert text.startswith(f"{len(nodes)}\n-1000 -1000 5 0\n") and text.endswith("\n")
    back = T.parse_tracks_dat(text)
    assert T.game_distances(back) == T.game_distances(nodes)
    assert [n.station for n in back] == [n.station for n in nodes]
    assert T.parse_tracks_dat("processed\n" + text) == back
    # errors
    seven = [T.TrackNode(n.x, n.y, n.z, i % 10 == 0) for i, n in enumerate(nodes)]
    assert "stations > 6" in raises(T.TrackError, T.validate_track, seven)
    assert T.validate_track(seven, track=1).stations > 6          # only tracks.dat marks stations
    huge = T.nodes_from_polyline([(-2900, -2900, 0), (2900, -2900, 0), (2900, 2900, 0), (-2900, 2900, 0)], 50)
    assert "loop length" in raises(T.TrackError, T.validate_track, huge)
    dup = [T.TrackNode(0, 0, 0), T.TrackNode(0.2, 0, 0), T.TrackNode(100, 0, 0), T.TrackNode(100, 100, 0)]
    assert "does not grow" in raises(T.TrackError, T.validate_track, dup)
    close = [T.TrackNode(n.x, n.y, n.z, i in (0, 2)) for i, n in enumerate(nodes)]
    assert "closer than" in raises(T.TrackError, T.validate_track, close)
    # quantisation: int16(trunc(v*8))/8
    assert T.quantize(2555.46) == 2555.375 and T.quantize(-1.06) == -1.0
    # no-train placeholder set is valid and far outside the map
    files = T.no_train_files()
    assert set(files) == {"tracks.dat", "tracks2.dat", "tracks3.dat", "tracks4.dat"}
    for name, txt in files.items():
        ns = T.parse_tracks_dat(txt)
        T.validate_track(ns, track=T.FILE_TRACK[name])
        assert all(abs(n.x) > 3500 and abs(n.y) > 3500 for n in ns)
        assert len(ns) == 8 and sum(n.station for n in ns) == (6 if name == "tracks.dat" else 0)


# --- radar -------------------------------------------------------------------------------------
def test_radar():
    assert R.tile_name(0) == "radar00" and R.tile_name(7) == "radar07" and R.tile_name(143) == "radar143"
    assert R.tile_world_rect(0, 0) == (-3000, 2500, -2500, 3000)
    assert R.tile_world_rect(11, 11) == (2500, -3000, 3000, -2500)
    assert R.world_to_tile(-2999, 2999) == (0, 0) and R.world_to_tile(2999, -2999) == (11, 11)
    assert R.world_to_tile(0.5, -0.5) == (6, 6)
    # world image with a distinct colour per tile
    full = Image.new("RGB", (1200, 1200))
    px = full.load()
    for y in range(1200):
        for x in range(1200):
            px[x, y] = (x // 100 * 20, y // 100 * 20, 0)
    tiles = R.split_radar_image(full, 128, resample=Image.NEAREST)
    assert len(tiles) == 144 and tiles[13][0] == tiles[13][1] == "radar13"
    for idx in (0, 11, 13, 132, 143):
        col, row = R.tile_colrow(idx)
        im = tiles[idx][2]
        assert im.size == (128, 128) and im.mode == "RGB"
        assert im.getpixel((64, 64)) == (col * 20, row * 20, 0), idx
    # a world point lands in the tile/pixel the game uses
    wx, wy = 1234.0, -2100.0
    col, row = R.world_to_tile(wx, wy)
    x0, y0, x1, y1 = R.tile_world_rect(col, row)
    assert x0 <= wx <= x1 and y0 <= wy <= y1
    pxl = R.world_to_pixel(wx, wy, 1200, 1200)
    assert (int(pxl[0]) // 100, int(pxl[1]) // 100) == (col, row)
    back = R.assemble_radar(tiles)
    assert back.size == (1536, 1536) and back.getpixel((128 * 5 + 3, 128 * 7 + 3)) == (100, 140, 0)
    raises(ValueError, R.split_radar_image, Image.new("RGB", (100, 50)))
    try:
        txds = R.build_radar_txds(tiles[:2])
    except ImportError:
        print("  (tools.rw.txd not available, TXD packing skipped)")
    else:
        from tools.rw import txd as rwtxd
        info = rwtxd.read_txd(txds[0][1])
        assert txds[0][0] == "radar00.txd" and info[0]["name"] == "radar00"
        assert info[0]["width"] == 128 and info[0]["levels"] == 1 and info[0]["d3d_format"] == rwtxd.FOURCC_DXT1


# --- optional vanilla reference files -------------------------------------------------------------
def test_vanilla_reference():
    ref = os.environ.get("SA_DATA_REF")
    if not ref or not os.path.isdir(ref):
        print("  (SA_DATA_REF not set, vanilla checks skipped)")
        return
    p = os.path.join(ref, "water.dat")
    if os.path.exists(p):
        text = open(p, newline="").read().replace("\r\n", "\n")
        polys = W.parse_water_dat(text)
        st = W.validate_water(polys)
        assert (st.quads, st.triangles, st.vertices) == (301, 6, 1021), W.summary(st)
        assert st.block_list_words == 633
        assert W.format_water_dat(polys, validate=False) == text       # byte-identical
        print("  vanilla water.dat:", W.summary(st))
    if os.path.exists(os.path.join(ref, "info.zon")):
        info = Z.parse_zon(open(os.path.join(ref, "info.zon")).read())
        maps = Z.parse_zon(open(os.path.join(ref, "map.zon")).read())
        c = Z.validate_zones(info, maps)
        assert c["navigation_zones"] == 379 and c["map_zones"] == 7
        assert Z.zone_at(info, 2495, -1680, 13).gxt == "GAN" and Z.level_at(maps, 2495, -1680, 13) == 1
        print("  vanilla zones:", c)
    for idx, name in T.TRACK_FILES.items():
        p = os.path.join(ref, name)
        if os.path.exists(p):
            ns = T.parse_tracks_dat(open(p).read().replace("\r\n", "\n"))
            st = T.validate_track(ns, track=idx)
            assert T.game_distances(T.parse_tracks_dat(T.format_tracks_dat(ns, track=idx))) == T.game_distances(ns)
            print(f"  vanilla {name}: {st.nodes} nodes, {st.length:.0f} units, {st.stations} stations")


if __name__ == "__main__":
    for fn in (test_water_roundtrip, test_water_rules, test_water_parser_rules, test_water_mask,
               test_zones, test_text, test_tracks, test_radar, test_vanilla_reference):
        fn()
        print("ok", fn.__name__)
