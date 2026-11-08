# GTA SA path files (`nodes0.dat` … `nodes63.dat`): verified format and engine semantics

Scope: PC 1.0 (classic). Written for `tools/gta/paths.py`. Every claim cites the
game function it comes from. Nobody can test in-game, so where sources disagree
this note says which one wins and why. Section 15 lists what is still uncertain.

## 0. Sources and how they were checked

| tag | source | trust |
|---|---|---|
| **GR** | gta-reversed `source/game_sa/…` (master, fetched 2026-11-08): `PathFind.cpp/.h`, `NodeAddress.h`, `TrafficLights.cpp`, `RoadBlocks.cpp`, `Curves.cpp`, `Streaming.cpp`, `CarGenerator.cpp`, `Tasks/TaskTypes/TaskComplexWander.cpp`, `AutoPilot.h`, `General.cpp`, `Enums/eCarMission.h` | primary |
| **ReSA** | `pedrokilo/ReSA` (gta-reversed fork, CPathFind and CCarCtrl fully reconstructed; comments state "verified against disassembly" with instruction addresses) | secondary, used where GR still calls the exe |
| **SAX** | `boludoz/sa-extended` (gta-reversed fork, CCarCtrl reconstructed with R*-style names such as `LanesTo/LanesFro`, "ASM match nn %") | secondary |
| **WG** | `Sir-Teo/web-gta` research notes (some functions read from the iOS binary) | corroboration only |
| **VAN** | the 64 vanilla files from `ziggi/FCNPC` (`sampsvr_files/scriptfiles/FCNPC/nodes/`). All 64 match the XXH3 sums of the 1.0 `data/Paths/NODES*.DAT` in `Shivelight/gta-integrity-checker`; `NODES15.DAT` zero-padded to 94,208 bytes has the SHA-256 that web-gta published for `nodes15.dat` taken from `gta3.img`. So these are the retail files. They were downloaded only to the scratchpad. | ground truth for layout and conventions |
| **WIKI** | gtamods "Paths (GTA SA)" and "Fastman92 Path Format" | wrong in several places (see §3.4) |

GR wins over the wiki. Where GR is wrong or not reversed yet, the claim comes from
ReSA/SAX and is checked against the VAN statistics quoted in §1–§11. `paths.py` round-trips all
64 vanilla files byte for byte, and `validate_areas` reports 0 errors on them. The 27 warnings are
known vanilla quirks: border nodes, 12 navis with no lanes, 2 shared navis, 4 duplicate ped links.

## 1. Area grid

* 64 areas of 750 × 750 units, starting at the south-west corner (−3000, −3000).
  `area = 8·row + col` with `col = clamp(trunc((x+3000)/750), 0, 7)` and `row` the same from y.
  Sources: GR `CPathFind::FindXRegionForCoors`/`FindYRegionForCoors` (`detail::RegionValueOf`) and
  `GetAreaIdFromXY` (`y * 8 + x`). So the index is row-major from the SW corner; this matches the wiki.
* A coordinate exactly on a border belongs to the higher column or row (truncation). Coordinates
  below −3000 give a negative float that is cast to uint32, which is undefined behaviour. Keep everything
  inside [−3000, 3000].
* VAN: 17 nodes and 4 navis are stored in an area other than the one the game formula gives. All of them
  are within 1/8 unit of a border. The 4 navis and 11 of the nodes are exactly on the border but stored in
  the lower area; 6 nodes are 1/8 unit below a border but stored in the higher area. So R* assigned areas
  before quantizing. It is harmless: `FindNodeClosestToCoors` (ReSA 0x44F460) also searches the neighbouring
  rings. `paths.py` uses the game formula on the quantized position, so every node is inside its own area.
* Areas 64–71 are runtime interior areas. Files only exist for 0–63.

## 2. Streaming, loading and packaging

* **Where the game reads them.** Each `nodesN.dat` is a streamed resource in an IMG archive with
  model id `RESOURCE_ID_DAT + N` (GR `Streaming.h`). `CStreaming::LoadCdDirectory` handles every
  `*.DAT` entry by `sscanf(name + 5, "%d")`, so every `.dat` in any IMG is taken for `nodesXX.dat`.
  It registers an id only if it has no CD position yet (`if (!info.HasCdPosnAndSize())`), so the
  **first** archive wins. `CStreaming::InitImageList` adds `MODELS\GTA3.IMG` first, then `GTA_INT.IMG`,
  then come the `IMG` lines of gta.dat (GR `CFileLoader::LoadLevel`). Consequences:
  * our 64 files must **replace the entries inside `models/gta3.img`**. A later IMG cannot override them.
  * do not put any other `*.dat` file in an IMG.
  * the loose `data/paths/nodes*.dat` are ignored. `LoadPathFindData(int)` at 0x452F40 would open them,
    but nothing calls it; the only caller of the stream version is `CStreaming::ConvertBufferToObject`.
    SA-MP's FCNPC and open.mp do read the loose copies.
  * order check: `CGame::Initialise` runs `Init1` (`CStreaming::InitImageList`: GTA3.IMG, GTA_INT.IMG),
    then `CFileLoader::LoadLevel` (gta.dat `IMG` lines), then `Init2` → `CStreaming::Init2` → `LoadCdDirectory`.
* **All 64 entries must exist** as `nodes<N>.dat` (the name is case-insensitive; the number is parsed from
  character 5 on). `UpdateStreaming` requests every area near the player. An area with no entry would be
  requested with no CD position or size (GR `CStreaming::RequestModel`/`GetNextFileOnCd`). What happens
  next was not traced, so treat a missing entry as undefined or fatal. An empty area is valid as a
  **20-byte header of zeros**. `LoadPathFindData` then allocates a 1-element dummy node array, sets the
  navi and link pointers to null and all counts to 0. `GeneratePedCreationCoors` guards
  `m_anNumPedNodes == 0` (ReSA 0x44E790). Every other loop runs over counts.
* **Loading.** `CStreaming::ConvertBufferToObject` calls `ThePaths.LoadPathFindData(stream, area)` (GR).
  `CPathFind::UpdateStreaming` (GR 0x450A60) runs about every 512 ms. It keeps loaded every area touching
  the rectangle player ± 350 (`MarkRegionsForCoors(FindPlayerCoors(), 350)`), plus ± 300 around
  `SetPathsNeededAtPosition` and around mission land vehicles, plus the rectangle requested by script
  (`MakeRequestForNodesToBeLoaded`, loaded as mission-required). It unloads every other area
  (`RemoveModel` → `UnLoadPathFindData`). In practice that is 1–4 areas. `LoadSceneForPathNodes` (0x44DE00)
  requests player ± 350 during `CStreaming::LoadScene`.
* **Partial loading is normal.** Code checks `m_pPathNodes[area] != nullptr` before using a linked node
  or a navi (`IsAreaLoaded`, used everywhere in DoPathSearch, PickNextNodeRandomly and FindNextNodeWandering).
  `CCarCtrl::StopCarIfNodesAreInvalid` (SAX 0x422590) parks a car while its current or next node or navi
  area is unloaded. Cross-area references are therefore fine, as long as they are valid once both areas
  are loaded.

## 3. Binary layout (as read by `CPathFind::LoadPathFindData(RwStream*, int)`, GR 0x4529F0)

All little-endian, packed, no padding. `N` = number of links ("addresses").

| offset | size | content |
|---|---|---|
| 0 | 20 | header: uint32 `numNodes`, `numVehicleNodes`, `numPedNodes`, `numNavi`, `numLinks` |
| 20 | 28·numNodes | `CPathNode[]`, vehicle nodes first, then ped nodes |
| … | 14·numNavi | `CCarPathLink[]` (navi nodes) |
| only if N > 0: | | |
| … | 4·(N+192) | `CNodeAddress[]` links, then 192 reserved entries (the wiki's "filler" section 4) |
| … | 2·N | `CCarPathLinkAddress[]` navi links (no reserve) |
| … | N+192 | uint8 link lengths, then 192 reserved bytes |
| … | N+192 | `CPathIntersectionInfo[]` (uint8), then 192 reserved bytes |

* 192 = `NUM_DYNAMIC_LINKS_PER_AREA (16) × 12`. The loader reads `numAddresses + 192` entries for
  links, lengths and intersections and `numAddresses` entries for navi links. Both GR and ReSA agree.
* File size = `20 + 28·numNodes + 14·numNavi + (N ? 8·N + 1152 : 0)`. **All 64 vanilla files match
  this exactly.** The wiki layout (lengths N, intersections N, then 192 bytes) is 192 bytes too short.
* `numNodes` must equal `numVehicleNodes + numPedNodes`. It is only asserted in debug builds, but
  ped spawning indexes `numVehicleNodes + rand % numPedNodes`.
* When N = 0 the loader reads nothing after the navis.
* **Vanilla reserved content: links = 192 × (area 0xFFFF, node 0x0000); lengths and intersections = 192 × 0.**
  This content matters. `AddDynamicLinkBetween2Nodes_For1Node` (ReSA 0x44E000, verified at 0x44E043
  `cmp word [esi],0FFFFh`) scans the 12-entry blocks after `numAddresses` for the first block whose
  entry has `area == 0xFFFF`, with no bounds check. Interior ped paths use it to link to the nearest
  exterior ped node (`CompleteNewInterior`). Zeros, as the wiki and Fastman92 pages allow, would make that
  scan run off the array. GR's version of the loop has the test inverted (a reconstruction bug).

### 3.1 CPathNode (28 bytes; GR `PathFind.h`)

| off | type | field | value to write |
|---|---|---|---|
| 0 | uint32 | `m_next` (runtime list pointer) | 0. Vanilla has garbage; the game writes it before reading it (`AddNodeToList`). |
| 4 | uint32 | `m_prev` | 0 |
| 8 | int16[3] | position × 8 | round(world·8) |
| 14 | int16 | `m_totalDistFromOrigin` | **must be 0x7FFE (32766)**. In `DoPathSearch` (GR/ReSA 0x4515D0) any other value makes it call `RemoveNodeFromList` on a node that is not in the list, which dereferences the garbage `m_prev`. Every vanilla node has 0x7FFE. |
| 16 | int16 | `m_wBaseLinkId` (first link index) | contiguous per node in node order (as vanilla); **signed**, so N + 192 ≤ 32767 |
| 18 | uint16 | `m_wAreaId` | file's area |
| 20 | uint16 | `m_wNodeId` | index in the file |
| 22 | uint8 | `m_nPathWidth`, world × 16 | see §4 |
| 23 | uint8 | `m_nFloodFill` | see §9 |
| 24 | uint32 | flags | see §4 |

### 3.2 CCarPathLink / navi node (14 bytes; GR `PathFind.h`)

| off | type | field |
|---|---|---|
| 0 | int16[2] | position × 8 |
| 4 | uint16, uint16 | attached node (`CNodeAddress` area, node). R* name `Node1` (SAX) |
| 8 | int8[2] | direction × 100, unit vector |
| 10 | uint8 | width × 16 (median half-width; see §6). The struct declares int8; `OneWayLaneOffset` reads it as uint8. Keep it ≤ 127. |
| 11 | uint8 | bits 0–2 **lanes towards the attached node** (GR `m_numOppositeDirLanes`, SAX `LanesTo`, wiki "left lanes", flags bits 8–10); bits 3–5 **lanes away from it** (GR `m_numSameDirLanes`, SAX `LanesFro`, wiki "right lanes", bits 11–13); bit 6 traffic-light direction (wiki bit 14); bit 7 unused |
| 12 | uint16 | bits 0–1 traffic-light group (`eTrafficLightsDirection`: 0 none, 1 north-south, 2 west-east; wiki bits 16–17); bit 2 `m_bridgeLights`, set at runtime by `SetLinksBridgeLights` (0x44D960) and never set in VAN; rest 0 |

GR's comments call bits 0–2 "opposite" and bits 3–5 "same direction". The names do not matter, but in
practice bits 0–2 serve traffic moving *along* the direction vector (§5.2).

### 3.3 Links and per-link arrays

* `CNodeAddress` = uint16 area, uint16 node (GR `NodeAddress.h`); 0xFFFF means invalid.
* `CCarPathLinkAddress` = uint16, `navi index (bits 0–9) | area << 10 (bits 10–15)`. This limits each area
  to **1024 navi nodes**. 0xFFFF is "invalid" and decodes to (63, 1023). For ped links VAN stores 0.
* Link length: uint8, see §11. Intersection byte: bit 0 `m_bRoadCross`, bit 1 `m_bPedTrafficLight` (§8).
  Always 0 on car links.

### 3.4 Wiki errata (all confirmed by GR/ReSA code and VAN)

1. Lengths and intersections each have a 192-entry reserve. There is no separate "192 unknown bytes" after section 7.
2. The filler must keep area = 0xFFFF; zeros are not equivalent.
3. Node flag bits 4/5 are not a "traffic level". They are dead-end (4) and switched-off (5). Bit 8,
   "emergency vehicles only" in the wiki, is `m_isSwitchedOffOriginal`: the loader overwrites it with bit 5,
   and bit 8 equals bit 5 on every vanilla node. Bits 12/13 are a 2-bit speed class (§4).
4. Node path width is /16, not /8 (see §4 for the one place that uses /8).
5. "Right (forward) lanes" is backwards. The lanes in bits 0–2 of byte 11 drive along the navi direction (§5.2).

## 4. Node flags and fields: semantics and recommended values

Flag word at offset 24 (GR `CPathNode` bitfields; usage cites):

| bits | name | used by | meaning |
|---|---|---|---|
| 0–3 | `m_nNumLinks` | everywhere | link count. **≥ 1**: `CCarCtrl::InitSequence` (0x421740) does `rand % numLinks`, so 0 links divides by zero. **≤ 11**: a dynamic link copies a node's links into a 12-slot block. VAN maximum is 4 (vehicle) / 6 (ped). |
| 4 | `m_onDeadEnd` | `GenerateCarCreationCoors2` (SAX): if the destination node has it, `ThisNodeWillLeadIntoADeadEnd` runs and the spawn is rejected when it returns true | marks dead-end branches. **That walk (0x44D310) has no cycle detection, so the bit must never be set where the walk can loop**, for example on a pure ring road (§10). |
| 5 | `m_isSwitchedOff` | car spawning skips switched-off segments (`GenerateCarCreationCoors2`, `bIgnoreSwitchedOff` for normal traffic); `FindNodeClosestInRegion(bLowTraffic)` (ReSA 0x44F2C0); PickNextNodeRandomly's 2nd sweep avoids entering them; ped wandering never enters them from a normal node (ReSA 0x451B70); ped spawning skips them unless the caller passes `bLowTraffic` (ReSA 0x44E790); script `SWITCH_ROADS_ON/OFF` | "no ambient traffic". Cars and emergency vehicles can still route over them (DoPathSearch ignores the bit). This is the wiki's "emergency only". |
| 6 | `m_bRoadBlocks` | `IsThisAnAppropriateNode` (SAX): a wandering car never moves from a node without the bit to a node with it | road closed to wandering traffic. VAN has 391 such car nodes. It is **not** the police-roadblock list (that is §13). |
| 7 | `m_bWaterNode` | boats. `FindNodeClosestInRegion` requires `water == bBoats`. IsThisAnAppropriateNode / DoPathSearch forbid water↔land moves (except the Vortex). Boat spawn needs `CWaterLevel::GetWaterLevel` at the spot. | |
| 8 | `m_isSwitchedOffOriginal` | the loader sets it to bit 5 (GR/ReSA `LoadPathFindData`) | write the same as bit 5 |
| 9 | — | skipped by `FindNodeClosestInRegion(bUnkn)` | 0 (never set in VAN) |
| 10 | `m_bDontWander` | mission-created cruising cars do not enter (IsThisAnAppropriateNode); peds do not wander into it from a normal node (0x451B70); script `MarkRoadNodeAsDontWander` | VAN: 17 ped nodes |
| 11 | — | | 0 |
| 12–13 | speed class (SAX `Speed`; GR `m_bNotHighway`/`m_bHighway`) | `FindSpeedMultiplierWithSpeedFromNodes` (SAX): 0 → ×0.8, 1 → ×1.0, 2 → ×1.2, 3 → ×1.0. `m_bMoreCarsAndFewerPeds = Speed ≥ 2` when the nearest road has ≥ 2 lanes in a direction (GenerateCarCreationCoors2). | car: 1 normal, 2 highway, 0 slow. Ped: 0. |
| 14–15 | — | | 0 |
| 16–19 | `m_nSpawnProbability` (density) | cars: `if ((rand & 15) > min(from, to)) skip` → probability (d+1)/16 (`GenerateOneRandomCar`, SAX). Peds: spawn only if `d > rand(0..14)` → d/15 (0x44E790). | 15 for normal roads and walks |
| 20–23 | `m_nBehaviourType` (SAX `SpecialFunction`) | `IsThisAnAppropriateNode` and `PickNextNodeRandomly` (SAX, ReSA 0x42DE80) | 0 normal. 1 parallel parking (car switches to `MISSION_PARK_PARALLEL`, 46). 2 perpendicular parking (45). 5 / 10 only vehicles with bbox max z ≥ 2. 8 / 9 only vehicles with bbox ≤ (2, 4, 1.5). 9 = 4.5 s stop at the node, 10 = 10 s stop. `HasToBeSwitchedOff` exempts 1 and 2 from script switches. The dead-end walk treats behaviour 1–10 as absent. |
| 24–31 | — | | 0 |

Other node fields:

* **Path width** (byte 22, units × 16). On car nodes it is the median half-width, the same meaning as
  the navi width. It is used by `FindNodeCoorsForScript` (`pos + perpLeft·(w/16 + 2.7)`, 0x4505E0/0x450780) and
  by `CRoadBlocks::GenerateRoadBlocks` (a nonzero width splits the police roadblock around the median).
  VAN car nodes: 98 % have 0.
  On ped nodes, wandering peds aim at `node + (w/16)·[−0.87 … +0.99]` in x and y. The offset is fixed per
  ped seed (`TakeWidthIntoAccountForWandering` 0x4509A0, called by `CTaskComplexWander::ComputeTargetPos`
  0x669F60, which also adds 1 to z). The "am I still at my node" radius is `max(7, w/8)` (0x451B70), the
  only /8 use. VAN ped widths: 1.0 (80 %), 0.5, 0.25.
* **z.** Car nodes must sit on the road or water surface. Spawn snaps to the nearest surface
  above or below the height interpolated between the nodes and rejects > 7 units (GenerateOneRandomCar, SAX).
  Far-away cars use `FindGhostRoadHeight` (0x422370), which interpolates node z linearly. On hills or
  ramps, add nodes so that the straight line between nodes follows the road. Ped nodes go on the walking
  surface. Spawn rejects ground > 3 units off the interpolated height (0x44E790), and wander targets are z + 1.

Recommended values (`paths.py` defaults are in **bold**):

| road type | links / lanes | node flags |
|---|---|---|
| city street, two-way | **1+1**, median 0 (VAN uses only 1+1 for two-way land navis) | **spawn 15, speed 1** |
| one-way street | 1–3 lanes in one direction | same |
| highway / arterial | separate one-way carriageways with 2–3 lanes each (VAN: 6,355 navis with 0/2), or N+N with a median (supported by the code but not used in VAN, §15) | speed 2 |
| service road, parking aisle, track, alley | 1+1 or one-way | switched_off (no ambient cars), speed 0 |
| closed road (e.g. construction) | | roadblock bit (wanderers never enter) |
| boat lane | 1+1 or one-way, width ≈ 2.5 (VAN 40/16) | boat |
| sidewalk / footpath | — | **ped width 1.0, spawn 15** |
| private or blocked footpath | — | ped switched_off |
| crossing | ped link with crossing = 1, + ped_light at signals | |

Not-for-parking = behaviour 0 (default). Parked cars come from IPL car generators. Those query
`FindNodeClosestToCoors` at runtime (GR `CarGenerator.cpp`) and store no node addresses.

## 5. Navi nodes: one per car segment

### 5.1 Attachment, position, direction, area (VAN, consistent with all code paths)

* Every undirected car link has exactly one navi, and **both** directed link entries point at it.
  VAN: 31,464 navis are referenced exactly twice; 2 are shared by two duplicated links.
* **Attached node = the endpoint with the lower `(area, node)` address, compared lexicographically, area first.**
  VAN: 62,932 of 62,936 entries; the other 4 are the shared-navi oddities. The game depends on this:
  * PickNextNodeRandomly (ReSA 0x42DE80, SAX) chooses the entry lane count with `attachedTo(link, next)`.
  * It chooses the next lane count and travel sign by address comparison: `current < chosen` gives sign −1 and bits 3–5.
  * GenerateOneRandomCar does the same (`FromNode < ToNode` → −1).
  Both only agree if the navi is attached to the lower node. ReSA's comparison is explicitly
  `area < area || (area == area && node < node)`. SAX uses `operator<=>` = default, which is lexicographic (area, node).
* **Direction = unit vector from the other endpoint towards the attached one**, ×100, rounded.
  VAN: all 62,932 valid entries (cos > 0.9). VAN vectors are slightly short (median length 0.993,
  range 0.986–1.0), consistent with truncation; `paths.py` rounds. The game never re-normalises; it uses
  the vector as a rotation for lane offsets and in the light-distance dot product.
* **Position:** the segment midpoint in VAN (median t = 0.499; 71 % within ±2 % of the midpoint; some are on
  an interpolated curve up to 15 units off). `paths.py` uses the exact midpoint of the quantized endpoints.
* **Area: the area containing the navi position** (VAN 62,928/62,936; the exceptions are on borders). So a
  segment crossing a border usually has its navi in the area of the *non-attached* end. A diagonal segment
  that cuts an area corner can put it in a third area that holds neither endpoint (VAN has 4, e.g. link
  (16,0)–(25,854) with its navi in area 17). Any area is legal: navis are addressed by their own 6-bit area, and every use
  checks `m_pPathNodes[navi.area]` first (PickNextNodeRandomly, StopCarIfNodesAreInvalid,
  ShouldCarStopForLight). `paths.py` can therefore spill navis to an endpoint's area when an area exceeds
  1024 navis, and warns when it does.
* **Width** = the attached node's path width in VAN (99.3 %). `paths.py` writes `CarLink.median` if given, else the attached node's width.

### 5.2 Which lanes serve which direction

For a car travelling from node P to node Q over a navi attached to `att`:

* travelling **towards** `att` (P > Q): sign +1, direction = +dir, lane count = byte 11 bits 0–2;
* travelling **away** from `att` (P < Q): sign −1, direction = −dir, lane count = bits 3–5.

Sources: PickNextNodeRandomly (ReSA: `attachedTo(link,next) ? flags & 7 : (flags>>3) & 7`, and
`below ? -1 : 1`, `below ? (flags>>3)&7 : flags&7`), SAX (`Node1 == CurrNode ? LanesTo : LanesFro`,
`OldNode < NewNode → InvertDir −1, LanesFro`), and GenerateOneRandomCar (SAX).

Independent check on VAN dual carriageways: for one-way navis, take this mapping and look for the nearest
antiparallel one-way navi 4–40 units sideways. It lies **left** of the travel direction in 6,024 cases and
right in 683, which is right-hand traffic. The opposite mapping would put SA on left-hand traffic.

`paths.py` API: `CarLink(a, b, lanes_ab, lanes_ba)`, where `lanes_ab` is the number of lanes for traffic
driving a→b. The compiler maps it to bits 0–2 when b is the attached node, otherwise to bits 3–5.

Caveat: `DoPathSearch` with `sameLaneOnly` (GR and ReSA identical) tests the lanes the opposite way round.
Only callers that pass `carCtrlFlags.bCantGoAgainstTraffic` use it (ReSA CarCtrl call sites; a script flag).
Ambient traffic uses PickNextNodeRandomly, and so do we. A mission car with that flag may refuse a
correct one-way street; R* data has the same behaviour.

## 6. Lane geometry: how wide the road meshes must be

Lateral position of a car (SteerAICarWithPhysicsFollowPath, ReSA 0x434900; PickNextNodeRandomly and
GenerateOneRandomCar, ReSA/SAX; the on-rails code, ReSA):

```
t      = navi.dir * sign                       (travel direction)
offset = (OneWayLaneOffset(navi) + lane) * 5.4 (+1.458 for VEHICLE_TYPE_BMX)
point  = navi.pos + offset * (t.y, -t.x)       (to the RIGHT of travel)
```

`LANE_WIDTH = 5.4` is the float at 0x858C50 (ReSA cites it in every use).
`CCarPathLink::OneWayLaneOffset` (0x44DB00, ReSA byte-level reconstruction; WG found the same in the iOS binary):

```
lanesTo   = byte11 & 7; lanesFro = (byte11 >> 3) & 7
if lanesTo == 0:  return 0.5 - lanesFro * 0.5
if lanesFro == 0: return 0.5 - lanesTo  * 0.5
return width_byte * 0.011574074 + 0.5          # = width/16/5.4 + 0.5
```

GR's header version (`if opposite: 0.5 - same/2; elif same: 0.5 - width/5.4/2; …`) is a reconstruction
error. Two independent disassemblies agree on the version above.

Result:

* **Two-way link** (both counts > 0): lane k of each direction is centred `w + 2.7 + 5.4·k` right of the
  navi line, where w = navi width (median half-width). Each carriageway runs from w to w + 5.4·N on its side.
* **One-way link** (N lanes): the lanes are centred on the line, `(k + 0.5 − N/2)·5.4`. The width is ignored.
* Lane 0 is the inner ("fast") lane. `bStayInFastLane` forces 0, `bStayInSlowLane` the outermost lane.
  Turn permissions: lane 0 may turn left, the last lane may turn right, any lane may go straight when there
  are < 3 lanes. Straight means a turn under 66° (cos > 0.4). Turns over 113.6° are skipped by far-away
  (on-rails) cars (`FindPathDirection` 0x422090, SAX; PickNextNodeRandomly). Cars change lane randomly
  every 4–7 segments longer than 16 units.
* **Corners:** `CCurves::CalcCurvePoint` (GR 0x43C900) drives straight along the lane line of the current
  segment. It bends within at most 5 units before and after the intersection with the next segment's lane
  line (a linear blend over ≤ 10 units), then follows the next lane line. If the lines do not meet ahead it
  uses an S-curve blend. So the car path is the polyline of lane lines with corners cut by ≤ 5 units.
  Segment-length asymmetry does not matter, but curved roads need nodes dense enough that chords stay on the asphalt.

Recommended curb-to-curb widths (`recommended_road_width`, 0.5-unit shoulders):

| lanes (a→b + b→a) | lane centres from centre line | lanes span | recommended |
|---|---|---|---|
| 1 one-way | 0 | 5.4 | 6.4 |
| 2 one-way | ±2.7 | 10.8 | 11.8 |
| 3 one-way | 0, ±5.4 | 16.2 | 17.2 |
| 1+1 | ±2.7 | 10.8 | **11.8** |
| 2+2 | ±2.7, ±8.1 | 21.6 | 22.6 |
| 3+3 | ±2.7, ±8.1, ±13.5 | 32.4 | 33.4 |
| N+N with median w | ±(w + 2.7 + 5.4k) | 10.8·N + 2w | + 1.0 |

A car about 2 units wide keeps about 1.7 units to its lane edge. Narrower meshes still work when the 2.7
offset stays on the asphalt, but cars will run along the curb. Bikes ride 1.458 further right.
The 0.67 map scale makes real roads narrower than this, so meshes must be widened to these game lanes.
Widths are quantized to 1/16 unit.

## 7. Traffic lights

* Phases are global (GR `CTrafficLights`). The cycle is `(timeMs/2) & 16383` (32.77 s).
  `LightForCars1` (group 1): green 0–10 s, yellow 10–12 s, red otherwise.
  `LightForCars2` (group 2): green 12–22 s, yellow 22–24 s.
  `LightForPeds`: green 24.0–30.77 s, flashing until 32.77 s, and only then are both car groups red.
  Riots and the green-lights cheat force car green.
* `ShouldCarStopForLight` (GR 0x49D610) checks the car's current, next and previous navi (`AutoPilot`).
  A navi with group ≠ 0 applies when `(navi.attached == route node) == dirbit`, where the route node is the
  node the car drives **towards** on that segment. So **dirbit = 1: the light controls traffic towards the
  attached node; 0: away from it.** The car stops while it is in the window `−12 < (pos − navi.pos)·t < 0`
  for current/next, and −6 for previous (physics cars only). That means the stop line is up to 12 units
  *before the navi point*. The slots are tested in the order current, next, previous. If an applicable
  slot's group is **green**, the function returns "do not stop" at once, without the distance test and
  without looking at the later slots. Yellow and red both stop inside the window.
* VAN placement (1,689 light navis: 823 group 1, 866 group 2; dirbit set on 1,213):
  * the controlled direction leads into a 3- or 4-link junction in 1,493 cases;
  * group 1 is used on north-south travel (784/823), group 2 on east-west (835/866);
  * light segments are short, median 16.75 units, navi about 8 units before the junction centre.
* `paths.py`: `CarLink.light_toward = junction node` and optional `light_group`, defaulting to N-S travel → 1, else 2.
  `PathNetwork.signalize_junction(node)` splits each approach at `2·(widest half-width + 3)` (16.8 for 1+1 roads,
  same as VAN) and puts the light on the short piece. Perpendicular approaches must get different groups,
  which the direction default ensures.
* Visual light objects choose their own phase from their heading
  (`FindTrafficLightTypeFromOrientation` 0x49D4D0: heading of the object's forward axis in 60–150° or
  240–330° → group 1, else group 2; heading = `GetATanOfXY(fwd.x, fwd.y)`, 0° = +x, 90° = +y).
  Orient them so they match the navi groups. Placing objects is outside the path files.
* Bridge lights (byte 12 bit 2) are runtime only. Write 0.

## 8. Ped paths, crossings, relation to the car graph

* Ped and car graphs are independent. VAN has no link between a vehicle node and a ped node, nor between
  land and water. The validator flags such links. Ped links have navi link 0 and no navis.
* Crossing flags are per directed link and symmetric in VAN. `CTaskComplexWander::CreateNextSubTask`
  (GR 0x674140, with `m_bWanderSensibly`) tests ped light first (`TASK_COMPLEX_OBSERVE_TRAFFIC_LIGHTS_AND_ACHIEVE_HEADING`,
  waits for `LightForPeds`), then road cross (`TASK_COMPLEX_CROSS_ROAD_LOOK_AND_ACHIEVE_HEADING`, looks for cars).
  VAN: only ped links carry flags, 466 entries = 1 and 1,346 entries = 3, never 2 alone. Crossing links are
  13–20 units long.
* Peds never spawn on road-crossing links (0x44E790 skips `m_bRoadCross`). Ped spawning only picks ped nodes in
  the area containing the spawn point (`FindRegionForCoors`, same function).
* Cars do not know about crossings; they brake for peds in their way. At signalled junctions every car
  approach must carry a light, otherwise cars can pass during the ped phase.
* `paths.py` flags ped links that cross a land car link in 2D at heights within 3 units (`detect_crossings`,
  `PedLink.crossing=None`). It adds the ped light when the crossed car link has a light or touches a node lights
  point at. Both can be set explicitly.

## 9. Flood fill

* `DoPathSearch` (GR/ReSA 0x4515D0) gives up immediately when `origin.floodFill != target.floodFill`. So:
  **connected nodes must share a value**, otherwise routes between them are impossible. **Disconnected
  components should have distinct values**, otherwise the search explores the whole reachable component
  before failing. That costs time but does not crash.
* VAN: the vehicle graph (land and water together) has 33 components with values 1–33; the main road
  network is 1 and the main water network is 2. The ped graph has 179 components with values 1–179,
  numbered independently. Every component has exactly one value, and no value is shared.
* `paths.py` numbers components by decreasing size from 1, separately for vehicles and peds. Past 254
  components the rest share 255, with a warning.

## 10. Dead-end bit (flag bit 4)

`ThisNodeWillLeadIntoADeadEnd(start, prev)` (0x44D310; ReSA and SAX agree, GR's condition `== 4 || > 10`
is wrong) walks away from `prev`. At each node it counts loaded neighbours with behaviour 0 or > 10,
excluding the node it came from. Zero means a dead end (true), two or more mean not a dead end (false),
exactly one means it continues. It has no visited set.

VAN's bit 4 is a subset of "nodes removed by iterative leaf pruning of the normal-node vehicle graph, plus
all special-behaviour nodes" (2,378 + 216 nodes, and 227 pruned nodes without the bit). Every VAN walk
terminates. `paths.py` sets exactly that set and simulates every walk (start node × neighbour) to make sure
it terminates; it clears the bit otherwise. The validator reports a never-ending walk as an error.
Cycle-only components (ring roads) never get the bit.

## 11. Link lengths

* uint8 = **floor(3D distance), clamped to [1, 255]**. This is the game's own rule when it builds interior
  links (`StoreInteriorNodeLink` 0x450FB0, ReSA: `max(1, (uint8)min(dist3D, 255))`). It matches VAN in
  99.5 % of links; the rest are 1 less, consistent with R* computing on unquantized positions.
* Used as Dijkstra edge weights. The hash buckets are `distance & 511` and the sums are int16
  (`m_totalDistFromOrigin`), so lengths must stay ≤ 255. Longer links are split; `paths.py` splits at 100 units
  by default (`max_link_length`, ≤ 255). VAN maximum: 165 (vehicle) and 81 (ped); medians 11 and 8.

## 12. Things that crash, hang or silently break

| condition | effect | where |
|---|---|---|
| `m_totalDistFromOrigin != 0x7FFE` | `RemoveNodeFromList` on a node not in the list (garbage pointer) | DoPathSearch |
| vehicle node with 0 links | integer division by zero | CCarCtrl::InitSequence |
| all links of a vehicle node share one navi | `do {} while (OldLink == NewLink)` never ends | GenerateOneRandomCar (SAX) |
| bit 4 where the dead-end walk cycles | infinite loop at spawn | ThisNodeWillLeadIntoADeadEnd |
| link / navi index beyond the target area's count | out-of-bounds read | everywhere (no bounds checks) |
| car link navi link 0xFFFF or garbage | OOB read of area 63 or another area | PickNextNodeRandomly, steering |
| reserved link slots without area 0xFFFF | dynamic-link scan runs off the array | AddDynamicLinkBetween2Nodes_For1Node |
| > 1024 navis in an area | the 10-bit index wraps, wrong navi | CCarPathLinkAddress |
| `numNodes != veh + ped` | ped spawn indexes past the array | GeneratePedCreationCoors |
| N + 192 > 32767 | `m_wBaseLinkId` (int16) overflows | all link access |
| > 11 links on a node that gets a dynamic link | overflows the 12-slot block | AddDynamicLinkBetween2Nodes_For1Node |
| missing area entry in the IMG | the request never completes | CStreaming |
| stale `roadblox.dat` | GenerateRoadBlocks reads node ids that do not exist in our files (OOB) | §13 |
| connected nodes with different flood fill | routes fail (no crash) | DoPathSearch |
| car link with no lanes | never used by traffic (VAN has 12) | PickNextNodeRandomly |
| 2D length 0 between linked nodes | NaN directions | FindPathDirection, navi dir |

`validate_areas` checks all of these that are visible in the files.

## 13. `data\paths\roadblox.dat` must be regenerated

`CRoadBlocks::Init` (GR 0x461100) reads a loose file, not from the IMG: `int32 NumRoadBlocks` followed
by 325 `CNodeAddress`. It always reads 1,300 bytes; the retail file is 1,304 bytes.
`GenerateRoadBlocks` (0x4629E0) runs while the player is wanted and in a vehicle. It calls
`GetPathNode(addr)` and `GetRoadBlockNodeInfo` (0x460EE0) on the first `NumRoadBlocks` entries.
`GetRoadBlockNodeInfo` reads links `base+0` and `base+1` and their navis without checks.

Requirement: every entry must be a land vehicle node with ≥ 2 links, ideally a mid-block node with exactly
2 links. The vanilla file points into vanilla node ids and **must be replaced**. Use `write_roadblox` with
`CompiledPaths.roadblock_addresses` (nodes with `police_roadblock=True`); count 0 is a safe minimum.
Entries past the count are skipped (`InOrOut` stays true). The roadblock width is
`(max lanes of the two navis + 1) · 5`, centred on the node, split around the node width if it is nonzero.
Script roadblocks are separate. `SWITCH_ROADS_OFF`-style switches are stored as boxes, at most 64, and are saved
in the savegame (`CPathFind::SwitchRoadsOffInArea`/`Save`).

## 14. Limits (enforced by `paths.py`)

| limit | value | why |
|---|---|---|
| areas | 64 | file names / 6-bit navi area |
| navi nodes per area | 1024 | 10-bit index (VAN max 985 in area 22) |
| links per area | 32,575 | int16 base link + 192 reserve (VAN max 6,328) |
| nodes per area | 32,767 (conservative) | uint16 ids, 0xFFFF invalid (VAN max 2,889) |
| links per node | 1…11 (warn above 8) | §4 |
| lanes per direction | 0…7 | 3 bits |
| link length | 1…255, split above `max_link_length` | uint8 |
| coordinates | world in [−3000, 3000], z in [−4096, 4095.875] | int16/8 and region formula |
| node width | 0…15.94 | uint8/16 |
| navi width (median) | 0…7.94 | ≤ 127 to avoid the int8/uint8 ambiguity (VAN max 7.5) |
| flood fill | 1…255 per graph | uint8 |
| police roadblocks | 325 | `RoadBlockNodes[325]` |

## 15. Open uncertainties (and the conservative choice made)

1. **`OneWayLaneOffset` for two-way links.** Two disassembly-based reconstructions agree (ReSA PC 0x44DB00,
   WG iOS); GR's inline header differs. VAN never has ≥ 2 lanes in *both* directions on land, and only 2 % of
   two-way navis have a median. N+N links follow the code but are less proven in practice.
   Conservative: 1+1 for two-way streets; model multi-lane roads as separate one-way carriageways
   like R* did, or accept N+N.
2. **`DoPathSearch(sameLaneOnly)` lane sense** looks inverted compared with PickNextNodeRandomly (§5.2).
   We follow PickNextNodeRandomly, the spawn code and VAN. Only script-flagged mission cars are affected.
3. Unknown or unused bits (node 9, 11, 14, 15, 24–31; navi byte 11 bit 7; light word bits 3–15) and
   behaviour values 3, 4, 6, 7, 11–15. Always written as 0 / not used.
4. The exact R* rule for bit 4. VAN's set is a subset of ours; the extra bits only add an extra check when cars spawn.
5. Light object heading → displayed phase is code-verified, but each traffic-light model's local axes are not.
   Check them when placing objects.
6. Empty areas (20 zero bytes) follow from the loader code, but no retail file is empty.
7. Navis stored outside their position's area (spill) are legal by code but rare in VAN. Prefer
   simplifying the graph over relying on spill.
8. Node z interpolation: no hard tolerance besides spawn ±7 (cars) and ±3 (peds). Ghost cars ride on the
   interpolated node line, so sample hills densely.
9. Not checked: the PC version of `IsThisAnAppropriateNode` and `GenerateCarCreationCoors2` comes from SAX
   (ASM match < 100 %). The facts used here (flag tests, parking missions, slope ≤ 0.5) are simple
   branches, and VAN statistics agree with them.

## 16. `tools/gta/paths.py` in short

* Input: `PathNetwork` with `CarNode(x, y, z, width, spawn, switched_off, speed, boat, roadblock,
  dont_wander, behaviour, police_roadblock, tag)`, `PedNode(x, y, z, width, spawn, switched_off, dont_wander, tag)`,
  `CarLink(a, b, lanes_ab, lanes_ba, median, light_toward, light_group)` and
  `PedLink(a, b, crossing, ped_light)`. Helpers: `add_car_polyline`, `split_car_link`, `signalize_junction`.
* `compile_network(net, max_link_length=100, dead_end_flags=True, detect_crossings=True, spill_navis=True,
  drop_isolated=False, z_func=None)` → `CompiledPaths` with 64 `AreaData`, address maps (input index →
  (area, id)), roadblock list, warnings, stats. `.write(dir)` writes all 64 files; `.write_roadblox(path)`.
  The compiler validates its own output with `strict=True` and raises `PathError` on any problem.
* Reader and validator: `parse_area`, `read_area_files`, `validate_areas(areas, strict, roadblocks)`,
  `read_roadblox`. Geometry helpers: `lane_center_offsets`, `carriageway_extents`, `recommended_road_width`,
  `area_of`, `area_bounds`.
* CLI: `python3 tools/gta/paths.py validate <dir> [--strict] [--roadblox f]`, `… stats <dir>`.
* Tests: `python3 tests/test_paths.py`. Set `GTA_SA_NODES_DIR` to also round-trip the vanilla files.
