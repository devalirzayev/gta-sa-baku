"""GTA San Andreas (PC 1.0) AI path files: ``nodes0.dat`` ... ``nodes63.dat``.

Everything here follows ``docs/notes/paths.md`` (verified against the
gta-reversed / ReSA reconstructions of the game code and against the 64
vanilla files).  Section references like ``[paths.md §3]`` point there.

High level::

    net = PathNetwork()
    a = net.add_car_node(10.0, 20.0, 5.0)
    b = net.add_car_node(60.0, 20.0, 5.0)
    net.add_car_link(a, b, lanes_ab=1, lanes_ba=1)      # 1 lane each way
    p = net.add_ped_node(10.0, 28.0, 5.2)
    q = net.add_ped_node(10.0, 12.0, 5.2)
    net.add_ped_link(p, q)                              # crossing flag auto-detected
    compiled = compile_network(net)                     # PathError on bad input
    compiled.write("out/")                              # nodes0.dat .. nodes63.dat
    write_roadblox("out/roadblox.dat", compiled.roadblock_addresses)

Low level: ``AreaData`` (numpy structured arrays mirroring the file),
``AreaData.to_bytes()``, ``parse_area()``, ``read_area_files()``,
``validate_areas()``, ``read_roadblox()``/``write_roadblox()``.

Conventions (see paths.md):
* area index = 8 * row + column, column = floor((x + 3000) / 750) clamped to 0..7,
  row likewise from y (CPathFind::FindXRegionForCoors / GetAreaIdFromXY);
* per area: vehicle nodes first (land, then water), then ped nodes;
* one navi node per undirected car link, attached to the endpoint with the
  lower (area, node) address, direction pointing from the other endpoint
  towards the attached one, stored in the area that contains its position;
* navi lane bits 0-2 of byte 11 = lanes for traffic driving towards the
  attached node, bits 3-5 = lanes driving away from it;
* lanes are 5.4 units wide and lie to the right of the travel direction.
"""

from __future__ import annotations

import dataclasses
import math
import os
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np

__all__ = [
    # constants
    "AREA_SIZE", "WORLD_MIN", "NUM_AREAS", "LANE_WIDTH", "MAX_NAVI_PER_AREA",
    "MAX_LINKS_PER_NODE", "MAX_LANES", "MAX_LINK_LENGTH", "DYNAMIC_LINK_SLOTS",
    "MAX_ROADBLOCKS", "SPEED_SLOW", "SPEED_NORMAL", "SPEED_HIGHWAY",
    "BEHAVIOUR_NONE", "BEHAVIOUR_PARK_PARALLEL", "BEHAVIOUR_PARK_PERPENDICULAR",
    "BEHAVIOUR_TALL_ONLY", "BEHAVIOUR_SMALL_ONLY", "BEHAVIOUR_SMALL_ONLY_WAIT",
    "BEHAVIOUR_TALL_ONLY_WAIT", "LIGHT_NONE", "LIGHT_NORTH_SOUTH", "LIGHT_WEST_EAST",
    "NODE_DTYPE", "NAVI_DTYPE", "LINK_DTYPE",
    # high level
    "CarNode", "PedNode", "CarLink", "PedLink", "PathNetwork", "CompiledPaths",
    "compile_network", "PathError",
    # low level
    "AreaData", "parse_area", "read_area_files", "area_file_name",
    "validate_areas", "ValidationReport", "read_roadblox", "write_roadblox",
    # helpers
    "area_of", "area_bounds", "lane_center_offsets", "carriageway_extents",
    "recommended_road_width", "decode_node_flags", "decode_navi",
]

# --------------------------------------------------------------------------
# constants  [paths.md §3, §6, §14]
# --------------------------------------------------------------------------

WORLD_MIN = -3000.0          # south-west corner of area 0
AREA_SIZE = 750.0            # 6000 / 8
AREAS_X = 8
AREAS_Y = 8
NUM_AREAS = AREAS_X * AREAS_Y
COORD_SCALE = 8.0            # positions: int16 / 8
WIDTH_SCALE = 16.0           # node and navi widths: uint8 / 16
DIR_SCALE = 100.0            # navi direction: int8 / 100
LANE_WIDTH = 5.4             # 0x858C50, used by every lane-offset computation
BMX_EXTRA_OFFSET = 1.458     # extra right offset for VEHICLE_TYPE_BMX (0x859010)
SEARCH_DIST_INIT = 0x7FFE    # CPathNode::m_totalDistFromOrigin "not in list" marker

MAX_NAVI_PER_AREA = 1024     # 10-bit navi index in CCarPathLinkAddress
MAX_LANES = 7                # 3 bits per direction
MAX_LINK_FIELD = 15          # 4-bit link count
MAX_LINKS_PER_NODE = 11      # dynamic-link blocks hold 12 links (AddDynamicLinkBetween2Nodes_For1Node)
RECOMMENDED_MAX_LINKS = 8    # vanilla max is 4 (cars) / 6 (peds)
MAX_LINK_LENGTH = 255        # uint8 link length
DYNAMIC_LINK_SLOTS = 16 * 12 # NUM_DYNAMIC_LINKS_PER_AREA * 12 reserved entries per array
MAX_LINKS_PER_AREA = 0x7FFF - DYNAMIC_LINK_SLOTS   # m_wBaseLinkId is int16
MAX_NODES_PER_AREA = 0x7FFF  # conservative (node ids are uint16, 0xFFFF = invalid)
MAX_FLOOD_FILL = 255         # uint8
MAX_ROADBLOCKS = 325         # CRoadBlocks::RoadBlockNodes
COORD_LIMIT_Q = 32767        # int16
MAX_NODE_WIDTH_RAW = 255
MAX_NAVI_WIDTH_RAW = 127     # exe reads it unsigned, struct declares int8: stay < 128
NAVI_LINK_PED = 0            # vanilla value of navi links of ped links

# node flag bits  [paths.md §4]
F_LINKS_MASK = 0x0000000F
F_DEAD_END = 1 << 4           # m_onDeadEnd: pre-filter for ThisNodeWillLeadIntoADeadEnd
F_SWITCHED_OFF = 1 << 5       # m_isSwitchedOff (no ambient spawning / wandering into)
F_ROADBLOCK = 1 << 6          # m_bRoadBlocks: wandering cars do not enter from normal nodes
F_WATER = 1 << 7              # m_bWaterNode: boats
F_SWITCHED_OFF_ORIG = 1 << 8  # m_isSwitchedOffOriginal: loader copies bit 5 into it
F_UNUSED_9 = 1 << 9           # skipped by FindNodeClosestInRegion(bUnkn); never set in vanilla
F_DONT_WANDER = 1 << 10       # m_bDontWander
SPEED_SHIFT = 12              # 2 bits: 0 slow (x0.8), 1 normal, 2 highway (x1.2)
SPAWN_SHIFT = 16              # 4 bits: spawn probability / density
BEHAVIOUR_SHIFT = 20          # 4 bits: special function

SPEED_SLOW = 0
SPEED_NORMAL = 1
SPEED_HIGHWAY = 2

BEHAVIOUR_NONE = 0
BEHAVIOUR_PARK_PARALLEL = 1        # wandering cars may park here (MISSION_PARK_PARALLEL)
BEHAVIOUR_PARK_PERPENDICULAR = 2   # (MISSION_PARK_PERPENDICULAR)
BEHAVIOUR_TALL_ONLY = 5            # only vehicles with bbox max z >= 2
BEHAVIOUR_SMALL_ONLY = 8           # only vehicles with bbox <= (2, 4, 1.5)
BEHAVIOUR_SMALL_ONLY_WAIT = 9      # small only + 4.5 s stop at the node
BEHAVIOUR_TALL_ONLY_WAIT = 10      # tall only + 10 s stop at the node

# navi bits  [paths.md §3.2, §5, §7]
N_LANES_TO_MASK = 0x07        # byte 11 bits 0-2: lanes driving towards the attached node
N_LANES_FROM_SHIFT = 3        # byte 11 bits 3-5: lanes driving away from it
N_LIGHT_DIR = 0x40            # byte 11 bit 6: light controls traffic towards the attached node
LIGHT_NONE = 0
LIGHT_NORTH_SOUTH = 1         # CTrafficLights::LightForCars1
LIGHT_WEST_EAST = 2           # CTrafficLights::LightForCars2
L_STATE_MASK = 0x0003         # bytes 12-13 bits 0-1
L_BRIDGE = 0x0004             # bit 2: runtime bridge/train-crossing lights (SetLinksBridgeLights)

# intersection flags (ped links only)
I_ROAD_CROSS = 0x01
I_PED_LIGHT = 0x02

NODE_DTYPE = np.dtype([
    ("next", "<u4"), ("prev", "<u4"),           # runtime pointers (garbage in vanilla, 0 here)
    ("x", "<i2"), ("y", "<i2"), ("z", "<i2"),   # world * 8
    ("search", "<i2"),                          # must be 0x7FFE
    ("base_link", "<i2"),
    ("area", "<u2"), ("node", "<u2"),
    ("width", "u1"),                            # world * 16
    ("flood", "u1"),
    ("flags", "<u4"),
])
NAVI_DTYPE = np.dtype([
    ("x", "<i2"), ("y", "<i2"),                 # world * 8
    ("area", "<u2"), ("node", "<u2"),           # attached node
    ("dir_x", "i1"), ("dir_y", "i1"),           # unit * 100, towards the attached node
    ("width", "u1"),                            # median half-width * 16
    ("lanes", "u1"),
    ("light", "<u2"),
])
LINK_DTYPE = np.dtype([("area", "<u2"), ("node", "<u2")])
assert NODE_DTYPE.itemsize == 28 and NAVI_DTYPE.itemsize == 14 and LINK_DTYPE.itemsize == 4

HEADER = struct.Struct("<5I")
LINK_FILLER = (0xFFFF, 0x0000)  # vanilla filler; area 0xFFFF marks a free dynamic-link block


class PathError(ValueError):
    """Raised for invalid input networks or malformed files."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _round_half_away(v: float) -> int:
    return int(math.floor(v + 0.5)) if v >= 0 else -int(math.floor(-v + 0.5))


def _quant(v: float, scale: float = COORD_SCALE) -> int:
    return _round_half_away(v * scale)


def _region(q: int) -> int:
    """Game region (column/row) of a quantized coordinate (int16 eighths)."""
    r = int((q / COORD_SCALE - WORLD_MIN) // AREA_SIZE)
    return min(max(r, 0), AREAS_X - 1)


def area_of(x: float, y: float) -> int:
    """Area id the game computes for world (x, y) (FindRegionForCoors)."""
    return _region(_quant(x)) + AREAS_X * _region(_quant(y))


def area_bounds(area_id: int) -> tuple[float, float, float, float]:
    """(min_x, min_y, max_x, max_y) of an area; max is exclusive except at the map edge."""
    cx, cy = area_id % AREAS_X, area_id // AREAS_X
    return (WORLD_MIN + cx * AREA_SIZE, WORLD_MIN + cy * AREA_SIZE,
            WORLD_MIN + (cx + 1) * AREA_SIZE, WORLD_MIN + (cy + 1) * AREA_SIZE)


def area_file_name(area_id: int) -> str:
    return f"nodes{area_id}.dat"


def _one_way_lane_offset(lanes_to: int, lanes_from: int, width_raw: int) -> float:
    """CCarPathLink::OneWayLaneOffset (0x44DB00), in lanes."""
    if lanes_to == 0:
        return 0.5 - lanes_from * 0.5
    if lanes_from == 0:
        return 0.5 - lanes_to * 0.5
    return width_raw * 0.011574074 + 0.5   # = width / 16 / 5.4 + 0.5


def lane_center_offsets(lanes_this: int, lanes_other: int, median: float = 0.0) -> list[float]:
    """Lateral offsets (units, + = right of travel) of the lane centres used by
    traffic in one direction of a link, measured from the navi line.

    ``lanes_this``: lanes in the direction of travel, ``lanes_other``: lanes of
    the opposite direction, ``median``: navi width (half-width of the central
    reservation, only used when both directions have lanes)."""
    if lanes_this <= 0:
        return []
    raw = min(max(_round_half_away(median * WIDTH_SCALE), 0), MAX_NAVI_WIDTH_RAW)
    off = _one_way_lane_offset(lanes_this, lanes_other, raw) if lanes_other else 0.5 - lanes_this * 0.5
    return [(k + off) * LANE_WIDTH for k in range(lanes_this)]


def carriageway_extents(lanes_ab: int, lanes_ba: int, median: float = 0.0) -> tuple[float, float]:
    """(left, right) lateral extent of all lanes relative to the a->b direction
    of a link (left is negative).  Lanes are LANE_WIDTH wide, centred on
    ``lane_center_offsets``."""
    if lanes_ab and lanes_ba:
        return (-(median + LANE_WIDTH * lanes_ba), median + LANE_WIDTH * lanes_ab)
    n = lanes_ab or lanes_ba
    return (-LANE_WIDTH * n / 2.0, LANE_WIDTH * n / 2.0)


def recommended_road_width(lanes_ab: int, lanes_ba: int, median: float = 0.0,
                           shoulder: float = 0.5) -> float:
    """Curb-to-curb width for a road mesh carrying the given lanes."""
    left, right = carriageway_extents(lanes_ab, lanes_ba, median)
    return (right - left) + 2.0 * shoulder


def decode_node_flags(flags: int) -> dict:
    flags = int(flags)
    return {
        "links": flags & F_LINKS_MASK,
        "dead_end": bool(flags & F_DEAD_END),
        "switched_off": bool(flags & F_SWITCHED_OFF),
        "roadblock": bool(flags & F_ROADBLOCK),
        "water": bool(flags & F_WATER),
        "switched_off_original": bool(flags & F_SWITCHED_OFF_ORIG),
        "bit9": bool(flags & F_UNUSED_9),
        "dont_wander": bool(flags & F_DONT_WANDER),
        "bit11": bool(flags & (1 << 11)),
        "speed": (flags >> SPEED_SHIFT) & 3,
        "bits14_15": (flags >> 14) & 3,
        "spawn": (flags >> SPAWN_SHIFT) & 15,
        "behaviour": (flags >> BEHAVIOUR_SHIFT) & 15,
        "bits24_31": (flags >> 24) & 0xFF,
    }


def decode_navi(rec) -> dict:
    lanes = int(rec["lanes"])
    light = int(rec["light"])
    return {
        "x": int(rec["x"]) / COORD_SCALE, "y": int(rec["y"]) / COORD_SCALE,
        "attached": (int(rec["area"]), int(rec["node"])),
        "dir": (int(rec["dir_x"]) / DIR_SCALE, int(rec["dir_y"]) / DIR_SCALE),
        "width": int(rec["width"]) / WIDTH_SCALE,
        "lanes_to_attached": lanes & N_LANES_TO_MASK,
        "lanes_from_attached": (lanes >> N_LANES_FROM_SHIFT) & 7,
        "light_towards_attached": bool(lanes & N_LIGHT_DIR),
        "lanes_bit7": bool(lanes & 0x80),
        "light_group": light & L_STATE_MASK,
        "bridge_lights": bool(light & L_BRIDGE),
        "light_high_bits": light >> 3,
    }


def _pack_navi_link(area: int, index: int) -> int:
    return ((area & 0x3F) << 10) | (index & 0x3FF)


def _unpack_navi_link(value: int) -> tuple[int, int]:
    value = int(value)
    return value >> 10, value & 0x3FF


# --------------------------------------------------------------------------
# low level: one area file  [paths.md §3]
# --------------------------------------------------------------------------

@dataclass
class AreaData:
    """Contents of one ``nodesN.dat`` as the game reads it (CPathFind::LoadPathFindData)."""

    area_id: int
    nodes: np.ndarray                 # NODE_DTYPE, vehicle nodes first
    num_vehicle_nodes: int
    navis: np.ndarray                 # NAVI_DTYPE
    links: np.ndarray                 # LINK_DTYPE, len == num_links (reserved tail excluded)
    navi_links: np.ndarray            # uint16
    link_lengths: np.ndarray          # uint8
    intersections: np.ndarray         # uint8
    # only set by parse_area(): what the file had in the 192 reserved slots / header
    link_reserve: np.ndarray | None = None
    length_reserve: np.ndarray | None = None
    intersection_reserve: np.ndarray | None = None
    header: tuple[int, int, int, int, int] | None = None
    trailing: bytes = b""

    @classmethod
    def empty(cls, area_id: int) -> "AreaData":
        return cls(area_id, np.zeros(0, NODE_DTYPE), 0, np.zeros(0, NAVI_DTYPE),
                   np.zeros(0, LINK_DTYPE), np.zeros(0, "<u2"), np.zeros(0, "u1"), np.zeros(0, "u1"))

    @property
    def num_nodes(self) -> int:
        return int(len(self.nodes))

    @property
    def num_ped_nodes(self) -> int:
        return self.num_nodes - self.num_vehicle_nodes

    @property
    def num_navis(self) -> int:
        return int(len(self.navis))

    @property
    def num_links(self) -> int:
        return int(len(self.links))

    def node_links(self, index: int) -> range:
        rec = self.nodes[index]
        base = int(rec["base_link"])
        return range(base, base + (int(rec["flags"]) & F_LINKS_MASK))

    def is_vehicle(self, index: int) -> bool:
        return index < self.num_vehicle_nodes

    def expected_size(self) -> int:
        n = HEADER.size + 28 * self.num_nodes + 14 * self.num_navis
        if self.num_links:
            n += 8 * self.num_links + 6 * DYNAMIC_LINK_SLOTS
        return n

    def to_bytes(self) -> bytes:
        """Serialise exactly as LoadPathFindData reads it.  An area without
        links gets no link arrays at all (the loader skips them)."""
        nl = self.num_links
        if not (len(self.navi_links) == len(self.link_lengths) == len(self.intersections) == nl):
            raise PathError(f"area {self.area_id}: link arrays have different lengths")
        parts = [HEADER.pack(self.num_nodes, self.num_vehicle_nodes, self.num_ped_nodes,
                             self.num_navis, nl),
                 np.ascontiguousarray(self.nodes, NODE_DTYPE).tobytes(),
                 np.ascontiguousarray(self.navis, NAVI_DTYPE).tobytes()]
        if nl:
            filler = np.zeros(DYNAMIC_LINK_SLOTS, LINK_DTYPE)
            filler["area"], filler["node"] = LINK_FILLER
            parts += [np.ascontiguousarray(self.links, LINK_DTYPE).tobytes(), filler.tobytes(),
                      np.ascontiguousarray(self.navi_links, "<u2").tobytes(),
                      np.ascontiguousarray(self.link_lengths, "u1").tobytes(), bytes(DYNAMIC_LINK_SLOTS),
                      np.ascontiguousarray(self.intersections, "u1").tobytes(), bytes(DYNAMIC_LINK_SLOTS)]
        data = b"".join(parts)
        assert len(data) == self.expected_size()
        return data


def parse_area(data: bytes, area_id: int, *, allow_padding: bool = True) -> AreaData:
    """Parse one area file.  ``allow_padding`` accepts trailing bytes (e.g. the
    2048-byte sector padding of an IMG entry); they are kept in ``trailing``."""
    if len(data) < HEADER.size:
        raise PathError(f"area {area_id}: file too short for header ({len(data)} bytes)")
    header = HEADER.unpack_from(data, 0)
    n, nveh, nped, nnavi, nlinks = header
    if n != nveh + nped:
        raise PathError(f"area {area_id}: header node count {n} != {nveh} vehicle + {nped} ped")
    off = HEADER.size
    need = off + 28 * n + 14 * nnavi + ((8 * nlinks + 6 * DYNAMIC_LINK_SLOTS) if nlinks else 0)
    if len(data) < need:
        raise PathError(f"area {area_id}: truncated, {len(data)} bytes < {need} expected")
    buf = memoryview(data)
    nodes = np.frombuffer(buf, NODE_DTYPE, n, off).copy(); off += 28 * n
    navis = np.frombuffer(buf, NAVI_DTYPE, nnavi, off).copy(); off += 14 * nnavi
    if nlinks:
        links_all = np.frombuffer(buf, LINK_DTYPE, nlinks + DYNAMIC_LINK_SLOTS, off).copy()
        off += 4 * (nlinks + DYNAMIC_LINK_SLOTS)
        navi_links = np.frombuffer(buf, "<u2", nlinks, off).copy(); off += 2 * nlinks
        lens_all = np.frombuffer(buf, "u1", nlinks + DYNAMIC_LINK_SLOTS, off).copy()
        off += nlinks + DYNAMIC_LINK_SLOTS
        ints_all = np.frombuffer(buf, "u1", nlinks + DYNAMIC_LINK_SLOTS, off).copy()
        off += nlinks + DYNAMIC_LINK_SLOTS
        area = AreaData(area_id, nodes, nveh, navis, links_all[:nlinks], navi_links,
                        lens_all[:nlinks], ints_all[:nlinks],
                        links_all[nlinks:], lens_all[nlinks:], ints_all[nlinks:], header)
    else:
        area = AreaData(area_id, nodes, nveh, navis, np.zeros(0, LINK_DTYPE), np.zeros(0, "<u2"),
                        np.zeros(0, "u1"), np.zeros(0, "u1"), header=header)
    area.trailing = bytes(data[off:])
    if area.trailing and not allow_padding:
        raise PathError(f"area {area_id}: {len(area.trailing)} unexpected trailing bytes")
    return area


def read_area_files(directory: str | os.PathLike, *, allow_missing: bool = False) -> list[AreaData | None]:
    """Read nodes0.dat..nodes63.dat (file names matched case-insensitively)."""
    directory = Path(directory)
    by_lower = {p.name.lower(): p for p in directory.iterdir() if p.is_file()}
    areas: list[AreaData | None] = []
    for i in range(NUM_AREAS):
        p = by_lower.get(area_file_name(i))
        if p is None:
            if not allow_missing:
                raise PathError(f"{directory}: missing {area_file_name(i)}")
            areas.append(None)
            continue
        areas.append(parse_area(p.read_bytes(), i))
    return areas


# --------------------------------------------------------------------------
# roadblox.dat  [paths.md §13]
# --------------------------------------------------------------------------

def write_roadblox(path: str | os.PathLike | None, addresses: Sequence[tuple[int, int]]) -> bytes:
    """``data\\paths\\roadblox.dat``: int32 count + 325 CNodeAddress (area, node).
    Every listed node must be a land vehicle node with >= 2 links.  Returns
    the bytes (and writes them when ``path`` is given)."""
    if len(addresses) > MAX_ROADBLOCKS:
        raise PathError(f"{len(addresses)} police roadblock nodes > {MAX_ROADBLOCKS}")
    arr = np.zeros(MAX_ROADBLOCKS, LINK_DTYPE)
    for i, (a, n) in enumerate(addresses):
        arr[i] = (a, n)
    data = struct.pack("<i", len(addresses)) + arr.tobytes()
    if path is not None:
        Path(path).write_bytes(data)
    return data


def read_roadblox(data: bytes) -> list[tuple[int, int]]:
    if len(data) < 4 + 4 * MAX_ROADBLOCKS:
        raise PathError(f"roadblox.dat too short ({len(data)} bytes, need {4 + 4 * MAX_ROADBLOCKS})")
    count = struct.unpack_from("<i", data, 0)[0]
    if not 0 <= count <= MAX_ROADBLOCKS:
        raise PathError(f"roadblox.dat count {count} out of range")
    arr = np.frombuffer(data, LINK_DTYPE, MAX_ROADBLOCKS, 4)
    return [(int(a), int(n)) for a, n in arr[:count]]


# --------------------------------------------------------------------------
# high level input  [paths.md §4, §16]
# --------------------------------------------------------------------------

@dataclass
class CarNode:
    """A vehicle path node.  ``z`` must be the road (or water) surface height."""

    x: float
    y: float
    z: float
    width: float = 0.0          # node path width, units (/16 in file); for cars = median half-width
    spawn: int = 15             # 0..15, ambient car spawn probability (spawn+1)/16
    switched_off: bool = False  # no ambient spawning, peds/cars avoid entering ("emergency only")
    speed: int = SPEED_NORMAL   # SPEED_SLOW / SPEED_NORMAL / SPEED_HIGHWAY
    boat: bool = False          # water node; must sit on water defined in water.dat
    roadblock: bool = False     # flag bit 6: wandering cars never enter from a normal node
    dont_wander: bool = False   # flag bit 10 (mission-created cruising cars avoid it)
    behaviour: int = BEHAVIOUR_NONE   # special function (parking etc.), 0 = normal / not for parking
    police_roadblock: bool = False    # list in roadblox.dat (needs exactly 2 links ideally)
    tag: object = None          # user data, carried to CompiledPaths.car_tags


@dataclass
class PedNode:
    x: float
    y: float
    z: float                    # walking surface height (spawn rejects > 3 units off the ground)
    width: float = 1.0          # peds spread about +-width around the node (vanilla mostly 1.0)
    spawn: int = 15             # 0..15, ped spawn probability spawn/15
    switched_off: bool = False  # no ped spawning, wandering peds avoid entering
    dont_wander: bool = False
    tag: object = None


@dataclass
class CarLink:
    """Undirected car link a-b.  ``lanes_ab`` = lanes for traffic driving from
    a to b (they lie right of the a->b direction), ``lanes_ba`` the reverse."""

    a: int
    b: int
    lanes_ab: int = 1
    lanes_ba: int = 1
    median: float | None = None       # navi width (two-way only); None = attached node's width
    light_toward: int | None = None   # node (a or b): traffic driving towards it obeys a light
    light_group: int | None = None    # LIGHT_NORTH_SOUTH / LIGHT_WEST_EAST / None = from direction


@dataclass
class PedLink:
    a: int
    b: int
    crossing: bool | None = None      # road-cross flag; None = auto (segment crosses a car link)
    ped_light: bool | None = None     # ped traffic light; None = auto (crossing at a signal)


@dataclass
class PathNetwork:
    car_nodes: list[CarNode] = field(default_factory=list)
    ped_nodes: list[PedNode] = field(default_factory=list)
    car_links: list[CarLink] = field(default_factory=list)
    ped_links: list[PedLink] = field(default_factory=list)

    def add_car_node(self, x: float, y: float, z: float, **kw) -> int:
        self.car_nodes.append(CarNode(x, y, z, **kw))
        return len(self.car_nodes) - 1

    def add_ped_node(self, x: float, y: float, z: float, **kw) -> int:
        self.ped_nodes.append(PedNode(x, y, z, **kw))
        return len(self.ped_nodes) - 1

    def add_car_link(self, a: int, b: int, lanes_ab: int = 1, lanes_ba: int = 1, **kw) -> int:
        self.car_links.append(CarLink(a, b, lanes_ab, lanes_ba, **kw))
        return len(self.car_links) - 1

    def add_ped_link(self, a: int, b: int, **kw) -> int:
        self.ped_links.append(PedLink(a, b, **kw))
        return len(self.ped_links) - 1

    def add_car_polyline(self, nodes: Sequence[int], lanes_fwd: int = 1, lanes_bwd: int = 1, **kw) -> list[int]:
        """Link consecutive nodes; ``lanes_fwd`` runs along the sequence."""
        return [self.add_car_link(a, b, lanes_fwd, lanes_bwd, **kw) for a, b in zip(nodes, nodes[1:])]

    def add_ped_polyline(self, nodes: Sequence[int], **kw) -> list[int]:
        return [self.add_ped_link(a, b, **kw) for a, b in zip(nodes, nodes[1:])]

    # -- editing helpers --------------------------------------------------

    def split_car_link(self, link_index: int, t: float, z: float | None = None) -> int:
        """Insert a node at fraction ``t`` (from a) of a car link; returns the new
        node index.  The original link keeps a..new, a new link new..b is
        appended; a light stays on the piece adjacent to its node."""
        ln = self.car_links[link_index]
        na, nb = self.car_nodes[ln.a], self.car_nodes[ln.b]
        m = self.add_car_node(*_lerp3(na, nb, t, z), **_car_attrs_between(na, nb))
        second = dataclasses.replace(ln, a=m, b=ln.b)
        first = dataclasses.replace(ln, b=m)
        first.light_toward = ln.light_toward if ln.light_toward == ln.a else None
        second.light_toward = ln.light_toward if ln.light_toward == ln.b else None
        if ln.light_toward is None:
            first.light_group = second.light_group = ln.light_group
        self.car_links[link_index] = first
        self.car_links.append(second)
        return m

    def signalize_junction(self, node: int, approach_length: float | None = None,
                           group_for: Callable[[int], int | None] | None = None,
                           min_piece: float = 4.0,
                           skip_from: Callable[[int], bool] | None = None) -> list[int]:
        """Put traffic lights on every approach into ``node``.

        Each incident car link longer than ``approach_length + min_piece`` is
        split so that the light sits on a short approach segment (vanilla: ~17
        units, navi ~8 units before the junction centre; cars stop within the
        12 units before that navi).  ``approach_length=None`` derives it from
        the widest crossing road: 2 * (half width + 3).  ``group_for(link)`` may
        force a light group; default: from the travel direction (N-S travel ->
        LIGHT_NORTH_SOUTH, else LIGHT_WEST_EAST).  ``skip_from(other_node)`` may
        exclude approaches (e.g. links inside a multi-node junction).  Returns the
        light links."""
        incident = [i for i, l in enumerate(self.car_links) if node in (l.a, l.b)]
        if skip_from is not None:
            incident = [i for i in incident
                        if not skip_from(self.car_links[i].b if self.car_links[i].a == node else self.car_links[i].a)]
        if approach_length is None:
            half = 0.0
            for i in incident:
                l = self.car_links[i]
                left, right = carriageway_extents(l.lanes_ab, l.lanes_ba, l.median or 0.0)
                half = max(half, -left, right)
            approach_length = min(max(2.0 * (half + 3.0), 12.0), 40.0)
        result = []
        for i in incident:
            l = self.car_links[i]
            lanes_in = l.lanes_ba if l.a == node else l.lanes_ab
            if lanes_in == 0:
                continue                      # nobody drives towards the junction here
            j = self.car_nodes[node]
            other = self.car_nodes[l.b if l.a == node else l.a]
            length = math.hypot(other.x - j.x, other.y - j.y)
            if length > approach_length + min_piece:
                t = approach_length / length
                frac = t if l.a == node else 1.0 - t
                self.split_car_link(i, frac)
                l = self.car_links[i]
                if node not in (l.a, l.b):    # split moved the junction end to the new link
                    i = len(self.car_links) - 1
                    l = self.car_links[i]
            l.light_toward = node
            l.light_group = group_for(i) if group_for else None
            result.append(i)
        return result


def _lerp3(na, nb, t: float, z: float | None) -> tuple[float, float, float]:
    x = na.x + (nb.x - na.x) * t
    y = na.y + (nb.y - na.y) * t
    return x, y, (na.z + (nb.z - na.z) * t) if z is None else z


def _car_attrs_between(na: CarNode, nb: CarNode) -> dict:
    return dict(width=min(na.width, nb.width), spawn=min(na.spawn, nb.spawn),
                switched_off=na.switched_off or nb.switched_off, speed=min(na.speed, nb.speed),
                boat=na.boat, roadblock=na.roadblock and nb.roadblock,
                dont_wander=na.dont_wander and nb.dont_wander)


def _ped_attrs_between(na: PedNode, nb: PedNode) -> dict:
    return dict(width=min(na.width, nb.width), spawn=min(na.spawn, nb.spawn),
                switched_off=na.switched_off or nb.switched_off,
                dont_wander=na.dont_wander and nb.dont_wander)


# --------------------------------------------------------------------------
# compiler  [paths.md §5, §9-§11, §16]
# --------------------------------------------------------------------------

@dataclass
class CompiledPaths:
    areas: list[AreaData]                         # 64 entries
    car_addresses: list[tuple[int, int]]          # per car node (input order, split nodes appended)
    ped_addresses: list[tuple[int, int]]
    car_tags: list[object]
    ped_tags: list[object]
    roadblock_addresses: list[tuple[int, int]]
    warnings: list[str]
    stats: dict
    car_links: list[CarLink] = field(default_factory=list)       # final links (after splitting)
    ped_links: list[PedLink] = field(default_factory=list)
    navi_addresses: list[tuple[int, int]] = field(default_factory=list)  # per final car link

    def file_bytes(self, area_id: int) -> bytes:
        return self.areas[area_id].to_bytes()

    def write_roadblox(self, path: str | os.PathLike) -> bytes:
        return write_roadblox(path, self.roadblock_addresses)

    def write(self, out_dir: str | os.PathLike) -> list[Path]:
        """Write all 64 area files (empty areas included)."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        paths = []
        for area in self.areas:
            p = out / area_file_name(area.area_id)
            p.write_bytes(area.to_bytes())
            paths.append(p)
        return paths


def compile_network(net: PathNetwork, *, max_link_length: float = 100.0,
                    dead_end_flags: bool = True, detect_crossings: bool = True,
                    spill_navis: bool = True, drop_isolated: bool = False,
                    z_func: Callable[[float, float], float] | None = None) -> CompiledPaths:
    """Turn a PathNetwork into the 64 area files.

    max_link_length  links longer than this (3D, <= 255) are split into equal
                     pieces; inserted nodes get z from ``z_func(x, y)`` if given.
    dead_end_flags   set flag bit 4 on dead-end branches (vanilla behaviour).
    detect_crossings ped links with crossing=None get the road-cross flag when
                     they cross a car link at a similar height.
    spill_navis      when an area already holds 1024 navis, store the navi in
                     an endpoint's area instead (legal: navis are addressed by
                     their own 6-bit area); otherwise raise.
    drop_isolated    drop nodes without links (else PathError; the game divides
                     by the link count in CCarCtrl::InitSequence).
    """
    if not 0 < max_link_length <= MAX_LINK_LENGTH:
        raise PathError(f"max_link_length must be in (0, {MAX_LINK_LENGTH}]")
    warnings: list[str] = []
    cars = [dataclasses.replace(n) for n in net.car_nodes]
    peds = [dataclasses.replace(n) for n in net.ped_nodes]
    clinks = [dataclasses.replace(l) for l in net.car_links]
    plinks = [dataclasses.replace(l) for l in net.ped_links]

    _check_input(cars, peds, clinks, plinks)
    if drop_isolated:
        cars, clinks, dropped_c = _drop_isolated(cars, clinks)
        peds, plinks, dropped_p = _drop_isolated(peds, plinks)
        if dropped_c or dropped_p:
            warnings.append(f"dropped {dropped_c} isolated car nodes and {dropped_p} isolated ped nodes")
    n_split = _split_long_links(cars, clinks, max_link_length, z_func, True)
    n_split += _split_long_links(peds, plinks, max_link_length, z_func, False)
    if n_split:
        warnings.append(f"split {n_split} links longer than {max_link_length} units")

    # quantize + areas
    cq = [(_quant(n.x), _quant(n.y), _quant(n.z)) for n in cars]
    pq = [(_quant(n.x), _quant(n.y), _quant(n.z)) for n in peds]
    for kind, qs in (("car", cq), ("ped", pq)):
        for i, q in enumerate(qs):
            if not all(-COORD_LIMIT_Q - 1 <= v <= COORD_LIMIT_Q for v in q):
                raise PathError(f"{kind} node {i}: coordinate out of int16/8 range")
    car_area = [_region(q[0]) + AREAS_X * _region(q[1]) for q in cq]
    ped_area = [_region(q[0]) + AREAS_X * _region(q[1]) for q in pq]

    # adjacency (insertion order) and degree checks
    cadj: list[list[tuple[int, int]]] = [[] for _ in cars]
    for li, l in enumerate(clinks):
        cadj[l.a].append((l.b, li)); cadj[l.b].append((l.a, li))
    padj: list[list[tuple[int, int]]] = [[] for _ in peds]
    for li, l in enumerate(plinks):
        padj[l.a].append((l.b, li)); padj[l.b].append((l.a, li))
    errors = []
    for kind, adj in (("car", cadj), ("ped", padj)):
        for i, nb in enumerate(adj):
            if not nb:
                errors.append(f"{kind} node {i} has no links (use drop_isolated=True to drop it)")
            elif len(nb) > MAX_LINKS_PER_NODE:
                errors.append(f"{kind} node {i} has {len(nb)} links > {MAX_LINKS_PER_NODE}")
            elif len(nb) > RECOMMENDED_MAX_LINKS:
                warnings.append(f"{kind} node {i} has {len(nb)} links (vanilla max 4 cars / 6 peds)")
    if errors:
        raise PathError(_join(errors))

    # node ids: per area land vehicle, water vehicle, ped
    order: list[list[tuple[str, int]]] = [[] for _ in range(NUM_AREAS)]
    for i, n in enumerate(cars):
        if not n.boat:
            order[car_area[i]].append(("c", i))
    for i, n in enumerate(cars):
        if n.boat:
            order[car_area[i]].append(("c", i))
    nveh = [len(o) for o in order]
    for i in range(len(peds)):
        order[ped_area[i]].append(("p", i))
    car_addr: list[tuple[int, int]] = [(-1, -1)] * len(cars)
    ped_addr: list[tuple[int, int]] = [(-1, -1)] * len(peds)
    for a, lst in enumerate(order):
        if len(lst) > MAX_NODES_PER_AREA:
            raise PathError(f"area {a}: {len(lst)} nodes > {MAX_NODES_PER_AREA}")
        for nid, (kind, i) in enumerate(lst):
            if kind == "c":
                car_addr[i] = (a, nid)
            else:
                ped_addr[i] = (a, nid)

    # flood fill (vehicle and ped graphs numbered independently)
    car_flood, ncomp_c, shared_c = _flood_fill(len(cars), clinks)
    ped_flood, ncomp_p, shared_p = _flood_fill(len(peds), plinks)
    if shared_c or shared_p:
        warnings.append(f"more than {MAX_FLOOD_FILL - 1} components: {shared_c} car / {shared_p} ped "
                        f"components share flood fill {MAX_FLOOD_FILL} (route searches between "
                        "them fail slowly instead of instantly)")

    # dead-end flags
    dead = [False] * len(cars)
    if dead_end_flags:
        dead = _dead_end_nodes(cars, cadj)
        bad = _walk_guard(cars, cadj, dead)
        if bad:
            warnings.append(f"cleared dead-end flag on {len(bad)} nodes whose dead-end walk would not terminate")
            for i in bad:
                dead[i] = False

    # traffic lights / crossings
    light_nodes = {l.light_toward for l in clinks if l.light_toward is not None}
    if detect_crossings and plinks:
        _auto_crossings(cars, peds, clinks, plinks, light_nodes)
    for l in plinks:
        if l.ped_light is None:
            l.ped_light = False
        if l.crossing is None:
            l.crossing = False
        if l.ped_light:
            l.crossing = True

    # navis
    navi_lists: list[list[tuple]] = [[] for _ in range(NUM_AREAS)]
    navi_addr: list[tuple[int, int]] = [(-1, -1)] * len(clinks)
    n_spilled = 0
    for li, l in enumerate(clinks):
        A, B = car_addr[l.a], car_addr[l.b]
        if A < B:
            att, oth, lanes_to, lanes_from = l.a, l.b, l.lanes_ba, l.lanes_ab
        else:
            att, oth, lanes_to, lanes_from = l.b, l.a, l.lanes_ab, l.lanes_ba
        qa, qo = cq[att], cq[oth]
        mx = _round_half_away((qa[0] + qo[0]) / 2.0)
        my = _round_half_away((qa[1] + qo[1]) / 2.0)
        dx, dy = qa[0] - qo[0], qa[1] - qo[1]
        dl = math.hypot(dx, dy)
        dir_x = max(-100, min(100, _round_half_away(dx / dl * DIR_SCALE)))
        dir_y = max(-100, min(100, _round_half_away(dy / dl * DIR_SCALE)))
        width = cars[att].width if l.median is None else l.median
        width_raw = min(max(_round_half_away(width * WIDTH_SCALE), 0), MAX_NAVI_WIDTH_RAW)
        lanes = (lanes_to & 7) | ((lanes_from & 7) << N_LANES_FROM_SHIFT)
        light = 0
        if l.light_toward is not None:
            towards_attached = l.light_toward == att
            if towards_attached:
                lanes |= N_LIGHT_DIR
            ctrl_from = oth if towards_attached else att
            group = l.light_group
            if group is None:
                tx = cq[l.light_toward][0] - cq[ctrl_from][0]
                ty = cq[l.light_toward][1] - cq[ctrl_from][1]
                group = LIGHT_NORTH_SOUTH if abs(ty) >= abs(tx) else LIGHT_WEST_EAST
            light = group & L_STATE_MASK
            if (lanes_to if towards_attached else lanes_from) == 0:
                warnings.append(f"car link {li}: light controls a direction without lanes")
        home = _region(mx) + AREAS_X * _region(my)
        target = home
        if len(navi_lists[home]) >= MAX_NAVI_PER_AREA:
            target = -1
            if spill_navis:
                for cand in sorted({car_addr[att][0], car_addr[oth][0]},
                                   key=lambda c: len(navi_lists[c])):
                    if len(navi_lists[cand]) < MAX_NAVI_PER_AREA:
                        target = cand
                        break
            if target < 0:
                raise PathError(f"area {home}: more than {MAX_NAVI_PER_AREA} navi nodes "
                                "(car link segments); simplify the road graph there")
            n_spilled += 1
        navi_addr[li] = (target, len(navi_lists[target]))
        navi_lists[target].append((mx, my, car_addr[att][0], car_addr[att][1], dir_x, dir_y,
                                   width_raw, lanes, light))
    if n_spilled:
        warnings.append(f"{n_spilled} navi nodes stored in an endpoint's area because their own "
                        f"area was full ({MAX_NAVI_PER_AREA})")

    # per area arrays
    areas = []
    for a in range(NUM_AREAS):
        lst = order[a]
        nodes = np.zeros(len(lst), NODE_DTYPE)
        links, nlinks, lens, ints = [], [], [], []
        for nid, (kind, i) in enumerate(lst):
            if kind == "c":
                n, q, adj, addr_of = cars[i], cq[i], cadj[i], car_addr
                flags = (len(adj) | (F_DEAD_END if dead[i] else 0)
                         | (F_SWITCHED_OFF | F_SWITCHED_OFF_ORIG if n.switched_off else 0)
                         | (F_ROADBLOCK if n.roadblock else 0) | (F_WATER if n.boat else 0)
                         | (F_DONT_WANDER if n.dont_wander else 0)
                         | ((n.speed & 3) << SPEED_SHIFT) | ((n.spawn & 15) << SPAWN_SHIFT)
                         | ((n.behaviour & 15) << BEHAVIOUR_SHIFT))
                flood = car_flood[i]
                qs = cq
            else:
                n, q, adj, addr_of = peds[i], pq[i], padj[i], ped_addr
                flags = (len(adj) | (F_SWITCHED_OFF | F_SWITCHED_OFF_ORIG if n.switched_off else 0)
                         | (F_DONT_WANDER if n.dont_wander else 0) | ((n.spawn & 15) << SPAWN_SHIFT))
                flood = ped_flood[i]
                qs = pq
            rec = nodes[nid]
            rec["x"], rec["y"], rec["z"] = q
            rec["search"] = SEARCH_DIST_INIT
            rec["base_link"] = len(links)
            rec["area"], rec["node"] = a, nid
            rec["width"] = min(max(_round_half_away(n.width * WIDTH_SCALE), 0), MAX_NODE_WIDTH_RAW)
            rec["flood"] = flood
            rec["flags"] = flags
            for other, li in adj:
                links.append(addr_of[other])
                o = qs[other]
                d = math.sqrt((q[0] - o[0]) ** 2 + (q[1] - o[1]) ** 2 + (q[2] - o[2]) ** 2) / COORD_SCALE
                lens.append(min(max(int(d), 1), MAX_LINK_LENGTH))
                if kind == "c":
                    nlinks.append(_pack_navi_link(*navi_addr[li]))
                    ints.append(0)
                else:
                    nlinks.append(NAVI_LINK_PED)
                    pl = plinks[li]
                    ints.append((I_ROAD_CROSS if pl.crossing else 0) | (I_PED_LIGHT if pl.ped_light else 0))
        if len(links) > MAX_LINKS_PER_AREA:
            raise PathError(f"area {a}: {len(links)} links > {MAX_LINKS_PER_AREA}")
        navis = np.array(navi_lists[a], NAVI_DTYPE) if navi_lists[a] else np.zeros(0, NAVI_DTYPE)
        areas.append(AreaData(a, nodes, nveh[a], navis,
                              np.array(links, LINK_DTYPE) if links else np.zeros(0, LINK_DTYPE),
                              np.array(nlinks, "<u2"), np.array(lens, "u1"), np.array(ints, "u1")))

    roadblocks = []
    for i, n in enumerate(cars):
        if n.police_roadblock:
            if n.boat or len(cadj[i]) < 2:
                raise PathError(f"car node {i}: police roadblock nodes must be land nodes with >= 2 links")
            roadblocks.append(car_addr[i])
    if len(roadblocks) > MAX_ROADBLOCKS:
        raise PathError(f"{len(roadblocks)} police roadblock nodes > {MAX_ROADBLOCKS}")

    report = validate_areas(areas, strict=True, roadblocks=roadblocks)
    if report.errors:
        raise PathError("compiled data failed validation:\n" + _join(report.errors))
    warnings.extend(report.warnings)
    stats = dict(report.stats)
    stats.update(car_components=ncomp_c, ped_components=ncomp_p, split_links=n_split,
                 spilled_navis=n_spilled, dead_end_nodes=sum(dead))
    for a, area in enumerate(areas):
        if area.num_navis > 0.9 * MAX_NAVI_PER_AREA:
            warnings.append(f"area {a}: {area.num_navis} navi nodes, close to the {MAX_NAVI_PER_AREA} limit")
    return CompiledPaths(areas, car_addr, ped_addr, [n.tag for n in cars], [n.tag for n in peds],
                         roadblocks, warnings, stats, clinks, plinks, navi_addr)


def _join(msgs: list[str], limit: int = 50) -> str:
    extra = f"\n... and {len(msgs) - limit} more" if len(msgs) > limit else ""
    return "\n".join(msgs[:limit]) + extra


def _check_input(cars, peds, clinks, plinks) -> None:
    errors = []
    for kind, nodes in (("car", cars), ("ped", peds)):
        for i, n in enumerate(nodes):
            for name in ("x", "y"):
                v = getattr(n, name)
                if not (math.isfinite(v) and WORLD_MIN <= v <= -WORLD_MIN):
                    errors.append(f"{kind} node {i}: {name}={v} outside [-3000, 3000]")
            if not (math.isfinite(n.z) and -4096.0 <= n.z <= 4095.875):
                errors.append(f"{kind} node {i}: z={n.z} outside int16/8 range")
            if not 0 <= n.spawn <= 15:
                errors.append(f"{kind} node {i}: spawn {n.spawn} not in 0..15")
            if not 0.0 <= n.width <= MAX_NODE_WIDTH_RAW / WIDTH_SCALE:
                errors.append(f"{kind} node {i}: width {n.width} not in 0..{MAX_NODE_WIDTH_RAW / WIDTH_SCALE}")
        if kind == "car":
            for i, n in enumerate(nodes):
                if n.speed not in (0, 1, 2, 3):
                    errors.append(f"car node {i}: speed {n.speed} not in 0..3")
                if not 0 <= n.behaviour <= 15:
                    errors.append(f"car node {i}: behaviour {n.behaviour} not in 0..15")
    for kind, nodes, links in (("car", cars, clinks), ("ped", peds, plinks)):
        seen = set()
        for li, l in enumerate(links):
            if not (0 <= l.a < len(nodes) and 0 <= l.b < len(nodes)):
                errors.append(f"{kind} link {li}: node index out of range ({l.a}, {l.b})")
                continue
            if l.a == l.b:
                errors.append(f"{kind} link {li}: links node {l.a} to itself")
                continue
            key = (min(l.a, l.b), max(l.a, l.b))
            if key in seen:
                errors.append(f"{kind} link {li}: duplicate link {key}")
            seen.add(key)
            na, nb = nodes[l.a], nodes[l.b]
            if math.hypot(na.x - nb.x, na.y - nb.y) < 0.25:
                errors.append(f"{kind} link {li}: endpoints closer than 0.25 units in 2D")
            if kind == "car":
                if not (0 <= l.lanes_ab <= MAX_LANES and 0 <= l.lanes_ba <= MAX_LANES):
                    errors.append(f"car link {li}: lanes ({l.lanes_ab}, {l.lanes_ba}) not in 0..{MAX_LANES}")
                if na.boat != nb.boat:
                    errors.append(f"car link {li}: links a water node to a land node")
                if l.light_toward not in (None, l.a, l.b):
                    errors.append(f"car link {li}: light_toward must be a, b or None")
                if l.light_group not in (None, LIGHT_NORTH_SOUTH, LIGHT_WEST_EAST):
                    errors.append(f"car link {li}: light_group must be 1, 2 or None")
                if l.median is not None and not 0.0 <= l.median <= MAX_NAVI_WIDTH_RAW / WIDTH_SCALE:
                    errors.append(f"car link {li}: median {l.median} not in 0..{MAX_NAVI_WIDTH_RAW / WIDTH_SCALE}")
    if errors:
        raise PathError(_join(errors))


def _drop_isolated(nodes, links):
    used = set()
    for l in links:
        used.add(l.a); used.add(l.b)
    keep = [i for i in range(len(nodes)) if i in used]
    remap = {old: new for new, old in enumerate(keep)}
    new_links = [dataclasses.replace(l, a=remap[l.a], b=remap[l.b],
                                     **({"light_toward": remap[l.light_toward]}
                                        if getattr(l, "light_toward", None) is not None else {}))
                 for l in links]
    return [nodes[i] for i in keep], new_links, len(nodes) - len(keep)


def _split_long_links(nodes, links, max_len, z_func, is_car) -> int:
    """Split links whose 3D length exceeds max_len (quantization margin 0.5)."""
    count = 0
    limit = max_len - 0.5
    i = 0
    while i < len(links):
        l = links[i]
        na, nb = nodes[l.a], nodes[l.b]
        d = math.dist((na.x, na.y, na.z), (nb.x, nb.y, nb.z))
        if d > limit:
            pieces = math.ceil(d / limit)
            new_ids = []
            for k in range(1, pieces):
                t = k / pieces
                x, y, z = _lerp3(na, nb, t, None)
                if z_func is not None:
                    z = float(z_func(x, y))
                attrs = _car_attrs_between(na, nb) if is_car else _ped_attrs_between(na, nb)
                nodes.append((CarNode if is_car else PedNode)(x, y, z, **attrs))
                new_ids.append(len(nodes) - 1)
            chain = [l.a] + new_ids + [l.b]
            pieces_links = []
            for a, b in zip(chain, chain[1:]):
                p = dataclasses.replace(l, a=a, b=b)
                if is_car:
                    p.light_toward = l.light_toward if l.light_toward in (a, b) else None
                pieces_links.append(p)
            links[i] = pieces_links[0]
            links.extend(pieces_links[1:])
            count += 1
        i += 1
    return count


def _flood_fill(n: int, links) -> tuple[list[int], int, int]:
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for l in links:
        ra, rb = find(l.a), find(l.b)
        if ra != rb:
            parent[ra] = rb
    roots = [find(i) for i in range(n)]
    sizes = Counter(roots)
    first: dict[int, int] = {}
    for i, r in enumerate(roots):
        first.setdefault(r, i)
    # largest component gets 1 (vanilla: main road network = 1); ties by first node
    ranked = sorted(sizes, key=lambda r: (-sizes[r], first[r]))
    value = {}
    shared = 0
    for k, r in enumerate(ranked):
        if k < MAX_FLOOD_FILL - 1:
            value[r] = k + 1
        else:
            value[r] = MAX_FLOOD_FILL
            shared += 1
    if shared == 1:
        shared = 0          # a single component on 255 is still unique
    return [value[r] for r in roots], len(ranked), shared


def _is_normal(n: CarNode) -> bool:
    return n.behaviour == 0 or n.behaviour > 10


def _dead_end_nodes(cars: list[CarNode], cadj) -> list[bool]:
    """Vanilla rule: nodes removed by iterative leaf pruning of the graph of
    normal (behaviour 0 or > 10) vehicle nodes, plus all special nodes."""
    normal = [_is_normal(n) for n in cars]
    deg = [sum(1 for o, _ in cadj[i] if normal[o]) if normal[i] else 0 for i in range(len(cars))]
    removed = [False] * len(cars)
    stack = [i for i in range(len(cars)) if normal[i] and deg[i] <= 1]
    while stack:
        i = stack.pop()
        if removed[i]:
            continue
        removed[i] = True
        for o, _ in cadj[i]:
            if normal[o] and not removed[o]:
                deg[o] -= 1
                if deg[o] <= 1:
                    stack.append(o)
    return [removed[i] or not normal[i] for i in range(len(cars))]


def _dead_end_walk(neighbours: Callable[[object], Iterable[object]], is_normal: Callable[[object], bool],
                   start, prev, limit: int):
    """CPathFind::ThisNodeWillLeadIntoADeadEnd (0x44D310). Returns True/False,
    or None when the walk does not finish within ``limit`` steps."""
    cur, pv = start, prev
    for _ in range(limit):
        nxt = None
        for o in neighbours(cur):
            if o == pv or not is_normal(o):
                continue
            if nxt is not None:
                return False
            nxt = o
        if nxt is None:
            return True
        pv, cur = cur, nxt
    return None


def _walk_guard(cars, cadj, dead) -> list[int]:
    limit = len(cars) + 2
    bad = []
    nb = lambda i: (o for o, _ in cadj[i])
    isn = lambda i: _is_normal(cars[i])
    for i, flag in enumerate(dead):
        if flag and any(_dead_end_walk(nb, isn, i, p, limit) is None for p, _ in cadj[i]):
            bad.append(i)
    return bad


def _auto_crossings(cars, peds, clinks, plinks, light_nodes) -> None:
    """Flag ped links that cross a land car link (2D segment intersection with
    heights within 3 units).  ped_light=None becomes True when the crossed car
    link carries a light or touches a node that lights point at."""
    cell = 32.0
    grid = defaultdict(list)
    for li, l in enumerate(clinks):
        a, b = cars[l.a], cars[l.b]
        if a.boat:
            continue
        for key in _cells(a.x, a.y, b.x, b.y, cell):
            grid[key].append(li)
    for pl in plinks:
        if pl.crossing is not None and pl.ped_light is not None:
            continue
        p, q = peds[pl.a], peds[pl.b]
        hit = None
        cand = set()
        for key in _cells(p.x, p.y, q.x, q.y, cell):
            cand.update(grid.get(key, ()))
        for li in cand:
            l = clinks[li]
            a, b = cars[l.a], cars[l.b]
            t = _seg_intersect(p.x, p.y, q.x, q.y, a.x, a.y, b.x, b.y)
            if t is None:
                continue
            tp, tc = t
            zp = p.z + (q.z - p.z) * tp
            zc = a.z + (b.z - a.z) * tc
            if abs(zp - zc) <= 3.0:
                lit = l.light_toward is not None or l.a in light_nodes or l.b in light_nodes
                hit = lit if hit is None else (hit or lit)
        if pl.crossing is None:
            pl.crossing = hit is not None
        if pl.ped_light is None:
            pl.ped_light = bool(hit) and bool(pl.crossing)


def _cells(x0, y0, x1, y1, cell):
    cx0, cx1 = sorted((int(math.floor(x0 / cell)), int(math.floor(x1 / cell))))
    cy0, cy1 = sorted((int(math.floor(y0 / cell)), int(math.floor(y1 / cell))))
    for cx in range(cx0, cx1 + 1):
        for cy in range(cy0, cy1 + 1):
            yield cx, cy


def _seg_intersect(ax, ay, bx, by, cx, cy, dx, dy):
    rx, ry, sx, sy = bx - ax, by - ay, dx - cx, dy - cy
    den = rx * sy - ry * sx
    if abs(den) < 1e-12:
        return None
    t = ((cx - ax) * sy - (cy - ay) * sx) / den
    u = ((cx - ax) * ry - (cy - ay) * rx) / den
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return t, u
    return None


# --------------------------------------------------------------------------
# validator  [paths.md §12]
# --------------------------------------------------------------------------

@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_errors(self) -> None:
        if self.errors:
            raise PathError(_join(self.errors))


def validate_areas(areas: Sequence[AreaData | None], *, strict: bool = False,
                   roadblocks: Sequence[tuple[int, int]] | None = None) -> ValidationReport:
    """Check every invariant the game relies on.

    errors   = things that can crash, hang or corrupt the game state
    warnings = deviations from vanilla conventions (errors when ``strict``)
               plus notes that are legal but worth knowing (never errors)."""
    rep = ValidationReport()
    E = rep.errors.append
    W = rep.errors.append if strict else rep.warnings.append   # convention
    info = rep.warnings.append                                  # note

    if len(areas) != NUM_AREAS:
        E(f"expected {NUM_AREAS} areas, got {len(areas)}")
    A: dict[int, AreaData] = {}
    for i, area in enumerate(areas):
        if area is None:
            E(f"area {i}: missing file (every nodesN.dat must exist; an empty one is 20 zero bytes)")
            continue
        if area.area_id != i:
            E(f"area slot {i} holds area_id {area.area_id}")
        A[i] = area

    def node(addr):
        a, n = addr
        ar = A.get(a)
        if ar is None or not 0 <= n < ar.num_nodes:
            return None
        return ar.nodes[n]

    def safe_links(ar: AreaData, idx: int) -> range:
        rec = ar.nodes[idx]
        base, cnt = int(rec["base_link"]), int(rec["flags"]) & F_LINKS_MASK
        return range(base, base + cnt) if 0 <= base and base + cnt <= ar.num_links else range(0)

    def neighbours(addr):
        ar = A[addr[0]]
        return [(int(ar.links[j]["area"]), int(ar.links[j]["node"])) for j in safe_links(ar, addr[1])]

    totals = Counter()
    navi_refs: dict[tuple[int, int], list[tuple]] = defaultdict(list)
    for a, ar in A.items():
        nd = ar.nodes
        totals["nodes"] += ar.num_nodes
        totals["vehicle_nodes"] += ar.num_vehicle_nodes
        totals["ped_nodes"] += ar.num_ped_nodes
        totals["navis"] += ar.num_navis
        totals["links"] += ar.num_links
        if ar.header is not None and ar.header[0] != ar.header[1] + ar.header[2]:
            E(f"area {a}: header node counts inconsistent {ar.header}")
        if not 0 <= ar.num_vehicle_nodes <= ar.num_nodes:
            E(f"area {a}: vehicle node count {ar.num_vehicle_nodes} > node count {ar.num_nodes}")
        if ar.num_navis > MAX_NAVI_PER_AREA:
            E(f"area {a}: {ar.num_navis} navi nodes > {MAX_NAVI_PER_AREA} (10-bit index)")
        if ar.num_links > MAX_LINKS_PER_AREA:
            E(f"area {a}: {ar.num_links} links > {MAX_LINKS_PER_AREA} (int16 base link id)")
        if ar.num_nodes > MAX_NODES_PER_AREA:
            E(f"area {a}: {ar.num_nodes} nodes > {MAX_NODES_PER_AREA}")
        if ar.num_nodes and not ar.num_links:
            E(f"area {a}: has nodes but no links")
        if ar.link_reserve is not None:
            if len(ar.link_reserve) and np.any(ar.link_reserve["area"] != 0xFFFF):
                E(f"area {a}: reserved dynamic-link slots must have area 0xFFFF (0x44E043 scans for it)")
            if len(ar.link_reserve) and np.any(ar.link_reserve["node"] != 0):
                W(f"area {a}: reserved dynamic-link slots differ from vanilla FFFF 0000 pattern")
            if np.any(ar.length_reserve != 0) or np.any(ar.intersection_reserve != 0):
                W(f"area {a}: reserved length/intersection slots are not zero")
        if ar.trailing and any(ar.trailing):
            W(f"area {a}: {len(ar.trailing)} non-zero trailing bytes")
        if len(nd):
            if np.any(nd["search"] != SEARCH_DIST_INIT):
                E(f"area {a}: {int(np.sum(nd['search'] != SEARCH_DIST_INIT))} nodes with search distance "
                  "!= 0x7FFE (DoPathSearch would unlink nodes that are not in its list)")
            if np.any(nd["area"] != a):
                E(f"area {a}: node area field differs from file area")
            if np.any(nd["node"] != np.arange(ar.num_nodes)):
                E(f"area {a}: node id field differs from node index")
            cols = np.clip(((nd["x"] / COORD_SCALE - WORLD_MIN) // AREA_SIZE).astype(int), 0, 7)
            rows = np.clip(((nd["y"] / COORD_SCALE - WORLD_MIN) // AREA_SIZE).astype(int), 0, 7)
            outside = int(np.sum(cols + AREAS_X * rows != a))
            if outside:
                W(f"area {a}: {outside} nodes lie outside the area (game region formula)")
            fl = nd["flags"].astype(np.int64)
            veh = np.arange(ar.num_nodes) < ar.num_vehicle_nodes
            if np.any(((fl >> 5) & 1) != ((fl >> 8) & 1)):
                W(f"area {a}: bit 8 (switched off original) differs from bit 5; the loader overwrites it")
            if np.any((fl & F_WATER).astype(bool) & ~veh):
                W(f"area {a}: ped nodes with the water flag")
            if np.any(((fl >> SPEED_SHIFT) & 3).astype(bool) & ~veh) or np.any(((fl >> BEHAVIOUR_SHIFT) & 15).astype(bool) & ~veh):
                W(f"area {a}: ped nodes with speed/behaviour bits")
            if np.any((fl >> 24) & 0xFF) or np.any(fl & ((1 << 9) | (1 << 11) | (3 << 14))):
                W(f"area {a}: unused flag bits set (9, 11, 14, 15 or 24-31)")
        for i in range(ar.num_nodes):
            rec = nd[i]
            cnt = int(rec["flags"]) & F_LINKS_MASK
            base = int(rec["base_link"])
            is_veh = i < ar.num_vehicle_nodes
            me = (a, i)
            if cnt == 0:
                (E if is_veh else W)(f"node {me}: no links" + (" (CCarCtrl::InitSequence divides by it)" if is_veh else ""))
                continue
            if cnt > MAX_LINKS_PER_NODE:
                W(f"node {me}: {cnt} links > {MAX_LINKS_PER_NODE} (dynamic links would overflow 12-slot blocks)")
            if base < 0 or base + cnt > ar.num_links:
                E(f"node {me}: link range [{base}, {base + cnt}) outside 0..{ar.num_links}")
                continue
            targets = []
            navis_here = []
            for j in range(base, base + cnt):
                t = (int(ar.links[j]["area"]), int(ar.links[j]["node"]))
                targets.append(t)
                if t == me:
                    E(f"node {me}: links to itself")
                    continue
                if t[0] >= NUM_AREAS or t[0] not in A:
                    E(f"node {me}: link to invalid area {t[0]}")
                    continue
                tr = A[t[0]]
                if not 0 <= t[1] < tr.num_nodes:
                    E(f"node {me}: link to node {t} beyond that area's {tr.num_nodes} nodes")
                    continue
                t_veh = t[1] < tr.num_vehicle_nodes
                if t_veh != is_veh:
                    E(f"node {me}: links a vehicle node and a ped node")
                    continue
                trec = tr.nodes[t[1]]
                if is_veh and ((int(rec["flags"]) ^ int(trec["flags"])) & F_WATER):
                    W(f"node {me}: links a water node and a land node {t}")
                if me not in neighbours(t):
                    E(f"node {me}: link to {t} is not reciprocated")
                d = math.sqrt(sum((int(rec[k]) - int(trec[k])) ** 2 for k in ("x", "y", "z"))) / COORD_SCALE
                L = int(ar.link_lengths[j])
                if L == 0:
                    W(f"node {me}: zero link length to {t}")
                elif abs(L - min(max(int(d), 1), MAX_LINK_LENGTH)) > 1:
                    W(f"node {me}: link length {L} to {t} but distance is {d:.2f}")
                inter = int(ar.intersections[j])
                if is_veh and inter:
                    W(f"node {me}: intersection flags on a vehicle link")
                if inter & ~3:
                    W(f"node {me}: unknown intersection bits {inter:#x}")
                if inter == I_PED_LIGHT:
                    W(f"node {me}: ped light without road-cross flag")
                if not is_veh:
                    if int(ar.navi_links[j]) != NAVI_LINK_PED:
                        W(f"node {me}: ped link with navi link {int(ar.navi_links[j]):#x} (vanilla 0)")
                    continue
                na, ni = _unpack_navi_link(ar.navi_links[j])
                navis_here.append((na, ni))
                if na not in A or ni >= A[na].num_navis:
                    E(f"node {me}: navi link ({na}, {ni}) out of range")
                    continue
                navi_refs[(na, ni)].append((me, t))
            if len(set(targets)) != len(targets):
                W(f"node {me}: duplicate link targets (vanilla has 4, all ped nodes)")
            if is_veh and len(navis_here) >= 2 and len(set(navis_here)) == 1:
                E(f"node {me}: all links share one navi (CCarCtrl::GenerateOneRandomCar loops forever)")
            elif is_veh and len(set(navis_here)) != len(navis_here):
                W(f"node {me}: two links share a navi")

    # navi checks
    lane_combos = Counter()
    for (na, ni), refs in navi_refs.items():
        rec = A[na].navis[ni]
        att = (int(rec["area"]), int(rec["node"]))
        pairs = {frozenset((x, y)) for x, y in refs}
        if len(refs) != 2 or len(pairs) != 1:
            W(f"navi ({na}, {ni}): referenced by {len(refs)} link entries / {len(pairs)} links (expected 1 link, both directions)")
        x, y = refs[0]
        if att not in (x, y):
            W(f"navi ({na}, {ni}): attached node {att} is not an endpoint of {x}-{y}")
            continue
        other = y if att == x else x
        if att > other:
            W(f"navi ({na}, {ni}): attached to the higher address {att} (game assumes the lower one)")
        ra, ro = node(att), node(other)
        dx, dy = int(ra["x"]) - int(ro["x"]), int(ra["y"]) - int(ro["y"])
        dl = math.hypot(dx, dy)
        ux, uy = int(rec["dir_x"]) / DIR_SCALE, int(rec["dir_y"]) / DIR_SCALE
        ul = math.hypot(ux, uy)
        if dl == 0 or ul == 0:
            E(f"navi ({na}, {ni}): zero direction or zero-length segment")
            continue
        if not 0.95 <= ul <= 1.02:
            W(f"navi ({na}, {ni}): direction length {ul:.3f}")
        if (ux * dx + uy * dy) / (ul * dl) < 0.9:
            W(f"navi ({na}, {ni}): direction does not point towards the attached node")
        px, py = int(rec["x"]) - int(ro["x"]), int(rec["y"]) - int(ro["y"])
        lat = abs(dx * py - dy * px) / dl / COORD_SCALE
        along = (dx * px + dy * py) / (dl * dl)
        if lat > (1.0 if strict else 20.0) or not -0.05 <= along <= 1.05:
            W(f"navi ({na}, {ni}): position {lat:.1f} units off its segment (t={along:.2f})")
        cols = min(max(int((int(rec["x"]) / COORD_SCALE - WORLD_MIN) // AREA_SIZE), 0), 7)
        rows = min(max(int((int(rec["y"]) / COORD_SCALE - WORLD_MIN) // AREA_SIZE), 0), 7)
        if cols + AREAS_X * rows != na and na not in (att[0], other[0]):
            W(f"navi ({na}, {ni}): stored in area {na}, neither its position's nor an endpoint's area")
        lanes = int(rec["lanes"])
        lt, lf = lanes & 7, (lanes >> 3) & 7
        lane_combos[(lt, lf)] += 1
        if lt == 0 and lf == 0:
            info(f"navi ({na}, {ni}): no lanes in either direction (vanilla has 12 such links)")
        if lanes & 0x80:
            W(f"navi ({na}, {ni}): lane byte bit 7 set")
        if int(rec["width"]) > MAX_NAVI_WIDTH_RAW:
            W(f"navi ({na}, {ni}): width byte {int(rec['width'])} >= 128 (sign ambiguity)")
        light = int(rec["light"])
        if light & ~0x7:
            W(f"navi ({na}, {ni}): unknown light bits {light:#x}")
        if light & L_BRIDGE:
            W(f"navi ({na}, {ni}): bridge-light bit set in file (runtime flag)")
        if (light & 3) == 3:
            W(f"navi ({na}, {ni}): reserved light group 3")
        if (light & 3) == 0 and lanes & N_LIGHT_DIR:
            W(f"navi ({na}, {ni}): light direction bit without light group")
    for a, ar in A.items():
        for ni in range(ar.num_navis):
            if (a, ni) not in navi_refs:
                W(f"navi ({a}, {ni}): not referenced by any link")

    # flood fill / components
    comp_values: dict[str, Counter] = {"veh": Counter(), "ped": Counter()}
    seen = set()
    for a, ar in A.items():
        for i in range(ar.num_nodes):
            if (a, i) in seen:
                continue
            kind = "veh" if i < ar.num_vehicle_nodes else "ped"
            stack, members = [(a, i)], []
            seen.add((a, i))
            while stack:
                cur = stack.pop()
                members.append(cur)
                cr = A[cur[0]]
                for j in safe_links(cr, cur[1]):
                    t = (int(cr.links[j]["area"]), int(cr.links[j]["node"]))
                    if t not in seen and t[0] in A and 0 <= t[1] < A[t[0]].num_nodes:
                        seen.add(t)
                        stack.append(t)
            vals = {int(A[m[0]].nodes[m[1]]["flood"]) for m in members}
            if len(vals) > 1:
                E(f"component containing {members[0]} has flood fill values {sorted(vals)[:8]} "
                  "(DoPathSearch fails between connected nodes)")
            for v in vals:
                comp_values[kind][v] += 1
    for kind, cnt in comp_values.items():
        shared = [v for v, c in cnt.items() if c > 1]
        if shared:
            info(f"{kind} flood fill values shared by several components: {sorted(shared)[:10]}"
                 + (" ..." if len(shared) > 10 else ""))
        rep.stats[f"{kind}_components"] = sum(cnt.values())

    # dead-end walks
    limit = totals["vehicle_nodes"] + 2
    def is_normal(addr):
        b = (int(A[addr[0]].nodes[addr[1]]["flags"]) >> BEHAVIOUR_SHIFT) & 15
        return b == 0 or b > 10
    for a, ar in A.items():
        for i in range(ar.num_vehicle_nodes):
            if int(ar.nodes[i]["flags"]) & F_DEAD_END:
                for p in neighbours((a, i)):
                    if p[0] in A and 0 <= p[1] < A[p[0]].num_nodes and \
                            _dead_end_walk(lambda x: [t for t in neighbours(x) if t[0] in A], is_normal,
                                           (a, i), p, limit) is None:
                        E(f"node {(a, i)}: dead-end flag on a node whose dead-end walk never ends (game hangs)")
                        break

    # police roadblocks
    if roadblocks is not None:
        if len(roadblocks) > MAX_ROADBLOCKS:
            E(f"{len(roadblocks)} roadblock nodes > {MAX_ROADBLOCKS}")
        for addr in roadblocks:
            r = node(addr)
            if r is None:
                E(f"roadblock node {addr} does not exist")
            elif addr[1] >= A[addr[0]].num_vehicle_nodes or int(r["flags"]) & F_WATER:
                E(f"roadblock node {addr} is not a land vehicle node")
            elif (int(r["flags"]) & F_LINKS_MASK) < 2:
                E(f"roadblock node {addr} has < 2 links (GetRoadBlockNodeInfo reads two)")

    rep.stats.update(totals)
    rep.stats["lane_combos"] = dict(lane_combos)
    return rep


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def _main(argv: Sequence[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="GTA SA nodes*.dat tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="validate a directory with nodes0..63.dat")
    v.add_argument("directory")
    v.add_argument("--strict", action="store_true", help="treat convention deviations as errors")
    v.add_argument("--roadblox", help="also check a roadblox.dat")
    s = sub.add_parser("stats", help="print per-area counts")
    s.add_argument("directory")
    args = ap.parse_args(argv)
    areas = read_area_files(args.directory, allow_missing=True)
    if args.cmd == "stats":
        for ar in areas:
            if ar is not None:
                print(f"area {ar.area_id:2d}: nodes {ar.num_nodes:5d} (veh {ar.num_vehicle_nodes:5d}, "
                      f"ped {ar.num_ped_nodes:5d}) navis {ar.num_navis:4d} links {ar.num_links:5d}")
        return 0
    rb = read_roadblox(Path(args.roadblox).read_bytes()) if args.roadblox else None
    rep = validate_areas(areas, strict=args.strict, roadblocks=rb)
    for w in rep.warnings[:200]:
        print("warning:", w)
    for e in rep.errors[:200]:
        print("ERROR:", e)
    print({k: v for k, v in rep.stats.items() if k != "lane_combos"})
    print(f"{len(rep.errors)} errors, {len(rep.warnings)} warnings")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    raise SystemExit(_main())
