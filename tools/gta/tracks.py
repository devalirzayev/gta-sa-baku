"""GTA San Andreas train track files (``data/paths/tracks.dat``, ``tracks2.dat``, ``tracks3.dat``,
``tracks4.dat``): writer, parser, validator and polyline resampling.

Engine behaviour (docs/notes/data_files.md section e; CTrain::InitTrains 0x6F7440 and
CTrain::ReadAndInterpretTrackFile 0x6F55D0):

* Track index -> file: 0 ``tracks.dat`` (random trains + stations), 1 ``tracks3.dat`` (random
  trams, only generated while CWeather::WeatherRegion == SF), 2 ``tracks2.dat``, 3 ``tracks4.dat``
  (mission trains only: CREATE_MISSION_TRAIN snaps to the nearest node of any track).
* The whole file is read into a 0xB530 (46384) byte heap buffer (the original LoadFile ignores
  the size -> larger files overflow).  Lines are split on '\\n' only.  Optional first line
  exactly ``processed`` (LF only), then the node count (``%d``), then exactly that many rows
  ``x y z station`` (``%f %f %f %d``).  Rows are copied without a terminator, so every row must
  carry all four fields, and every row (including the last) must end with '\\n'.
* Nodes are stored as int16(x*8) (1/8 unit precision, |coord| < 4096).  Cumulative 2-D length
  is stored as uint16(3*distance) and the track is always closed (last node -> first node), so
  the loop length must stay below 65536/3 = 21845.3 units.
* In ``tracks.dat`` every row with a non-zero 4th field overwrites the next entry of the
  hard-coded CTrain::aStationCoors[6] - more than 6 overflows into other globals.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

TRACK_FILES = {0: "tracks.dat", 1: "tracks3.dat", 2: "tracks2.dat", 3: "tracks4.dat"}
FILE_TRACK = {v: k for k, v in TRACK_FILES.items()}
BUFFER_SIZE = 0xB530             # operator new(0xB530) in ReadAndInterpretTrackFile
MAX_STATIONS = 6                 # CTrain::aStationCoors[6]
COORD_SCALE = 8.0                # int16(x * 8)
MAX_COORD = 32767 / COORD_SCALE  # 4095.875
DIST_SCALE = 3.0                 # uint16(3 * distance)
MAX_LOOP_LENGTH = 65535 / DIST_SCALE
MIN_SEGMENT = 1.0 / 3.0          # shorter segments can repeat a uint16(3*dist) value -> 1/0 -> NaN
STATION_MIN_SPACING = 100.0      # FindNextStationPositionInDirection treats < 100 as "at station"
# hard-coded SA station positions (CTrain::aStationCoors @0x8D48F8) used when tracks.dat marks < 6
SA_STATION_COORS = ((1741.0, -1954.0, 15.0), (1297.0, -1898.0, 3.0), (-1945.0, 128.0, 29.0),
                    (1434.0, 2632.0, 13.0), (2783.0, 1758.0, 12.0), (2865.0, 1281.0, 12.0))


class TrackError(ValueError):
    pass


@dataclass(frozen=True)
class TrackNode:
    x: float
    y: float
    z: float
    station: bool = False


def quantize(v: float) -> float:
    """Value the game stores: int16(trunc(v*8)) / 8."""
    n = int(math.trunc(float(v) * COORD_SCALE))
    n = ((n + 0x8000) & 0xFFFF) - 0x8000
    return n / COORD_SCALE


def snap(v: float) -> float:
    """Round to the nearest 1/8 unit (use before :func:`quantize` when generating new tracks)."""
    return round(float(v) * COORD_SCALE) / COORD_SCALE


def _stored(n: TrackNode) -> TrackNode:
    return TrackNode(quantize(n.x), quantize(n.y), quantize(n.z), bool(n.station))


def _fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def format_tracks_dat(nodes: Sequence[TrackNode], *, track: int = 0, newline: str = "\n",
                      validate: bool = True) -> str:
    """Text for one tracks*.dat file (no 'processed' header, every line LF-terminated).

    Coordinates are written as the exact values the game stores (int16(trunc(v*8))/8), so the
    file re-reads to identical CTrainNode data."""
    nodes = [_stored(n) for n in nodes]
    if validate:
        validate_track(nodes, track=track)
    lines = [str(len(nodes))] + [f"{_fmt(n.x)} {_fmt(n.y)} {_fmt(n.z)} {1 if n.station else 0}"
                                 for n in nodes]
    text = newline.join(lines) + newline
    if validate and len(text.encode("ascii")) >= BUFFER_SIZE:
        raise TrackError(f"file is {len(text)} bytes; must be < {BUFFER_SIZE} (heap buffer)")
    return text


def write_tracks_dat(path, nodes: Sequence[TrackNode], *, track: int | None = None, **kw) -> "TrackStats":
    import os
    if track is None:
        track = FILE_TRACK.get(os.path.basename(str(path)).lower(), 0)
    text = format_tracks_dat(nodes, track=track, **kw)
    with open(path, "w", encoding="ascii", newline="") as f:
        f.write(text)
    return validate_track([_stored(n) for n in nodes], track=track)


def parse_tracks_dat(text: str) -> list:
    """Parse like 0x6F55D0 (rows split on LF; optional exact 'processed' first line)."""
    rows = text.split("\n")
    i = 0
    if rows and rows[0] == "processed":
        i = 1
    count = int(rows[i].split()[0])
    out = []
    for r in rows[i + 1:i + 1 + count]:
        t = r.split()
        if len(t) < 4:
            raise TrackError(f"row needs 'x y z station', got {r!r}")
        out.append(TrackNode(float(t[0]), float(t[1]), float(t[2]), int(float(t[3])) != 0))
    if len(out) != count:
        raise TrackError(f"declared {count} nodes, found {len(out)}")
    return out


@dataclass
class TrackStats:
    nodes: int
    length: float          # closed 2-D length as the game computes it
    stations: int
    min_segment: float
    max_segment: float
    closing_segment: float
    file_bytes: int


def game_distances(nodes: Sequence[TrackNode]) -> tuple:
    """(m_nDistanceFromStart list, total length) exactly as ReadAndInterpretTrackFile computes."""
    q = [(quantize(n.x), quantize(n.y)) for n in nodes]
    dist, out = 0.0, []
    for i in range(len(q)):
        out.append(int(math.trunc(DIST_SCALE * dist)) & 0xFFFF)
        x0, y0 = q[i]
        x1, y1 = q[(i + 1) % len(q)]
        dist += math.hypot(x0 - x1, y0 - y1)
    return out, dist


def validate_track(nodes: Sequence[TrackNode], *, track: int = 0) -> TrackStats:
    errors = []
    if len(nodes) < 2:
        errors.append("need at least 2 nodes (use dummy_track() for a harmless placeholder)")
    for i, n in enumerate(nodes):
        for v in (n.x, n.y, n.z):
            if not math.isfinite(v) or abs(v) >= MAX_COORD:
                errors.append(f"node {i}: coordinate {v} outside int16/8 range")
    st_idx = [i for i, n in enumerate(nodes) if n.station]
    if track == 0 and len(st_idx) > MAX_STATIONS:
        errors.append(f"{len(st_idx)} stations > {MAX_STATIONS} (aStationCoors overflow)")
    segs = []
    if len(nodes) >= 2:
        q = [(quantize(n.x), quantize(n.y)) for n in nodes]
        for i in range(len(q)):
            a, b = q[i], q[(i + 1) % len(q)]
            segs.append(math.hypot(a[0] - b[0], a[1] - b[1]))
    dists, total = game_distances(nodes) if len(nodes) >= 2 else ([], 0.0)
    for i in range(len(dists) - 1):
        if dists[i + 1] <= dists[i]:
            errors.append(f"segment {i}->{i + 1} ({segs[i]:.3f} units): stored distance does not grow "
                          f"(segments must be > {MIN_SEGMENT:.3f}); the train position becomes NaN")
    if len(dists) >= 2 and int(math.trunc(DIST_SCALE * total)) <= dists[-1]:
        errors.append(f"closing segment {len(dists) - 1}->0 is too short ({segs[-1]:.3f} units)")
    if total * DIST_SCALE > 65535:
        errors.append(f"closed loop length {total:.1f} > {MAX_LOOP_LENGTH:.1f} (uint16 distance wraps)")
    if track == 0 and len(st_idx) > 1 and total > 0:
        sd = [dists[i] / DIST_SCALE for i in st_idx]
        for a, b in zip(sd, sd[1:] + [sd[0] + total]):
            if b - a < STATION_MIN_SPACING:
                errors.append(f"stations {a:.0f} and {b % total:.0f} closer than {STATION_MIN_SPACING} "
                              f"along the track")
    if errors:
        raise TrackError("\n".join(errors))
    size = len(format_tracks_dat(nodes, track=track, validate=False).encode("ascii"))
    return TrackStats(len(nodes), total, len(st_idx), min(segs[:-1] or segs), max(segs[:-1] or segs),
                      segs[-1], size)


# --- geometry helpers ----------------------------------------------------------------------------
def resample_polyline(points: Sequence[Sequence[float]], spacing: float, *, closed: bool = True,
                      keep_vertices: bool = True) -> list:
    """Resample a 3-D polyline to nodes about ``spacing`` apart (2-D distance).

    With ``keep_vertices`` the input vertices are kept and every edge is split evenly, which
    preserves sharp geometry; vanilla node spacing is 1.3-256 units (mean ~20 on tracks.dat),
    use ~5-10 on curves and <= 50 on straights.  For ``closed`` input do not repeat the first
    point at the end."""
    pts = [tuple(map(float, p[:3])) if len(p) >= 3 else (float(p[0]), float(p[1]), 0.0) for p in points]
    if closed and len(pts) > 1 and math.dist(pts[0][:2], pts[-1][:2]) < 1e-9:
        pts = pts[:-1]
    edges = list(zip(pts, pts[1:] + ([pts[0]] if closed else [])))
    out = []
    if keep_vertices:
        for a, b in edges:
            n = max(1, int(math.ceil(math.dist(a[:2], b[:2]) / spacing)))
            for k in range(n):
                t = k / n
                out.append(tuple(a[j] + (b[j] - a[j]) * t for j in range(3)))
        if not closed:
            out.append(pts[-1])
        return out
    # uniform arc-length sampling
    lengths = [math.dist(a[:2], b[:2]) for a, b in edges]
    total = sum(lengths)
    n = max(2, int(round(total / spacing)))
    step = total / n if closed else total / (n - 1)
    targets = [k * step for k in range(n)]
    out, ei, acc = [], 0, 0.0
    for t in targets:
        while ei < len(edges) - 1 and acc + lengths[ei] < t:
            acc += lengths[ei]
            ei += 1
        a, b = edges[ei]
        f = 0.0 if lengths[ei] == 0 else min(1.0, (t - acc) / lengths[ei])
        out.append(tuple(a[j] + (b[j] - a[j]) * f for j in range(3)))
    return out


def nodes_from_polyline(points: Sequence[Sequence[float]], spacing: float = 10.0, *,
                        stations: Iterable[Sequence[float]] = (), closed: bool = True,
                        keep_vertices: bool = True) -> list:
    """Closed track nodes from a polyline; each station (x, y) marks the nearest node."""
    pts = resample_polyline(points, spacing, closed=closed, keep_vertices=keep_vertices)
    # drop near-duplicates created by coincident input vertices
    clean = []
    for p in pts:
        if not clean or math.dist(clean[-1][:2], p[:2]) >= MIN_SEGMENT:
            clean.append(p)
    if len(clean) > 2 and math.dist(clean[0][:2], clean[-1][:2]) < MIN_SEGMENT:
        clean.pop()
    flags = [False] * len(clean)
    for s in stations:
        j = min(range(len(clean)), key=lambda k: math.dist(clean[k][:2], (s[0], s[1])))
        if flags[j]:
            raise TrackError(f"two stations snap to node {j}")
        flags[j] = True
    return [TrackNode(p[0], p[1], p[2], f) for p, f in zip(clean, flags)]


def dummy_track(center=(3900.0, 3900.0, -100.0), half: float = 160.0, stations: bool = True) -> list:
    """Tiny closed square loop far outside the playable map (+-3000) for 'no trains' setups.

    8 nodes; with ``stations`` 6 of them are flagged so tracks.dat overwrites all hard-coded SA
    station coordinates (keeps CTrain::InitTrains' station lookup in bounds).  Random trains are
    only generated when a random node is within 70-100 units (2-D) of the camera, i.e. never."""
    cx, cy, cz = center
    pts = [(-half, -half), (0, -half), (half, -half), (half, 0), (half, half), (0, half), (-half, half),
           (-half, 0)]
    return [TrackNode(cx + dx, cy + dy, cz, stations and k < MAX_STATIONS) for k, (dx, dy) in enumerate(pts)]


def no_train_files(center=(3900.0, 3900.0, -100.0)) -> dict:
    """{filename: text} for the four track files with identical dummy loops."""
    nodes = dummy_track(center)
    return {name: format_tracks_dat(nodes if idx == 0 else [TrackNode(n.x, n.y, n.z) for n in nodes],
                                    track=idx) for idx, name in TRACK_FILES.items()}


def station_order_ok(nodes: Sequence[TrackNode]) -> bool:
    """FindNextStationPositionInDirection assumes StationDist ascending; true when exactly 6
    stations are flagged (file order == track order).  With fewer, the remaining hard-coded SA
    coordinates are snapped to the nearest node and may break the ordering."""
    return sum(1 for n in nodes if n.station) == MAX_STATIONS
