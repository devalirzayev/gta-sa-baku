"""Tests for tools/gta/paths.py (GTA SA nodes*.dat writer / reader / validator).

Run:  python3 tests/test_paths.py          (pytest also works)
Optional: GTA_SA_NODES_DIR=/path/to/vanilla/nodes python3 tests/test_paths.py
          additionally checks the reader against the 64 vanilla files.
"""

from __future__ import annotations

import importlib.util
import math
import os
import struct
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load_paths_module():
    sys.dont_write_bytecode = True        # keep the repository free of __pycache__
    spec = importlib.util.spec_from_file_location("ila_gta_paths", ROOT / "tools" / "gta" / "paths.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


P = _load_paths_module()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def build_grid_town(ox=100.0, oy=100.0, n=5, spacing=60.0, z=10.0):
    """5x5 grid of two-way streets, one one-way street, a signalised centre
    junction, a dead-end spur, a parking spur, a separate ring road, a boat
    pond and a sidewalk network with crossings around every junction."""
    net = P.PathNetwork()
    grid = {}
    for i in range(n):
        for j in range(n):
            grid[i, j] = net.add_car_node(ox + i * spacing, oy + j * spacing, z, tag=("grid", i, j))
    for i in range(n):
        for j in range(n):
            if i + 1 < n:                      # east-west streets
                if j == n - 1:                 # top street: one-way eastbound, 2 lanes
                    net.add_car_link(grid[i, j], grid[i + 1, j], lanes_ab=2, lanes_ba=0)
                else:
                    net.add_car_link(grid[i, j], grid[i + 1, j])
            if j + 1 < n:                      # north-south streets
                net.add_car_link(grid[i, j], grid[i, j + 1])
    # dead-end spur going south from the SW corner
    s1 = net.add_car_node(ox, oy - 30.0, z, tag="spur1")
    s2 = net.add_car_node(ox, oy - 60.0, z, tag="spur2")
    net.add_car_link(grid[0, 0], s1)
    net.add_car_link(s1, s2)
    # parking node west of grid[0, 2]
    park = net.add_car_node(ox - 12.0, oy + 2 * spacing, z, behaviour=P.BEHAVIOUR_PARK_PERPENDICULAR, tag="park")
    net.add_car_link(grid[0, 2], park)
    # ring road (pure cycle, separate component): must not get dead-end flags
    ring = [net.add_car_node(500.0 + 30.0 * math.cos(k * math.pi / 3), 500.0 + 30.0 * math.sin(k * math.pi / 3), z,
                             speed=P.SPEED_HIGHWAY, tag=("ring", k)) for k in range(6)]
    net.add_car_polyline(ring + ring[:1])
    # boat pond (water nodes, separate component)
    pond = [net.add_car_node(600.0 + dx, 150.0 + dy, 0.0, boat=True) for dx, dy in ((0, 0), (40, 0), (40, 40), (0, 40))]
    net.add_car_polyline(pond + pond[:1])
    # police roadblock in the middle of the street between grid[1,1] and grid[2,1]
    li = next(k for k, l in enumerate(net.car_links) if {l.a, l.b} == {grid[1, 1], grid[2, 1]})
    rb = net.split_car_link(li, 0.5)
    net.car_nodes[rb].police_roadblock = True
    # signalised centre junction
    centre = grid[n // 2, n // 2]
    light_links = net.signalize_junction(centre)

    # sidewalks: 4 corner nodes per junction, 8 units off the centre lines
    corner = {}
    for (i, j) in grid:
        x, y = ox + i * spacing, oy + j * spacing
        for sx in (-1, 1):
            for sy in (-1, 1):
                corner[i, j, sx, sy] = net.add_ped_node(x + 8.0 * sx, y + 8.0 * sy, z + 0.15)
    for (i, j) in grid:
        # crossings around the junction (auto-detected: only where a road exists)
        net.add_ped_link(corner[i, j, 1, 1], corner[i, j, 1, -1])     # east arm
        net.add_ped_link(corner[i, j, -1, 1], corner[i, j, -1, -1])   # west arm
        net.add_ped_link(corner[i, j, -1, 1], corner[i, j, 1, 1])     # north arm
        net.add_ped_link(corner[i, j, -1, -1], corner[i, j, 1, -1])   # south arm
        # sidewalks to the neighbouring junctions (parallel to roads)
        if i + 1 < n:
            net.add_ped_link(corner[i, j, 1, 1], corner[i + 1, j, -1, 1])
            net.add_ped_link(corner[i, j, 1, -1], corner[i + 1, j, -1, -1])
        if j + 1 < n:
            net.add_ped_link(corner[i, j, 1, 1], corner[i, j + 1, 1, -1])
            net.add_ped_link(corner[i, j, -1, 1], corner[i, j + 1, -1, -1])
    return net, dict(grid=grid, spur=(s1, s2), park=park, ring=ring, pond=pond, roadblock=rb,
                     centre=centre, light_links=light_links, corner=corner)


def node_rec(areas, addr):
    return areas[addr[0]].nodes[addr[1]]


def links_of(area, idx):
    return [(tuple(int(v) for v in area.links[j]), j) for j in area.node_links(idx)]


def qpos(areas, addr):
    r = node_rec(areas, addr)
    return np.array([int(r["x"]), int(r["y"]), int(r["z"])], float) / 8.0


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_grid_town_roundtrip():
    net, ids = build_grid_town()
    comp = P.compile_network(net)
    with tempfile.TemporaryDirectory() as tmp:
        written = comp.write(tmp)
        assert len(written) == 64 and all(p.exists() for p in written)
        names = sorted(p.name for p in Path(tmp).iterdir())
        assert names == sorted(f"nodes{i}.dat" for i in range(64))
        used = {a for a, _ in comp.car_addresses} | {a for a, _ in comp.ped_addresses}
        for i in range(64):
            data = (Path(tmp) / f"nodes{i}.dat").read_bytes()
            if i not in used:
                assert data == bytes(20), f"empty area {i} must be a 20-byte zero header"
            else:
                n, v, p, nav, nl = struct.unpack_from("<5I", data)
                assert n == v + p and len(data) == 20 + 28 * n + 14 * nav + 8 * nl + 1152
        areas = P.read_area_files(tmp)
    rep = P.validate_areas(areas, strict=True, roadblocks=comp.roadblock_addresses)
    assert rep.ok, rep.errors[:10]
    for a in range(64):
        assert areas[a].to_bytes() == comp.areas[a].to_bytes()

    # ordering: vehicle nodes first, land before water; ids = index; areas by game formula
    for a, ar in enumerate(areas):
        veh = ar.nodes[:ar.num_vehicle_nodes]
        water = (veh["flags"] & P.F_WATER) != 0
        assert not np.any(water[:-1] & ~water[1:]), "water nodes must follow land vehicle nodes"
    for i, n in enumerate(net.car_nodes):
        assert comp.car_addresses[i][0] == P.area_of(n.x, n.y)
    assert comp.car_addresses[ids["grid"][0, 0]][0] == 36           # (100, 100) -> col 4, row 4

    # every car link: lower-address attachment, direction to it, lanes mapping, length
    for li, l in enumerate(comp.car_links):
        A, B = comp.car_addresses[l.a], comp.car_addresses[l.b]
        na, ni = comp.navi_addresses[li]
        nv = P.decode_navi(areas[na].navis[ni])
        att, other = (A, B) if A < B else (B, A)
        assert nv["attached"] == att
        d = (qpos(areas, att) - qpos(areas, other))[:2]
        assert np.dot(d / np.linalg.norm(d), nv["dir"]) > 0.999
        if att == A:      # traffic towards the attached node = b -> a
            assert (nv["lanes_to_attached"], nv["lanes_from_attached"]) == (l.lanes_ba, l.lanes_ab)
        else:
            assert (nv["lanes_to_attached"], nv["lanes_from_attached"]) == (l.lanes_ab, l.lanes_ba)
        mid = (qpos(areas, A) + qpos(areas, B))[:2] / 2.0
        assert np.allclose((nv["x"], nv["y"]), mid, atol=0.07)
        assert na == P.area_of(nv["x"], nv["y"])                   # navi lives where it is
        for src, dst in ((A, B), (B, A)):
            for t, j in links_of(areas[src[0]], src[1]):
                if t == dst:
                    dist = np.linalg.norm(qpos(areas, src) - qpos(areas, dst))
                    assert int(areas[src[0]].link_lengths[j]) == max(1, min(255, int(dist)))
                    assert P._unpack_navi_link(areas[src[0]].navi_links[j]) == (na, ni)
                    break
            else:
                raise AssertionError("missing reciprocal link")

    # one-way top street: 2 lanes eastbound only
    g = ids["grid"]
    li = next(k for k, l in enumerate(comp.car_links) if {l.a, l.b} == {g[1, 4], g[2, 4]})
    na, ni = comp.navi_addresses[li]
    nv = P.decode_navi(areas[na].navis[ni])
    assert nv["attached"] == comp.car_addresses[g[1, 4]]             # west node has the lower id
    assert nv["dir"] == (-1.0, 0.0)                                   # points west, at the attached node
    assert nv["lanes_to_attached"] == 0 and nv["lanes_from_attached"] == 2

    # flood fill: grid+spur+park = 1 (largest), ring and pond get their own values
    fl = lambda i: int(node_rec(areas, comp.car_addresses[i])["flood"])
    assert fl(g[0, 0]) == 1 and fl(ids["spur"][1]) == 1 and fl(ids["park"]) == 1
    assert len({fl(g[0, 0]), fl(ids["ring"][0]), fl(ids["pond"][0])}) == 3
    assert int(node_rec(areas, comp.ped_addresses[0])["flood"]) == 1

    # dead-end bits: spur and parking node yes, grid and ring no
    de = lambda i: bool(int(node_rec(areas, comp.car_addresses[i])["flags"]) & P.F_DEAD_END)
    assert de(ids["spur"][0]) and de(ids["spur"][1]) and de(ids["park"])
    assert not any(de(i) for i in g.values())
    assert not any(de(i) for i in ids["ring"])

    # flags
    f_ring = P.decode_node_flags(node_rec(areas, comp.car_addresses[ids["ring"][0]])["flags"])
    assert f_ring["speed"] == P.SPEED_HIGHWAY and f_ring["spawn"] == 15 and f_ring["links"] == 2
    f_pond = P.decode_node_flags(node_rec(areas, comp.car_addresses[ids["pond"][0]])["flags"])
    assert f_pond["water"] and f_pond["speed"] == P.SPEED_NORMAL
    f_park = P.decode_node_flags(node_rec(areas, comp.car_addresses[ids["park"]])["flags"])
    assert f_park["behaviour"] == P.BEHAVIOUR_PARK_PERPENDICULAR
    for r in areas[36].nodes:
        assert int(r["search"]) == 0x7FFE and int(r["next"]) == 0 and int(r["prev"]) == 0

    # traffic lights on the four approaches of the centre junction
    centre_addr = comp.car_addresses[ids["centre"]]
    groups = {}
    for li in ids["light_links"]:
        l = comp.car_links[li]
        assert l.light_toward == ids["centre"]
        na, ni = comp.navi_addresses[li]
        nv = P.decode_navi(areas[na].navis[ni])
        assert nv["light_towards_attached"] == (nv["attached"] == centre_addr)
        other = l.a if l.b == ids["centre"] else l.b
        travel = qpos(areas, centre_addr)[:2] - qpos(areas, comp.car_addresses[other])[:2]
        assert np.linalg.norm(travel) < 20.0                          # short approach segment
        groups[(int(np.sign(round(travel[0]))), int(np.sign(round(travel[1]))))] = nv["light_group"]
    assert groups == {(1, 0): P.LIGHT_WEST_EAST, (-1, 0): P.LIGHT_WEST_EAST,
                      (0, 1): P.LIGHT_NORTH_SOUTH, (0, -1): P.LIGHT_NORTH_SOUTH}

    # ped crossings: auto-detected road-cross flags; ped lights only at the signal
    c = ids["corner"]
    def inter(pa, pb):
        A, B = comp.ped_addresses[pa], comp.ped_addresses[pb]
        for t, j in links_of(areas[A[0]], A[1]):
            if t == B:
                return int(areas[A[0]].intersections[j])
        raise AssertionError("no such ped link")
    ci, cj = 2, 2
    assert inter(c[ci, cj, 1, 1], c[ci, cj, 1, -1]) == P.I_ROAD_CROSS | P.I_PED_LIGHT
    assert inter(c[ci, cj, 1, -1], c[ci, cj, 1, 1]) == P.I_ROAD_CROSS | P.I_PED_LIGHT   # symmetric
    assert inter(c[1, 1, 1, 1], c[1, 1, 1, -1]) == P.I_ROAD_CROSS                      # unsignalised
    assert inter(c[0, 0, -1, 1], c[0, 0, -1, -1]) == 0                                 # no road west of (0,0)
    assert inter(c[0, 0, 1, 1], c[1, 0, -1, 1]) == 0                                   # sidewalk
    assert inter(c[0, 0, -1, -1], c[0, 0, 1, -1]) == P.I_ROAD_CROSS                    # crosses the spur
    for ar in areas:      # car links never carry intersection flags
        for k in range(ar.num_vehicle_nodes):
            assert all(int(ar.intersections[j]) == 0 for j in ar.node_links(k))

    # police roadblock -> roadblox.dat
    assert comp.roadblock_addresses == [comp.car_addresses[ids["roadblock"]]]
    with tempfile.TemporaryDirectory() as tmp:
        data = comp.write_roadblox(Path(tmp) / "roadblox.dat")
        assert len(data) == 4 + 325 * 4 == len((Path(tmp) / "roadblox.dat").read_bytes())
        assert P.read_roadblox(data) == comp.roadblock_addresses


def test_cross_area_links():
    net = P.PathNetwork()
    # straight road across the x = 0 border (areas 35 | 36) and up across y = 750 (row 4 | 5)
    xs = [-40.0, -10.0, 20.0, 50.0]
    road = [net.add_car_node(x, 300.0, 3.0) for x in xs]
    border_links = net.add_car_polyline(road, 1, 1)
    # light for eastbound traffic arriving at road[2] (higher address than the attached road[1])
    net.car_links[border_links[1]].light_toward = road[2]
    up = [road[-1], net.add_car_node(50.0, 740.0, 3.0), net.add_car_node(50.0, 770.0, 3.0)]
    net.add_car_polyline(up, 1, 1)
    # a node exactly on the x = 0 border belongs to column 4 (game formula floor)
    b0 = net.add_car_node(0.0, 400.0, 3.0)
    b1 = net.add_car_node(-30.0, 400.0, 3.0)
    net.add_car_link(b0, b1)
    # a diagonal link whose midpoint lies in a third area (corner case)
    d0 = net.add_car_node(-5.0, 745.0, 3.0)     # area 35
    d1 = net.add_car_node(4.0, 760.0, 3.0)      # area 44 (col 4, row 5); midpoint (-0.5, 752.5) -> area 43
    net.add_car_link(d0, d1)
    peds = [net.add_ped_node(x, 310.0, 3.1) for x in xs]
    net.add_ped_polyline(peds)

    comp = P.compile_network(net)
    with tempfile.TemporaryDirectory() as tmp:
        comp.write(tmp)
        areas = P.read_area_files(tmp)
    rep = P.validate_areas(areas, strict=True)
    assert rep.ok, rep.errors[:10]

    addr = comp.car_addresses
    assert [a for a, _ in (addr[i] for i in road)] == [35, 35, 36, 36]
    assert addr[up[1]][0] == 36 and addr[up[2]][0] == 44
    assert addr[b0][0] == 36 and addr[b1][0] == 35
    assert addr[d0][0] == 35 and addr[d1][0] == 44

    # the border-crossing link (-10 -> 20): attached to the area-35 node, navi stored in area 36
    li = next(k for k, l in enumerate(comp.car_links) if {l.a, l.b} == {road[1], road[2]})
    na, ni = comp.navi_addresses[li]
    nv = P.decode_navi(areas[na].navis[ni])
    assert nv["attached"] == addr[road[1]] and na == 36 and nv["dir"] == (-1.0, 0.0)
    # the light controls traffic AWAY from the attached node -> direction bit 0, W-E group
    assert nv["light_group"] == P.LIGHT_WEST_EAST and nv["light_towards_attached"] is False
    A = areas[35]
    t = [tt for tt, _ in links_of(A, addr[road[1]][1])]
    assert addr[road[2]] in t                                     # cross-area link target
    B = areas[36]
    assert addr[road[1]] in [tt for tt, _ in links_of(B, addr[road[2]][1])]

    # diagonal corner case: navi stored in area 43 although no endpoint lives there
    li = next(k for k, l in enumerate(comp.car_links) if {l.a, l.b} == {d0, d1})
    na, ni = comp.navi_addresses[li]
    assert na == 43 and areas[43].num_nodes == 0 and areas[43].num_navis == 1
    assert len(areas[43].to_bytes()) == 20 + 14                  # header + 1 navi, no link arrays

    # ped links across the border: navi links are 0, intersections 0
    pa = comp.ped_addresses
    assert pa[peds[1]][0] == 35 and pa[peds[2]][0] == 36
    for t, j in links_of(areas[35], pa[peds[1]][1]):
        assert int(areas[35].navi_links[j]) == 0 and int(areas[35].intersections[j]) == 0


def test_limits():
    # --- navi overflow: 1100 links with midpoints in area 36, endpoints in 35 | 36
    net = P.PathNetwork()
    for k in range(1100):
        y = 10.0 + 0.5 * k
        a = net.add_car_node(-1.0, y, 0.0)
        b = net.add_car_node(3.0, y, 0.0)
        net.add_car_link(a, b)
    comp = P.compile_network(net, dead_end_flags=True)
    assert comp.areas[36].num_navis == 1024 and comp.areas[35].num_navis == 76
    assert any("navi nodes stored in an endpoint's area" in w for w in comp.warnings)
    assert any("share flood fill 255" in w for w in comp.warnings)          # 1100 components
    flood = np.concatenate([comp.areas[35].nodes["flood"], comp.areas[36].nodes["flood"]])
    assert flood.max() == 255 and flood.min() == 1
    with tempfile.TemporaryDirectory() as tmp:
        comp.write(tmp)
        assert P.validate_areas(P.read_area_files(tmp), strict=True).ok
    try:
        P.compile_network(net, spill_navis=False)
        raise AssertionError("expected navi overflow error")
    except P.PathError as e:
        assert "1024" in str(e)

    # --- long links are split (3D length <= max_link_length, z from z_func)
    net = P.PathNetwork()
    a = net.add_car_node(-300.0, -100.0, 0.0)
    b = net.add_car_node(300.0, -100.0, 30.0)
    net.add_car_link(a, b, 2, 1)
    comp = P.compile_network(net, max_link_length=255.0, z_func=lambda x, y: 99.0)
    assert len(comp.car_links) == 3 and comp.stats["split_links"] == 1
    zs = sorted(n["z"] / 8.0 for ar in comp.areas for n in ar.nodes)
    assert zs.count(99.0) == 2
    lens = [int(v) for ar in comp.areas for v in ar.link_lengths]
    assert max(lens) <= 255 and len(lens) == 6
    assert all((l.lanes_ab, l.lanes_ba) == (2, 1) for l in comp.car_links)
    comp = P.compile_network(net)                                  # default 100
    assert max(int(v) for ar in comp.areas for v in ar.link_lengths) <= 100

    # --- input errors
    def expect_error(build, text):
        net = P.PathNetwork()
        build(net)
        try:
            P.compile_network(net)
        except P.PathError as e:
            assert text in str(e), str(e)
            return
        raise AssertionError(f"expected PathError containing {text!r}")

    def star(net, k):
        c = net.add_car_node(0.0, 0.0, 0.0)
        for i in range(k):
            o = net.add_car_node(40.0 * math.cos(i * 2 * math.pi / k), 40.0 * math.sin(i * 2 * math.pi / k), 0.0)
            net.add_car_link(c, o)
    expect_error(lambda n: star(n, 12), "links > 11")
    net = P.PathNetwork(); star(net, 9)
    assert any("9 links" in w for w in P.compile_network(net).warnings)
    expect_error(lambda n: (n.add_car_node(3100.0, 0.0, 0.0), n.add_car_node(0.0, 0.0, 0.0), n.add_car_link(0, 1)),
                 "outside [-3000, 3000]")
    expect_error(lambda n: (n.add_car_node(0.0, 0.0, 0.0), n.add_car_node(20.0, 0.0, 0.0), n.add_car_link(0, 1, 8, 1)),
                 "lanes")
    expect_error(lambda n: (n.add_car_node(0.0, 0.0, 0.0), n.add_car_node(20.0, 0.0, 0.0),
                            n.add_car_link(0, 1), n.add_car_link(1, 0)), "duplicate link")
    expect_error(lambda n: (n.add_car_node(0.0, 0.0, 0.0), n.add_car_node(20.0, 0.0, 0.0, boat=True),
                            n.add_car_link(0, 1)), "water node to a land node")
    expect_error(lambda n: (n.add_car_node(0.0, 0.0, 0.0), n.add_car_node(20.0, 0.0, 0.0),
                            n.add_car_node(50.0, 50.0, 0.0), n.add_car_link(0, 1)), "has no links")
    expect_error(lambda n: (n.add_car_node(0.0, 0.0, 0.0, police_roadblock=True), n.add_car_node(20.0, 0.0, 0.0),
                            n.add_car_link(0, 1)), "police roadblock")
    expect_error(lambda n: (n.add_ped_node(0.0, 0.0, 0.0), n.add_ped_node(0.1, 0.0, 0.0), n.add_ped_link(0, 1)),
                 "closer than 0.25")
    net = P.PathNetwork()
    net.add_car_node(0.0, 0.0, 0.0); net.add_car_node(20.0, 0.0, 0.0); net.add_car_node(50.0, 50.0, 0.0)
    net.add_car_link(0, 1)
    comp = P.compile_network(net, drop_isolated=True)
    assert sum(ar.num_nodes for ar in comp.areas) == 2

    # --- flood fill numbering: largest component first, values unique below 255
    net = P.PathNetwork()
    for k in range(300):
        a = net.add_ped_node(-2000.0 + 3.0 * (k % 100), -2000.0 + 3.0 * (k // 100), 0.0)
        b = net.add_ped_node(-2000.0 + 3.0 * (k % 100) + 1.0, -2000.0 + 3.0 * (k // 100), 0.0)
        net.add_ped_link(a, b)
    chain = [net.add_ped_node(-1500.0 + 2.0 * k, -1500.0, 0.0) for k in range(5)]
    net.add_ped_polyline(chain)
    comp = P.compile_network(net)
    assert int(node_rec(comp.areas, comp.ped_addresses[chain[0]])["flood"]) == 1
    vals = [int(node_rec(comp.areas, comp.ped_addresses[2 * k])["flood"]) for k in range(300)]
    assert vals.count(255) == 300 - 253 and len(set(v for v in vals if v < 255)) == 253


def test_lane_geometry():
    assert P.LANE_WIDTH == 5.4
    close = lambda a, b: len(a) == len(b) and all(abs(x - y) < 1e-6 for x, y in zip(a, b))
    assert close(P.lane_center_offsets(1, 1), [2.7])                      # classic SA street
    assert close(P.lane_center_offsets(2, 2), [2.7, 8.1])
    assert close(P.lane_center_offsets(1, 1, median=1.5), [4.2])          # median half-width added
    assert close(P.lane_center_offsets(2, 0), [-2.7, 2.7])                # one-way: centred on the line
    assert close(P.lane_center_offsets(3, 0), [-5.4, 0.0, 5.4])
    assert P.lane_center_offsets(0, 2) == []
    assert P.carriageway_extents(1, 1) == (-5.4, 5.4)
    assert P.carriageway_extents(2, 1, 1.0) == (-6.4, 11.8)
    assert P.carriageway_extents(0, 2) == (-5.4, 5.4)
    assert abs(P.recommended_road_width(1, 1) - 11.8) < 1e-9
    # the in-game formula (navi width byte / 86.4 + 0.5) for both-direction links
    assert abs(P._one_way_lane_offset(1, 1, 24) - (24 / 86.4 + 0.5)) < 1e-6
    assert P._one_way_lane_offset(0, 3, 50) == -1.0 and P._one_way_lane_offset(2, 0, 50) == -0.5
    # area grid
    assert P.area_of(-3000.0, -3000.0) == 0 and P.area_of(2999.0, 2999.0) == 63
    assert P.area_of(-2250.0, -3000.0) == 1 and P.area_of(-2250.125, -3000.0) == 0
    assert P.area_of(0.0, 0.0) == 36 and P.area_of(3000.0, 3000.0) == 63
    assert P.area_bounds(36) == (0.0, 0.0, 750.0, 750.0)


def test_validator_detects_corruption():
    net, ids = build_grid_town()
    comp = P.compile_network(net)
    base = [ar.to_bytes() for ar in comp.areas]
    fresh = lambda: [P.parse_area(b, i) for i, b in enumerate(base)]

    def errors(areas):
        return P.validate_areas(areas).errors

    assert errors(fresh()) == []
    g = ids["grid"]
    a, i = comp.car_addresses[g[1, 1]]

    areas = fresh(); areas[a].nodes["search"][i] = 0
    assert any("0x7FFE" in e for e in errors(areas))

    areas = fresh(); j = areas[a].node_links(i)[0]
    areas[a].links["node"][j] = comp.car_addresses[g[4, 4]][1]
    assert any("not reciprocated" in e for e in errors(areas))

    areas = fresh(); areas[a].navi_links[j] = (a << 10) | 1023
    assert any("navi link" in e and "out of range" in e for e in errors(areas))

    areas = fresh()
    for jj in areas[a].node_links(i):
        areas[a].navi_links[jj] = areas[a].navi_links[j]
    assert any("share one navi" in e for e in errors(areas))

    areas = fresh(); areas[a].nodes["flood"][i] = 77
    assert any("flood fill values" in e for e in errors(areas))

    areas = fresh(); ra, ri = comp.car_addresses[ids["ring"][0]]
    areas[ra].nodes["flags"][ri] |= P.F_DEAD_END
    assert any("never ends" in e for e in errors(areas))

    areas = fresh(); areas[a].nodes["flags"][i] &= ~np.uint32(0xF)
    assert any("no links" in e or "outside" in e for e in errors(areas))

    areas = fresh(); areas[a].nodes["base_link"][i] = 30000          # corrupt range of a link target
    errs = errors(areas)
    assert any("outside 0.." in e for e in errs) and any("not reciprocated" in e for e in errs)

    raw = bytearray(base[a])
    n, v, p, nav, nl = struct.unpack_from("<5I", raw)
    filler_off = 20 + 28 * n + 14 * nav + 4 * nl
    raw[filler_off:filler_off + 4] = b"\0\0\0\0"
    areas = fresh(); areas[a] = P.parse_area(bytes(raw), a)
    assert any("0xFFFF" in e for e in errors(areas))

    try:
        P.parse_area(base[a][:-10], a)
        raise AssertionError("truncated file accepted")
    except P.PathError:
        pass
    areas = fresh(); areas[5] = None
    assert any("missing" in e for e in errors(areas))


def test_vanilla_files():
    d = os.environ.get("GTA_SA_NODES_DIR")
    if not d:
        print("  (skipped: set GTA_SA_NODES_DIR to the 64 vanilla nodes*.dat)")
        return
    areas = P.read_area_files(d)
    rep = P.validate_areas(areas)
    assert rep.ok, rep.errors[:10]
    assert (rep.stats["nodes"], rep.stats["vehicle_nodes"], rep.stats["ped_nodes"],
            rep.stats["navis"], rep.stats["links"]) == (68237, 30587, 37650, 31466, 143622)
    by_lower = {p.name.lower(): p for p in Path(d).iterdir()}
    for i, ar in enumerate(areas):
        assert ar.to_bytes() == by_lower[f"nodes{i}.dat"].read_bytes(), f"area {i} not byte-identical"


def main() -> int:
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
