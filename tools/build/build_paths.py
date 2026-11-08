"""Vehicle and pedestrian AI paths (nodes0..63.dat) from the generated road network."""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import pyarrow.parquet as pq
import shapely
from scipy.spatial import cKDTree

from tools.gta import paths as P
from tools.geo import profiles
from tools.geo.roadmesh import polyline_normals
from tools.geo.proj import lonlat_to_game, WORLD
from tools.build import ground


LIM = WORLD - 3.0


def light_group_for_dir(dx, dy):
    """Phase group the engine gives a traffic-light object whose forward (+y) axis points along
    (dx, dy): CTrafficLights::FindTrafficLightTypeFromOrientation (0x49D4D0), heading 60-150 or
    240-330 deg -> group 1 (LightForCars1, 'north-south'), otherwise group 2.  Path lights use the
    same rule on the travel direction, so the visible lights (props.py places them with their
    forward axis along the travel direction) always show the phase the AI obeys."""
    a = math.degrees(math.atan2(dy, dx)) % 360.0
    return P.LIGHT_NORTH_SOUTH if (60.0 <= a < 150.0 or 240.0 <= a < 330.0) else P.LIGHT_WEST_EAST


def signal_points():
    out = []
    for r in pq.read_table("data/raw/infrastructure.parquet", columns=["class", "geometry"]).to_pylist():
        if r["class"] == "traffic_signals":
            p = shapely.from_wkb(r["geometry"])
            if p.geom_type == "Point":
                out.append(lonlat_to_game(p.x, p.y))
    return np.array(out, dtype=float).reshape(-1, 2)


def signalised_junctions(W, max_dist=20.0):
    """Layout junctions (ground level) with an OSM traffic_signals point on or near them.
    Mid-block signals (pedestrian crossings) are ignored: in SA a light always runs a car phase."""
    js = [j for j in W.lay.junctions.values() if j.polygon is not None and not j.polygon.is_empty]
    if not js:
        return []
    polys = [j.polygon for j in js]
    tree = shapely.STRtree(polys)
    marked = set()
    for s in signal_points():
        p = shapely.Point(float(s[0]), float(s[1]))
        i = tree.nearest(p)
        if i is not None and polys[int(i)].distance(p) <= max_dist:
            marked.add(js[int(i)].id)
    return [W.lay.junctions[k] for k in sorted(marked)]


def _runs(mask):
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def _simplify_idx(pts, tol=0.9, max_len=45.0):
    """Indices of a simplified polyline (Douglas-Peucker + max segment length)."""
    keep = {0, len(pts) - 1}

    def rec(a, b):
        if b <= a + 1:
            return
        p, q = pts[a], pts[b]
        d = q - p
        L = math.hypot(*d)
        seg = pts[a + 1:b]
        if L < 1e-6:
            dist = np.hypot(*(seg - p).T)
        else:
            dist = np.abs(d[0] * (seg[:, 1] - p[1]) - d[1] * (seg[:, 0] - p[0])) / L
        i = int(np.argmax(dist))
        if dist[i] > tol:
            keep.add(a + 1 + i)
            rec(a, a + 1 + i)
            rec(a + 1 + i, b)

    rec(0, len(pts) - 1)
    idx = sorted(keep)
    s = profiles.arclen(pts)
    out = [idx[0]]
    for j in idx[1:]:
        while s[j] - s[out[-1]] > max_len:
            # insert intermediate vertex
            target = s[out[-1]] + max_len * 0.95
            k = int(np.searchsorted(s, target))
            k = min(max(k, out[-1] + 1), j)
            if k == j:
                break
            out.append(k)
        out.append(j)
    return out


def build_network(W, verbose=True):
    g = W.g
    net = P.PathNetwork()
    car_of = {}
    node_surface = {}
    for e in g.edges.values():
        if not e.major:
            continue
        zs = e.z + e.lift
        node_surface.setdefault(e.u, float(zs[0]))
        node_surface.setdefault(e.v, float(zs[-1]))

    def car_node(gn, speed, spawn):
        if gn not in car_of:
            x, y = g.nodes[gn]
            car_of[gn] = net.add_car_node(float(x), float(y), node_surface[gn], speed=speed, spawn=spawn)
        return car_of[gn]

    n_links = 0
    for e in g.edges.values():
        if not e.major or e.u == e.v:
            continue
        hw = e.cls in ("motorway", "trunk")
        speed = P.SPEED_HIGHWAY if hw else P.SPEED_NORMAL
        spawn = 15 if e.cls in ("motorway", "trunk", "primary", "secondary") else (12 if e.cls == "tertiary" else 9)
        pts_all = e.pts
        zs_all = e.z + e.lift
        inside = (np.abs(pts_all[:, 0]) < LIM) & (np.abs(pts_all[:, 1]) < LIM)
        if e.oneway:
            lf, lb = max(1, e.lanes_f), 0
        else:
            lf, lb = max(1, e.lanes_f), max(1, e.lanes_b)
        for i0, i1 in _runs(inside):
            pts = pts_all[i0:i1 + 1]
            zs = zs_all[i0:i1 + 1]
            if len(pts) < 2:
                continue
            idx = _simplify_idx(pts)
            first = car_node(e.u, speed, spawn) if i0 == 0 else \
                net.add_car_node(float(pts[0, 0]), float(pts[0, 1]), float(zs[0]), speed=speed, spawn=spawn)
            seq = [first]
            for k in idx[1:-1]:
                seq.append(net.add_car_node(float(pts[k, 0]), float(pts[k, 1]), float(zs[k]), speed=speed, spawn=spawn))
            last = car_node(e.v, speed, spawn) if i1 == len(pts_all) - 1 else \
                net.add_car_node(float(pts[-1, 0]), float(pts[-1, 1]), float(zs[-1]), speed=speed, spawn=spawn)
            seq.append(last)
            for a, b in zip(seq, seq[1:]):
                if a == b:
                    continue
                net.add_car_link(a, b, lf, lb)
                n_links += 1
    # traffic lights at signalised junctions (Overture traffic_signals points).  A layout junction
    # may consist of several graph nodes (dual carriageways): every member node gets lights on the
    # approaches that come from outside the junction, none on the links inside it.
    signalised = set()
    sig_junctions = signalised_junctions(W)
    for j in sig_junctions:
        members = {car_of[n] for n in j.nodes if n in car_of}
        for n in j.nodes:
            c = car_of.get(n)
            if c is None:
                continue

            def group_for(i, c=c):
                l = net.car_links[i]
                o = l.b if l.a == c else l.a
                return light_group_for_dir(net.car_nodes[c].x - net.car_nodes[o].x,
                                           net.car_nodes[c].y - net.car_nodes[o].y)
            try:
                if net.signalize_junction(c, group_for=group_for, skip_from=lambda o, m=members: o in m):
                    signalised.add(c)
            except Exception as ex:  # pragma: no cover
                if verbose:
                    print("signal fail", c, ex)
    # ---------------- peds ----------------
    # sidewalk polylines along both sides of every ground-level ribbon; their ends are collected per
    # layout junction (all member nodes of a dual-carriageway junction together) or per plain node
    ped_ends = defaultdict(list)
    lay = W.lay

    def end_key(node):
        j = lay.node_junction.get(node)
        return ("j", j) if j is not None else ("n", node)

    sidewalk_ids = []
    for r in lay.ribbons.values():
        e = r.edge
        s_full = profiles.arclen(e.pts)
        s_c = r.s0 + profiles.arclen(r.center)
        lf = np.interp(s_c, s_full, e.lift)
        if np.abs(lf).max() > 0.3:
            continue
        n = polyline_normals(r.center)
        for side, w, sw in ((+1, r.left, r.sw_left), (-1, r.right, r.sw_right)):
            if sw <= 0:
                continue
            line = r.center + n * side * (w + sw * 0.5)
            if not ((np.abs(line[:, 0]) < LIM) & (np.abs(line[:, 1]) < LIM)).all():
                continue
            idx = _simplify_idx(line, tol=1.2, max_len=28.0)
            z = ground.H_at(W, line[idx, 0], line[idx, 1]) + ground.KERB
            ids = [net.add_ped_node(float(line[k, 0]), float(line[k, 1]), float(zz), width=max(0.6, sw * 0.35))
                   for k, zz in zip(idx, z)]
            sidewalk_ids += ids
            if len(ids) >= 2:
                net.add_ped_polyline(ids)
            ped_ends[end_key(e.u)].append(ids[0])
            ped_ends[end_key(e.v)].append(ids[-1])
    for key, ends in ped_ends.items():
        if len(ends) < 2:
            continue
        EP = np.array([[net.ped_nodes[i].x, net.ped_nodes[i].y] for i in ends])
        if key[0] == "j":
            # ring by angle around the junction: corner links + crossings over every arm mouth
            c = np.array(lay.junctions[key[1]].polygon.centroid.coords[0])
            order = np.argsort(np.arctan2(EP[:, 1] - c[1], EP[:, 0] - c[0]))
            ring = [ends[i] for i in order]
            for a, b in zip(ring, ring[1:] + ring[:1]):
                pa, pb = net.ped_nodes[a], net.ped_nodes[b]
                if a != b and math.hypot(pa.x - pb.x, pa.y - pb.y) < 70:
                    net.add_ped_link(a, b)
        else:
            # plain continuation node: join the ends on the same side of the road only
            used = set()
            for i in range(len(ends)):
                d = np.hypot(*(EP - EP[i]).T)
                d[i] = np.inf
                j = int(np.argmin(d))
                if d[j] < 4.5 and (min(i, j), max(i, j)) not in used:
                    used.add((min(i, j), max(i, j)))
                    net.add_ped_link(ends[i], ends[j])
    # footways, park paths, steps and the OSM crosswalk ways (sidewalk ways are ours)
    fw_ends = []
    for e in g.edges.values():
        if e.cls not in ("footway", "path", "pedestrian", "steps", "cycleway") or e.subclass == "sidewalk":
            continue
        if e.length < 1.0 or not ((np.abs(e.pts[:, 0]) < LIM) & (np.abs(e.pts[:, 1]) < LIM)).all():
            continue
        idx = _simplify_idx(e.pts, tol=1.5, max_len=30.0)
        z = ground.H_at(W, e.pts[idx, 0], e.pts[idx, 1]) + ground.KERB
        ids = [net.add_ped_node(float(e.pts[k, 0]), float(e.pts[k, 1]), float(zz), width=1.0) for k, zz in zip(idx, z)]
        if len(ids) >= 2:
            net.add_ped_polyline(ids)
        fw_ends += [ids[0], ids[-1]]
    # footway ends near a sidewalk: join them (OSM paths stop at the OSM sidewalk, ours is nearby)
    if sidewalk_ids and fw_ends:
        sxy = np.array([[net.ped_nodes[i].x, net.ped_nodes[i].y] for i in sidewalk_ids])
        stree = cKDTree(sxy)
        for i in fw_ends:
            pn = net.ped_nodes[i]
            d, k = stree.query([pn.x, pn.y])
            if 0.6 < d < 7.0 and abs(net.ped_nodes[sidewalk_ids[k]].z - pn.z) < 1.5:
                net.add_ped_link(i, sidewalk_ids[k])
    if verbose:
        print(f"paths: {len(net.car_nodes)} car nodes, {len(net.car_links)} car links, {len(signalised)} signalised junctions, "
              f"{len(net.ped_nodes)} ped nodes, {len(net.ped_links)} ped links")
    return net


def _merge(nodes, links, tol, ztol):
    """Merge nodes closer than tol (2D) with |dz| < ztol; drop degenerate/duplicate links."""
    if not nodes:
        return {}, links
    xy = np.array([[n.x, n.y] for n in nodes])
    z = np.array([n.z for n in nodes])
    tree = cKDTree(xy)
    parent = list(range(len(nodes)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in tree.query_pairs(tol):
        if abs(z[a] - z[b]) < ztol:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    remap = {i: find(i) for i in range(len(nodes))}
    seen = set()
    out = []
    for l in links:
        a, b = remap[l.a], remap[l.b]
        if a == b:
            continue
        key = (min(a, b), max(a, b))
        if key in seen:
            continue
        pa, pb = nodes[a], nodes[b]
        if math.hypot(pa.x - pb.x, pa.y - pb.y) < 0.3:
            continue
        seen.add(key)
        lt = getattr(l, "light_toward", None)
        if lt is not None:
            lt = remap[lt]
            l.light_toward = lt if lt in (a, b) else None
            if l.light_toward is None:
                l.light_group = None
        l.a, l.b = a, b
        out.append(l)
    return remap, out


def sanitize(net):
    _, net.car_links = _merge(net.car_nodes, net.car_links, 0.35, 1.5)
    _, net.ped_links = _merge(net.ped_nodes, net.ped_links, 0.6, 1.0)
    # ped nodes with more than 11 links: drop extra ring links
    deg = defaultdict(int)
    keep = []
    for l in net.ped_links:
        if deg[l.a] >= 10 or deg[l.b] >= 10:
            continue
        deg[l.a] += 1
        deg[l.b] += 1
        keep.append(l)
    net.ped_links = keep
    deg = defaultdict(int)
    keep = []
    for l in net.car_links:
        if deg[l.a] >= 11 or deg[l.b] >= 11:
            continue
        deg[l.a] += 1
        deg[l.b] += 1
        keep.append(l)
    net.car_links = keep
    return net


def connect_ped_components(net, max_d=25.0, min_keep=10, verbose=True):
    """Bridge separate ped networks (park path systems, sidewalks cut off by structures) to the
    largest one with one short link each (crossing flags are added automatically where the link
    crosses a road); tiny leftovers that stay isolated are removed."""
    n = len(net.ped_nodes)
    if n == 0:
        return
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    deg = np.zeros(n, int)
    for l in net.ped_links:
        deg[l.a] += 1
        deg[l.b] += 1
        ra, rb = find(l.a), find(l.b)
        if ra != rb:
            parent[ra] = rb
    xy = np.array([[p.x, p.y] for p in net.ped_nodes])
    z = np.array([p.z for p in net.ped_nodes])
    comps = defaultdict(list)
    for i in range(n):
        if deg[i] > 0:
            comps[find(i)].append(i)
    order = sorted(comps, key=lambda r: -len(comps[r]))
    main = set(comps[order[0]])
    added = dropped = 0
    for _ in range(3):                      # a few passes: bridged components help the next ones
        main_idx = np.array(sorted(main))
        tree = cKDTree(xy[main_idx])
        changed = False
        for r in order[1:]:
            nodes = comps[r]
            if not nodes or nodes[0] in main:
                continue
            d, k = tree.query(xy[nodes], distance_upper_bound=max_d)
            best = None
            for ni, (dd, kk) in enumerate(zip(d, k)):
                if not np.isfinite(dd):
                    continue
                a, b = nodes[ni], int(main_idx[kk])
                if abs(z[a] - z[b]) > 2.0 or deg[a] >= 8 or deg[b] >= 8:
                    continue
                if best is None or dd < best[0]:
                    best = (dd, a, b)
            if best is not None:
                net.add_ped_link(best[1], best[2])
                deg[best[1]] += 1
                deg[best[2]] += 1
                main.update(nodes)
                added += 1
                changed = True
        if not changed:
            break
    keep = []
    gone = set()
    for r in order[1:]:
        nodes = comps[r]
        if nodes[0] not in main and len(nodes) < min_keep:
            gone.update(nodes)
            dropped += 1
    if gone:
        net.ped_links = [l for l in net.ped_links if l.a not in gone and l.b not in gone]
    if verbose:
        print(f"ped components: {len(order)} -> bridged {added}, removed {dropped} tiny ones")


def light_approaches(net):
    """Traffic-light approaches of a (sanitised) network: one record per car link with a light."""
    out = []
    for l in net.car_links:
        if l.light_toward is None:
            continue
        j = l.light_toward
        o = l.b if l.a == j else l.a
        nj, no = net.car_nodes[j], net.car_nodes[o]
        lanes_in = l.lanes_ab if l.b == j else l.lanes_ba
        lanes_out = l.lanes_ba if l.b == j else l.lanes_ab
        group = l.light_group or light_group_for_dir(nj.x - no.x, nj.y - no.y)
        out.append({"jx": nj.x, "jy": nj.y, "jz": nj.z, "ox": no.x, "oy": no.y, "oz": no.z,
                    "group": int(group), "lanes_in": int(lanes_in), "lanes_out": int(lanes_out)})
    return out


def compile_paths(W, out_dir=None, verbose=True):
    import json
    import os
    net = sanitize(build_network(W, verbose))
    connect_ped_components(net, verbose=verbose)
    lights = light_approaches(net)
    cp = P.compile_network(net, max_link_length=100.0, drop_isolated=True)
    if verbose:
        print("compiled:", cp.stats if hasattr(cp, "stats") else "", "warnings:", len(cp.warnings))
        for w in cp.warnings[:10]:
            print("  ", w)
        print("traffic-light approaches:", len(lights))
    if out_dir:
        cp.write(out_dir)
        with open(os.path.join(out_dir, "lights.json"), "w") as f:
            json.dump(lights, f)
    return cp


if __name__ == "__main__":
    W = ground.prepare()
    c = compile_paths(W, "build/out/paths")
    c.write_roadblox("build/out/paths/roadblox.dat")
