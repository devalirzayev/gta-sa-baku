# SA data files for a total conversion: water, zones, zone text, radar, tracks, hard-wired SA stuff

Verified spec for GTA San Andreas PC 1.0 (gta_sa.exe US). The game code wins over the wiki; where only a
secondary source exists, the claim is marked.

## Sources and how to read the citations

| Tag | Source |
|---|---|
| **GR** | gta-reversed `master`, `source/game_sa/<file>`, fetched 2026-11-08 from raw.githubusercontent.com. Hook addresses are the gta_sa.exe 1.0 function addresses. Functions marked `NOTSA` or `{ .Reversed = false }` there are not authoritative. |
| **ReSA** | `pedrokilo/ReSA` (`source/game_sa/WaterLevel.cpp`, `X87Math.cpp`, `Entity/Vehicle/Train.cpp`, `TheZones.cpp`, `CarCtrl.cpp`, `Plane.cpp`, plus the Ghidra dump `reversing/VehicleAudio355Evidence/Train_6F55D0.txt`). It transcribes functions gta-reversed has not reversed yet, with addresses, and says it validates them against the exe. It is a secondary source, used where GR is missing. |
| **MTA** | `multitheftauto/mtasa-blue` `Client/game_sa/CWaterManagerSA.{h,cpp}`: the sizes of the water pools in the exe and the outside-world water addresses. |
| **EUR** | aap `librwgta` `tools/euryopa/WaterLevel.cpp` `InitialiseWater()`: an independent water.dat reader. |
| **CLEO4 / ML** | `cleolibrary/CLEO4 source/CTextManager.cpp`, and `thelink2012/modloader src/shared/fxt_parser/{fxt_parser.hpp,fxt.hpp}` with `include/modloader/gta3/fxt.hpp`. |
| **VAN** | Vanilla PC data files (`Google61/gta3sa.app`, identical to `fMamprim/gitieiMEd`). water.dat is 70 099 B (md5 690400ec…), info.zon 27 759 B, map.zon 428 B, tracks.dat 24 547 B, tracks2 1 824 B, tracks3 8 027 B, tracks4 17 690 B, popcycle.dat 126 382 B. The copies are kept in the scratchpad (`datafiles_ref/g61`), not in the repo. |
| **CR** | `Joshimuz/Cretaceous-Runner-Speedrun-Version` data files: a released SA total conversion, used as a real-world example. |
| **WIKI** | gtamods.com pages Water.dat, ZONE, Tracks.dat, FXT, GXT, Gta.dat and CULL. They are thin and partly wrong, so they are used only as a cross-check. |

Every vanilla data file under §§a, b and e round-trips through the tools here. Run
`SA_DATA_REF=<dir> python3 tests/test_data_files.py`: water.dat comes back byte-identical, and zones and
tracks come back with the same stored values.

---

## a) `data/water.dat`

### Loading
* `CWaterLevel::WaterLevelInitialise` (0x6EAE80) is called from `CGame::Init2`, after gta.dat [GR Game.cpp].
  It zeroes the counters, parses `DATA\water.dat`, calls `FillQuadsAndTrianglesList` (0x6E7B30) and loads the
  textures `waterclear256`, `seabd32` and `waterwake` from `particle.txd` [GR WaterLevel.cpp].
  `DATA\water1.dat` is read instead when `m_nWaterConfiguration == 1`. Script command `SET_WATER_CONFIGURATION`
  0x0857 switches it [ReSA LoadDataFile; GR eScriptCommands.h]. The file must exist: the open is
  not checked [ReSA]. Ship `water1.dat` as a copy of `water.dat`.
* Lines are read with `CFileLoader::LoadLine` (0x536F80), which calls `fgets(ms_line, 512)`. Characters below
  0x20 (including `\r` and `\t`) and `,` become spaces, and leading whitespace is skipped [GR FileLoader.cpp
  LoadLine, FileMgr.cpp ReadLine]. As a result:
  * A line, including its newline, must fit in 511 bytes. A vanilla quad line is about 250 characters.
  * Commas, tabs and CRLF are all accepted (CR's file uses tabs).
* **Skipped lines** are those whose first non-blank character is `;`, `*`, `p` or NUL. The `processed`
  header is skipped this way, because it starts with `p` [ReSA 0x6EAF80..0x6EAF9C; EUR skips `;` `*` `p`].
  The header is not required, and it carries no meaning to the game.
* **`#` is not a comment.** Every other line is parsed with `sscanf`, and a failed parse falls through to the
  triangle path. That path calls `AddWaterLevelTriangle` with the values left over from the previous line
  [ReSA; EUR has the same fall-through]. A `#` line that follows a data line therefore adds a copy of that
  line's first three vertices as a triangle, and only 6 triangle slots exist. CR's water.dat starts with two
  `#` lines. It works only because nothing has been parsed before them (the stack is uninitialised).
  Never write comments or blank lines. `water.parse_water_dat` raises on them.

### Line format
```
processed
x y z flowX flowY bigWaves smallWaves    x y z ...    x y z ...    [x y z ...]  flags
```
* Parsing per line follows [ReSA LoadDataFile; EUR]:
  1. `sscanf` with 28×`%f` then `%d`. If that returns 29, the line is a **quad** with flags.
  2. Otherwise `sscanf` with 28×`%f`. If that returns 28, the line is a quad and flags defaults to **1**.
  3. Otherwise the line is a **triangle**: `sscanf` with 21×`%f` then `%d`. The result is not checked, and
     flags defaults to 1.
* Per vertex (7 values): `x y z flowX flowY bigWaves smallWaves`. This field order is the same in GR
  (`ReadNextVertex`), ReSA and EUR.
  * x and y are truncated to int with `_ftol` and stored as int16 in `CWaterVertex` [GR WaterLevel.h; ReSA].
  * z, bigWaves and smallWaves are floats (`CRenPar`).
  * flow is stored as `int8(flow*64)`, so the usable range is [-2, 1.98] [GR WaterLevel.h CRenPar; ReSA `Flow`].
    ReSA notes an exe bug: quad vertex 1 gets the flowY of vertex 2.
* Vanilla values [VAN]:
  * flow is always 0.
  * bigWaves ranges 0..1 and smallWaves 0..0.41. The open sea typically uses 0.2–1.0 / 0.24, and harbours,
    lakes and reservoirs use 0.051 / 0.102.
  * Pools use waves 0 / 0.
  * Vertices on the world edge carry bigWaves 1.0 and smallWaves 0, which the engine forces anyway (see below).
* Vanilla formatting [VAN] is `%.1f` for x and y, `%.5f` for the other fields, 4 spaces between vertices and
  2 spaces before an integer flag. `water.format_water_dat` reproduces vanilla byte for byte.

### Flags
* `AddWaterLevelQuad` and `AddWaterLevelTriangle` do the following [GR, ReSA]:
  * `bInvisible = (flags & 1) == 0`
  * `bLimitedDepth = (flags & 2) != 0`
  * Other bits are ignored.
* **1 = visible**, the normal value (284 vanilla polys).
* **0 = invisible water**. It is still water for physics, because `GetWaterLevelNoWaves` does not look at
  bInvisible, but it is never rendered (2 vanilla polys at z 5.27) [GR `CWaterPolygon::DoMarkToBeRendered`].
* **3 = visible + limited depth** (21 vanilla polys: swimming pools and ponds at z 7–112, plus one interior
  pool at z 1082.7).
  A point more than **6 units below** the surface of such a poly does not count as being in it
  [ReSA `WaterLevelQuadCpp`/`WaterLevelTriangleCpp` 0x6E5BB0/0x6E5E90: `level - 6 > z && limited → false`].
* Any poly is ignored for a point more than **20 units above** its surface (`level + 20 < z`) [same].
  This is what allows several water layers to overlap in x/y.
* Interior water: when the poly's first vertex, in engine order, has z > 950, it is drawn only while
  `CGame::currArea != 0`; all other water is drawn only in the normal world
  [GR WaterLevel.h `IsInInterior`, `MarkQuadsAndPolysToBeRendered`/`BlockHit`].

### Vertex sharing and coordinate snapping
* `AddWaterLevelVertex` (0x6E5A40) [GR, ReSA]:
  1. If x ≤ -3000, x ≥ 3000, y ≤ -3000 or y ≥ 3000, the coordinate is **clamped to ±3000** and the
     parameters are forced to z=0, bigWaves=1, smallWaves=0, flow=0 [ReSA, matching VAN edge vertices].
     GR clamps too but zeroes the parameters.
  2. It then **reuses an existing vertex** when x, y and the float32 z are all equal. The waves and flow of the
     first poly that created the vertex win.
  * Consequence: shared corners must have bit-identical z. Vanilla wastes vertices on 40.6 vs 40.59998.
  * Consequence: water touching the map edge must be at z=0, so a lake must not touch ±3000.
* x and y are **truncated** (`_ftol`), not rounded. The game itself does not make them even.
* **All coordinates must be even integers** (derived from the code, so treat it as a hard rule):
  * The high-detail renderer `RenderHighDetailWaterRectangle` (0x6EB810) computes `half = (max-min)/2` and
    `RenderHighDetailWaterRectangle_OneLayer` (0x6E91D0) divides by it with an integer `idiv`, so a piece
    1 unit wide or tall is a division by zero [ReSA].
  * The pieces come from cuts at the camera range. That range is always even:
    `CameraRangeMin/Max = 2*floor/ceil((cam±48)/2)` [GR `SetCameraRange`]. An odd edge next to the camera range
    boundary produces a 1-unit sliver. Example: with a quad edge at x=101 and the camera at x≈149, the range
    minimum is 100 and the piece [100,101] makes the game crash.
  * With even coordinates every cut is even. The size splits at `min + (half/2)*2` are even too [ReSA].
  * Corroboration: all 1222 vanilla vertices are even [VAN], and MTA forces even values (`(long)x & ~1`) in
    `CWaterManagerSA::CreateVertex` [MTA].
  * CR has one odd edge (y=2317): a latent crash.
* The world is ±3000. `GetWaterLevelNoWaves` computes the block as `floor(x*0.002+6)`, and blocks 0..11 make up
  the water world [ReSA 0x6E8580].

### Geometry rules
* **Quads are axis-aligned rectangles.**
  * The engine sorts a quad's vertices: the two with minimum y become v0 and v1, sorted by x, and the other two
    become v2 and v3, sorted by x [ReSA AddWaterLevelQuad; GR sorts by (y, x)].
  * Rendering uses only `RenderWaterRectangle(v0.x, v1.x, v0.y, v2.y, …)` [ReSA RenderWater].
  * The water test checks `v0.x ≤ x ≤ v1.x && v0.y ≤ y ≤ v2.y` and interpolates z over the two triangles of the
    v1–v2 diagonal [ReSA WaterLevelQuadCpp].
  * Any other shape is treated as its bounding rectangle. A quad with all x equal or all y equal is silently
    dropped [GR].
  * Vanilla order is SW, SE, NW, NE (301/301 quads) [VAN].
* **Triangles are right triangles with a horizontal and a vertical leg.**
  * The engine puts the vertex pair with equal y first, sorted by x, followed by the third vertex [GR/ReSA
    `AddWaterLevelTriangle`].
  * Rendering assumes `Y1 == Y2` and `X3 == X1 or X3 == X2` [GR `SplitWaterTriangleAlongXLine`: "triangles
    always contain a 90deg corner"].
  * The water test is `u+v ≤ 1` when v0.x == v2.x, and `u ≥ v` otherwise [ReSA WaterLevelTriangleCpp].
  * Use **45° triangles only** (legs of equal length):
    * The high-detail triangle renderer steps both axes by `(X2-X1)/2` [ReSA 0x6EDDC0/0x6E8780].
    * The Y-split computes `XS = X1 + Δy·w/h` with integer division [GR `SplitWaterTriangleAlongYLine`].
    * Any other slope gives mis-sized meshes and odd split points, and therefore the same risk of a 1-unit
      division by zero.
    * All 6 vanilla triangles are 45° with even legs (60–188 units).
* There is no maximum poly size. Flat (far) polys are split at 168 units [GR `BigPolySize`], and vanilla has a
  quad 5832 units wide. The smallest vanilla quad is 12×8.

### Limits (fixed arrays, **no bounds checks**)
| Pool | Size | Address | Vanilla use [VAN] | Overflow |
|---|---|---|---|---|
| vertices `m_aVertices` | **1021** | 0xC22910 | 1021 | corrupts what follows the array |
| quads `WaterQuads` | **301** | 0xC21C90 | 301 | overwrites `WaterTriangles` |
| triangles `WaterTriangles` | **6** | 0xC22854 | 6 | overwrites `NumWaterTriangles/Quads/Vertices` |
| block lists `m_PolyCombos` | **700** words | 0xC215F8 | 633 | overwrites `m_BlockPolyInfo` |

* Sources: sizes and addresses from GR WaterLevel.h. MTA gives the same numbers (`NUM_DefWaterVertices 0x3FD`,
  `Quads 0x12D`, `Triangles 6`, `ZonePolys 0x2BC`). The contiguity of the arrays follows from the addresses.
  WIKI's figure of "1021 planes" is wrong.
* **Vanilla fills the vertex, quad and triangle pools exactly.** A replacement file can never be larger than
  vanilla.
* Block lists: `FillQuadsAndTrianglesList` adds each poly to every 500×500 block (12×12 grid) it overlaps.
  * A quad is added to the block when `v1.x > xlo && v0.x < xhi && v2.y > ylo && v0.y < yhi`
    [ReSA 0x6E7B30].
  * A block holding a single poly stores it inline. A block with n ≥ 2 polys uses **n+1** words of
    `m_PolyCombos` [ReSA `AddToQuadsAndTrianglesList` 0x6E5750; MTA `CWaterZoneSA::AddPoly`, same algorithm].
  * Long thin polys crossing many blocks are therefore expensive.
* `water.validate_water` simulates all of this (`GameWater`) and reports usage and headroom.
* The "outside world" render list holds only 70 blocks [GR `m_MaxNumBlocksOutsideWorldToBeRendered`]. MTA
  raises it to 512 for long draw distances, so far ocean blocks can drop out.

### "Am I in water?"
* `CWaterLevel::GetWaterLevelNoWaves` (0x6E8580) [ReSA] works as follows:
  1. It computes the block of (x, y).
  2. **Outside the 12×12 grid (|x| or |y| ≥ 3000): it returns true with level 0, bigWaves 1 and
     smallWaves 0.** This is the endless ocean.
  3. Inside the grid it walks the block's poly list (quads first, in file order, then triangles) and returns
     the first poly that contains the point and passes the depth checks above.
  4. A block with no polys means no water.
* `GetWaterLevel` (0x6EB690) [GR] then returns false when the surface is more than 3 units above the point,
  unless the caller passes `bTouchingWater`. Otherwise it adds the waves (`AddWaveToResult`).
* `water.GameWater.level_at(x, y, z)` reimplements this check.

### Rendering
* `ScanThroughBlocks` finds the 500-unit blocks inside the camera frustum. `BlockHit` marks their polys
  (visible ones, with matching interior state), and also queues blocks at or beyond the edge for the
  outside-world pass [GR].
* `RenderWater` (0x6EF650) [ReSA]:
  1. It draws seabed strips (`seabd32`) along the world-edge blocks.
  2. It draws each marked triangle and quad in two texture layers (`waterclear256`, at 12.5 and 25 units per
     repeat) using the timecyc water colour.
  3. **For every queued block outside the grid it draws a 500×500 ocean rectangle at z=0** (bigWaves 1).
     The z=0 constants are at 0x6EFECC/0x6EFF0C/0x6EFF4A/0x6EFFA6 (render), 0x6E873F (collision) and
     0x6EA238 (sound) [MTA `SetOutsideWorldWaterLevel`].
* Inside the camera range (±48 units, `DETAILEDWATERDIST`) water is drawn as a 2-unit wave mesh. Beyond it is
  drawn as flat polys [GR].
* **Consequences for Baku:**
  * Beyond ±3000 there is **always** sea at z=0, both rendered and swimmable, and it cannot be removed with data.
  * Inside ±3000 there is water only where water.dat defines it. The Caspian part of the map must be covered
    by quads that reach the map edge, as vanilla does with quads ending at ±3000, so it joins the outside
    ocean without a gap.
  * Land edges of the map (north and west) will show open sea at the horizon. Raise or close the terrain at
    the edge.

### Water at other heights (lakes)
* Give all of a quad's vertices the lake level as z. Sloped water is allowed: z is interpolated, and vanilla
  has a 0 → -5 quad.
* Keep the lake inside ±3000, because edge vertices are forced to z=0. Below 950 it counts as exterior water.
* **Flags:**
  * Use 1 for a normal lake.
  * Use **3** (limited depth) when there is walkable space more than 6 units below the surface (tunnels,
    underpasses, basements). Otherwise those spaces count as underwater.
  * A poly is also ignored more than 20 units above its surface, so a lake above a lower water layer does not
    hide it.
* Use small waves, like vanilla reservoirs: bigWaves 0.05, smallWaves 0.1.
* **Budget tactics:**
  * Water may extend under terrain, because terrain hides it, and a point above the surface is not in water.
    So cover the sea with few large rectangles running under the shore, and cover lakes with coarse
    rectangles whose overlap the shore banks hide.
  * Never put water under land that lies lower than the water level (depressions, cuttings) unless it is
    limited depth and more than 6 units down.
  * Prefer quads to triangles (only 6 triangles exist), and share corners exactly.
  * `water.rects_from_mask(mask, x0, y0, cell)` does a greedy rectangle cover of a raster mask.
  * Check every candidate set with `validate_water`.

---

## b) Zones: `data/info.zon`, `data/map.zon` (and `popcycle.dat`)

### Loading and format
* Both files are listed in gta.dat as `IPL DATA\MAP.ZON` and `IPL DATA\INFO.ZON`, before the map IPLs
  [VAN gta.dat; GR `CFileLoader::LoadLevel`].
* `CFileLoader::LoadScene` (0x5B8700) opens the file `rb` and handles it as follows [GR]:
  * `LoadLine` is applied, so commas and CR become spaces.
  * Empty lines and lines starting with `#` are skipped.
  * A line starting with `zone` opens the section, and a line starting with `end` (case-sensitive, at line
    start) closes it.
* `CFileLoader::LoadZone` (0x5B4AB0) parses each line with `sscanf("%s %d %f %f %f %f %f %f %d %s")`. The 10
  fields are **name, type, x1, y1, z1, x2, y2, z2, level, gxtKey** [GR]. Vanilla writes
  `SUNMA, 0, -2353.17, 2275.79, 0.0, -2153.17, 2475.79, 200.0, 1, SUNMA`.
* `CTheZones::CreateZone` (0x5728A0) [GR, ReSA]:
  * Both labels are **upper-cased and cut to 7 characters** (`char[8]`). The sscanf buffers are only 24 and 12
    bytes, so keep both labels at 7 characters or fewer.
  * The box is min/max sorted and stored as **int16, truncated toward zero**. Use integer coordinates when
    tiling zones without gaps.
  * Zone **types**:
    * 0 = `ZONE_TYPE_NAVI` (all 378 vanilla info.zon lines).
    * 1 = `LOCAL_NAVI`. It goes into the same navigation array but is skipped by "type 0 only" lookups.
    * 2 = INFO. It is **ignored**: nothing is created [ReSA].
    * 3 = MAP (map.zon).
* Containment is an inclusive 3-D box test (`PointLiesWithinZone` 0x572270). Choose z ranges that cover the
  terrain and stay below the ~1000 interior band: vanilla uses -89..110 or 0..200, and `zones.navi_zone`
  defaults to -100..900.

### Limits (no bounds checks)
| Array | Size | Built-in | Vanilla [VAN] |
|---|---|---|---|
| `NavigationZoneArray` | **380** | `SAN_AND` (whole map, created in `CTheZones::Init`) | 1 + 378 |
| `ZoneInfoArray` | **380** | index 0 for SAN_AND | 1 + 377 distinct names |
| `MapZoneArray` | **39** | `THEMAP` | 1 + 6 |

* Sources: GR TheZones.h, and `CTheZones::Init` 0x572670.
* So at most 379 info zones, 379 distinct info names and 38 map zones are available. The arrays are contiguous
  with `m_CurrLevel` and the counters.
* **Zones are stored in the savegame** (`CTheZones::Save/Load`), so changing the .zon files needs a new game.

### Two labels per zone
* The **name** (field 1) is the *info label*.
  * Zones with the same name **share one `CZoneInfo`** (`AssignZoneInfoForThisZone` 0x572180). That
    `CZoneInfo` holds the popcycle type, 10 gang densities, dealer density, the no-cops flag and allowed races.
  * Script commands address the zone by this label: `SET_ZONE_POPULATION_TYPE` 0x0767,
    `SET_ZONE_GANG_STRENGTH` 0x076C, `SET_ZONE_DEALER_STRENGTH` 0x076A, `SET_ZONE_POPULATION_RACE` 0x0874 and
    `SET_ZONE_NO_COPS` 0x09B7, which go through `FindZoneByLabel(…, ZONE_TYPE_INFO)` [GR ZoneCommands.cpp].
  * Defaults for every zone info, without script: PopType = **RESIDENTIAL_AVERAGE** (5), all races, gangs 0,
    dealers 0 [GR `CZoneInfo` and `InitZonesPopulationSettings` 0x5720D0; ReSA, same bits].
* The **gxtKey** (field 10) is the display text key. map.zon uses `UNUSED`.

### Zone name on screen
* Each frame `CPlaceName::Process` (0x571F20) runs `CTheZones::FindSmallestZoneForPosition(playerPos, false)`
  [GR PlaceName.cpp, TheZones.cpp]. It returns the containing navigation zone (type 0 or 1) with the smallest
  `(x2-x1)+(y2-y1)`:
  * The search starts from zone 0, SAN_AND.
  * Comparisons are strict, so ties keep the earlier zone. A zone exactly as large as the map never beats
    SAN_AND.
  * Outside every zone the result is SAN_AND.
* The text is `TheText.Get(zone.m_TextLabel)`. CHud draws it in `FONT_GOTHIC` at the bottom right: about 1 s
  fade-in, 3 s shown, 1 s fade-out, and it starts again whenever the label pointer changes
  [GR Hud.cpp `DrawAreaName`].
* Script command 0x09BA `DISPLAY_ZONE_NAMES` switches the zone name display off and on.
* **For Baku:**
  * Approximate each district with one or more axis-aligned boxes that share the name and GXT key. Nest
    smaller boxes for sub-areas, because the smallest box wins.
  * Override the GXT key **`SAN_AND`** with the name for "outside all zones" (for example "Bakı").
* Not used: the `level` field of navigation zones. Only map zones are queried (`GetLevelFromPosition`).
  Vanilla writes 1.

### map.zon level → `CTheZones::m_CurrLevel`
* `CTheZones::Update` (0x572D10) sets `m_CurrLevel = GetLevelFromPosition(player)` every 5 s. That function
  returns the level of the first map zone after THEMAP that contains the point, and otherwise 0
  [GR TheZones.cpp].
* Levels: 0 countryside, 1 Los Santos, 2 San Fierro, 3 Las Venturas [GR eLevelName.h].
* Effects:
  * Cop models: `ms_aDefaultCopModel[level]` and `ms_aDefaultCopCarModel[level]`. Index 4 is the cop bike
    [GR Streaming.cpp `StreamCopModels`, `GetDefaultCopCarModel`].
    * Per web-gta (secondary) the cars are 599 copcarru, 596 copcarla, 597 copcarsf and 598 copcarvg, and the
      peds are csher, lapd1, sfpd1 and lvpd1.
  * **Cop bikes only when the level is not 0** and the wanted level is below 3 [GR `StreamCopModels`].
  * Ambulance, medic, fire truck and fireman models come from per-level tables
    [GR `StreamAmbulanceAndMedic`/`StreamFireEngineAndFireman`].
  * **Random ambulances and fire trucks are not generated at all at level 0**:
    `CCarCtrl::GenerateEmergencyServicesCar` returns early when `m_CurrLevel == 0` [ReSA 0x42F9C0].
  * Cab-driver model [GR `GetDefaultCabDriverModel`] and the shop price multiplier `gPriceMultipliers[level]`
    [GR Shopping.h].
  * Restart choice: a candidate restart in another level gets its distance multiplied by 6, unless the death
    point is at level 0 [GR Restart.cpp].
  * Train station skipping: a station is allowed only if `STAT_CITY_UNLOCKED` (stat 181) + 1 ≥ its level
    [ReSA `IsNextStationAllowed`].
* **Not** affected by the level: weather (§f), radio, and civilian peds and cars. Civilian population comes
  from popcycle, pedgrp and cargrp.
* **Recommendation:** one map zone over the whole ±3000 at **level 1**. Then cops, bikes, ambulances and fire
  trucks all work, and stations are always allowed. Use level 0 boxes only for areas that should feel rural.
  Levels 2 and 3 need stat 181 set by script before trains may stop in them.

### popcycle.dat
* `CPopCycle::Initialise` (0x5BC090) reads `DATA\POPCYCLE.DAT` [GR PopCycle.cpp]. It skips lines that are empty
  or start with `/`, and then expects exactly **20 zone types × 2 (weekday, weekend) × 12 two-hour slots =
  480 lines** [VAN 480].
* Each line holds 24 numbers in 0–255: `#Peds #Cars Dealers Gang Cops Other` and then 18 percentages for the
  "other" groups (Workers … Aircrew_runway). Trailing `// …` text is ignored.
* The 18 percentages must sum to ≥ 100: the exe rescales sums below 100, imperfectly, and
  `PickARandomGroupOfOtherPeds` fails on low sums [GR comment].
* The block order is `eZonePopulationType`: BUSINESS, DESERT, ENTERTAINMENT, COUNTRYSIDE, RESIDENTIAL_RICH,
  RESIDENTIAL_AVERAGE, RESIDENTIAL_POOR, GANGLAND, BEACH, SHOPPING, PARK, INDUSTRY, ENTERTAINMENT_BUSY,
  SHOPPING_BUSY, SHOPPING_POSH, RESIDENTIAL_RICH_SECLUDED, AIRPORT, GOLF_CLUB, OUT_OF_TOWN_FACTORY,
  AIRPORT_RUNWAY [GR ZoneInfo.h]. `zones.POP_TYPES` lists them.
* How it connects to zones: `CPopCycle::Update` takes `CTheZones::GetZoneInfo(player)->PopType`, which is the
  smallest navigation zone's info, or SAN_AND's, and uses that block. The ped and car models come from
  `pedgrp.dat` and `cargrp.dat` (`CPopulation::LoadPedGroups/LoadCarGroups`) [GR].
* The vanilla popcycle, pedgrp and cargrp are map-independent and can be kept. **Our main.scm must call
  `SET_ZONE_POPULATION_TYPE` for every info label**, otherwise everything is RESIDENTIAL_AVERAGE and there are
  no gangs or dealers.

---

## c) Zone-name text

### Lookup
* `CText::Get(key)` (0x6A0050) [GR Text.cpp]:
  1. If the key is empty or starts with a space, it returns "".
  2. Otherwise it binary-searches the **MAIN** table's TKEY by `CKeyGen::GetUppercaseKey(key)`.
     That hash is CRC-32 (reflected, polynomial 0xEDB88320) with init 0xFFFFFFFF and **no final XOR**, taken
     over the key with a–z upper-cased. In Python it is `zlib.crc32(KEY) ^ 0xFFFFFFFF`, verified against the
     engine's own table in GR KeyGen.h. `CRC("SAN_AND") = 0xAE14F34B`.
  3. If that fails and a mission table is loaded, it searches that table.
  4. **A missing key returns an empty string**, so no zone name is shown.
* The GXT file is `TEXT\<language>.GXT` (american.gxt, …). SA format version 4, 8 bits per character:
  1. `TABL`: 12-byte entries `name[8] + offset`, at most 200 tables. MAIN is listed first and its offset
     points straight at its TKEY. Mission offsets point at `name[8]` followed by TKEY.
  2. `TKEY`: `{uint32 offset into TDAT, uint32 hash}`, sorted by unsigned hash.
  3. `TDAT`: NUL-terminated 8-bit strings.
  * `CText::Load` reads TABL, then MAIN's TKEY and TDAT, in file order [GR Text.cpp, KeyArray, MissionTextOffsets].
  * `zones.build_gxt` and `zones.read_gxt` implement this format. The tables are keyed by hash, so the vanilla
    american.gxt can be read, extended and rewritten.

### FXT, the easy route
* CLEO 4 and modloader both hook `CText::Get` and look up their FXT tables **before** the GXT, so FXT entries
  override existing keys such as SAN_AND [CLEO4 `CText__locate`; ML `GxtHook`].
* File locations: CLEO 4 reads `CLEO\CLEO_TEXT\*.fxt`; modloader reads `*.fxt` anywhere in a mod folder.
* Format: one `KEY text` per line.
  * The key runs up to the first whitespace. The text is everything after **exactly one** separator, with no
    trimming on the left.
  * Trailing whitespace is trimmed. `#` starts a comment, so it cannot appear in text. CLEO 4 also treats `//`
    in the key position as a comment.
  * CLEO 4 reads into a 256-byte line buffer. modloader hashes only the **first 7 characters** of the key.
  * Sources: CLEO4 `ParseFxtFile`; ML `ParseFXT`, `fxt_hash_functor`.
* The bytes are passed to the game unchanged, so the text must already be in the game's 8-bit encoding (below).
  `zones.format_fxt` writes such files.
* Zone keys must be 1–7 characters of A–Z, 0–9 and `_` (`CZone::m_TextLabel[8]`).

### SA font character set and Azerbaijani
* The text bytes in the SA font are as follows:
  * 0x20–0x7E are ASCII.
  * Glyph = byte − 32. For the Gothic and subtitle fonts, glyphs above 155 (bytes above 0xBB) are blank
    [GR Font.cpp `RemapCharacterWithoutGraphic`].
  * The accented glyph order is [GR `CFont::FindSubFontCharacter` comments, confirmed independently by
    majomix GtaGxtTool `GxtReaderSanAndreas`, and consistent with GR Text.cpp `UpperCaseTable`]:
```
0x80 À Á Â Ä Æ Ç È É Ê Ë Ì Í Î Ï Ò Ó Ô Ö Ù Ú Û Ü ß        (0x80-0x96)
0x97 à á â ä æ ç è é ê ë ì í î ï ò ó ô ö ù ú û ü          (0x97-0xAC)
0xAD Ñ   0xAE ñ   0xAF ¿
```
* This is **not** Windows-1252, despite WIKI. In this encoding ö is 0xA8, ü 0xAC, ç 0x9C and ä 0x9A.
* Available Azerbaijani letters: **ç Ç ö Ö ü Ü**. Missing letters: **ə Ə ş Ş ğ Ğ ı İ**.
* Proposed fallbacks (`zones.to_sa_text`):

| AZ | style "sa" (default) | style "ascii" |
|---|---|---|
| ə Ə | **ä Ä**: the 1991–92 Azerbaijani Latin alphabet used Ä for Ə | e E |
| ş Ş | sh Sh | sh Sh |
| ğ Ğ | gh Gh | gh Gh |
| ı | i | i |
| İ | I | I |
| ç ö ü | kept | ch o u |

* Examples: "Dərnəgül" becomes "Därnägül" or "Dernegul", and "Ağ Şəhər" becomes "Agh Shähär" or "Agh Sheher".
* Avoid `~` (GXT tokens), `#` (FXT comments) and `_` (`PrintChar` blanks glyph 63) in displayed text.
* `zones.make_label` derives unique 7-character keys from names.
* Font glyph art for 0x80–0xAF exists, because the PC game ships English, French, German, Italian and Spanish
  with one `fonts.txd`. This was not inspected (unverified).

---

## d) Radar map tiles

* **Count and naming.**
  * `CRadar::Initialise` (0x587FB0) sets `gRadarTextures[y][x] = CTxdStore::FindTxdSlot("radar%02d" % (y*12+x))`
    for a 12×12 grid, giving **144** TXDs named radar00 … radar99, radar100 … radar143 [GR Radar.cpp].
    SAEncyclopedia's disassembly confirms it: the loop bound is `cmp esi, 0x90`.
  * The TXD slots exist only if an IMG archive contains `radarNN.txd`. They are registered by
    `CStreaming::LoadCdDirectory` and are case-insensitive.
* **Tile → world mapping** [GR `GetTextureCorners` 0x584D90, `TransformRealWorldToTexCoordSpace` 0x583600,
  `StreamRadarSections` 0x5858D0]:
  * Tile (col x, row y) covers X ∈ [500x−3000, 500x−2500] and Y ∈ [2500−500y, 3000−500y].
  * Col 0 is the west edge and row 0 the north edge. Index = row·12 + col: tile 0 is the NW corner, tile 11
    NE, tile 132 SW and tile 143 SE.
  * Inside a texture, u runs west→east and v north→south, the normal orientation of a north-up image.
  * `radar.split_radar_image` returns `[(txd_name, texture_name, PIL.Image)]` in this order.
* **Texture inside the TXD.** `DrawRadarSection` and `DrawRadarSectionMap` use `GetFirstTexture(txd)`, so
  the name does not matter [GR]. Vanilla has exactly one texture named like the TXD (`radar143`) [INU-ez
  GTACHECK docs; CianDeps arizona-dumper].
* **Size and raster format.**
  * Vanilla PC tiles are **128×128** [aap radaredit asserts 128×128 when extracting the vanilla tiles; INU-ez
    docs].
  * The raster is **DXT1 without alpha with 1 mip level**: raster format 0x200 (565), fourCC `DXT1`, depth 16,
    D3D9 platform 9, compressed flag. renan-hath/GTASA-Map-Tools writes "Same layout as the radar tiles of the
    game: one DXT1 texture without mipmaps", with filter linear (2) and wrap addressing (0x11). X-Seti
    Img-Factory lists PC as "DXT1·4bpp". *Not verified on a vanilla binary here.*
  * Any power-of-two size works: the UVs are 0–1 per tile, and HD-radar mods ship 256 or 512. Memory is
    144 × size.
  * Use `tools.rw.txd.texture_native(name, img, "DXT1", mipmaps=False)`, or `radar.build_radar_txds`.
* **Minimap.**
  * `DrawRadarMap` streams and draws only the 3×3 tiles around the radar centre [GR].
  * Grid cells outside 0..11 are filled with **RGB (111,137,170)**: the sea beyond ±3000. Use the same colour
    for open water in the tiles (`radar.SEA_RGB`) so the edge is invisible.
  * While `bPlayerIsOffTheMap` the minimap draws grey (204,204,204).
  * A missing TXD (slot −1) is skipped: no tile, no crash [INU-ez docs: `DrawRadarSection` 0x586180 checks −1].
    GR's reimplementation lacks that check.
* **Pause map.** `CMenuManager::PrintMap` draws every in-view tile (x, y) as a screen rectangle at column x
  and row y: west→east, north at the top. It uses `DrawRadarSectionMap` after all sections are streamed
  ("FEM_PWT" wait screen) [GR MenuManager_Pages.cpp]. On the map, short-range blips appear only in 600×600
  cells already visited (`ZonesVisited` 10×10), until more than 80 cells have been revealed
  [GR `HasThisBlipBeenRevealed`].
* Hard-wired on the radar: the "nearest airstrip" plane indicator uses a fixed table (LS, SF, LV and Verdant
  Meadows) [GR Radar.cpp `airstrip_table`].

---

## e) Train tracks: `data/paths/tracks*.dat`

### Files and track ids
* `CTrain::InitTrains` (0x6F7440) runs at the end of `CFileLoader::LoadLevel` and again on restart. It loads
  track ids **0 = tracks.dat, 1 = tracks3.dat, 2 = tracks2.dat, 3 = tracks4.dat** [GR Train.cpp, LoadLevel;
  ReSA].
* What uses which track:
  * **Track 0**: random freight and passenger trains (configs 0–7, from the table at 0x8D44F8) and **all
    station logic**. Vanilla is the LS–SF–LV loop (926 nodes, 18 107 units, 6 stations).
  * **Track 1**: random **trams** (configs 8–9). They are generated only while `CWeather::WeatherRegion == SF`,
    which is a hard-coded rectangle (§f) [ReSA `DoTrainGenerationAndRemoval` 0x6F7900]. Vanilla is the SF
    tram loop (300 nodes).
  * **Tracks 2 and 3**: never used for random trains. `CreateMissionTrain` called with node −1 snaps to the
    nearest node of **any** of the 4 tracks (`FindClosestTrackNode`) [ReSA 0x6F7550]. This is the path used by
    the script command `CREATE_MISSION_TRAIN` 0x06D8, assuming its handler passes −1 (not traced).
    In vanilla they are open lines for missions, with 67 and 656 nodes and a huge closing segment.

### Parser (`ReadAndInterpretTrackFile` 0x6F55D0)
Sources: ReSA, the Ghidra pseudocode, and `TrackNode.h` in GR.
* **Reading.**
  * The whole file is read with `CFileMgr::LoadFile` into a **0xB530 = 46 384-byte** heap buffer. The original
    `LoadFile` ignores the size and appends a NUL [GR FileMgr.cpp comment], so the file must be **< 46 384
    bytes**.
  * The file must exist: a missing file leaves garbage in the buffer.
  * Lines are found by scanning for `\n` only.
* **Header.**
  * The first line is copied and NUL-terminated. If it equals `"processed"` exactly (`memcmp` of 10 bytes, so
    **no CR allowed**), the next line is read. Vanilla has no header; don't write one.
  * That line is parsed with `sscanf("%d")` = N, the node count. Then `new CTrainNode[N]`.
* **Rows.**
  * Each of the N rows is copied *without* a terminator and parsed with `sscanf("%f %f %f %d")`, giving
    `x y z station`.
  * A short row therefore picks up leftovers of the previous longer row, so **always write all 4 fields**,
    and end **every** row with `\n`, including the last.
* **Storage.**
  * Each node is stored as `int16(trunc(v*8))` per axis (1/8-unit precision, |v| < 4096) (`CTrainNode`,
    10 bytes).
  * The cumulative **2-D** distance is stored as `uint16(trunc(3·d))`, and the closing segment last→first is
    always included in the total length.
  * So **closed loop length < 65 535/3 = 21 845 units**, and consecutive nodes must be more than 1/3 unit
    apart. If two nodes have equal stored distances, `FindCoorsFromPositionOnTrack` computes `1/0` and the
    train position becomes NaN.
* **Stations.**
  * Only in tracks.dat (track 0), every row with a non-zero 4th field overwrites the next entry of the
    hard-coded `CTrain::aStationCoors[6]`, which holds the SA stations at 0x8D48F8 [GR Train.cpp].
    **There is no bound check: at most 6.**
  * `InitTrains` then sets `StationDist[i]` from `FindClosestTrackNode(aStationCoors[i])` and reads the result
    index in **track 0's** array [ReSA].
  * Stations are assumed to be in ascending track order, and a station within 100 units counts as "at"
    it [ReSA `FindNextStationPositionInDirection`].
* `tracks.format_tracks_dat` writes the exact stored values (multiples of 1/8). Vanilla re-reads to identical
  `CTrainNode` data.

### Runtime behaviour worth knowing
* **Random trains** [ReSA 0x6F7900]:
  * Random trains more than 220 units from the camera are deleted.
  * If no random train exists, a random node of track 0 within **70 units** of the camera (2-D; 100 when the
    player is within 60 of a station) is walked along the track until it is 170–220 units away, and a train
    is spawned there.
  * The speed is 15–44, halved within 300 units of the hard-coded point (2222, −1750).
  * `bDisableRandomTrains` stops generation, but not the removal.
* **Disabling random trains from main.scm:** `SWITCH_RANDOM_TRAINS` 0x06D7 (`CTrain::DisableRandomTrains`).
  The flag is reset to "enabled" by every `InitTrains`, that is on load and restart, so set it at script start
  and after loading a save.
* **Stopping at stations.** A train stops when its front is about 35–40 units past the station node
  (`FindMaximumSpeedToStopAtStations`: ±40, < 5), so **put the station node at the platform centre**.
  The dwell is governed by the timers 10/18/22 s (Streak: 20/28/32 s) in `ProcessControl` [ReSA].
* **Derailing.** At speed above 1.0 the train derails when consecutive segments bend by more than about 5°
  (dot < 0.996) [ReSA ProcessControl]. Keep segment-to-segment bends below 5° where high speed is possible:
  spacing ≤ 0.087·R, for example ≤ 17 units at R = 200. Vanilla spacing is 1.4–256, with a mean of 20.
* Trains follow the node z exactly, with no ground collision, so node z must sit on our rail geometry. Train
  lighting is sampled from the collision under the nodes (`CTrainNode::GetLightingFromCollision`).

### What to ship
**(i) Our own Baku loop:**
* **tracks.dat** is one **closed** loop below 21 845 units. A real line that is not a loop can be closed with a
  parallel return track and hidden turning loops, or with a tunnel.
  * Nodes 5–10 units apart on curves and up to about 50 on straights. Follow `nodes_from_polyline`.
  * **Exactly 6 stations**, flagged in track order at platform centres and more than 100 units apart. With
    fewer, the remaining SA coordinates snap to arbitrary nodes and break the ascending order the station
    code expects. If there are fewer real stations, add plausible halts.
  * Map-zone level 0 or 1 at the stations (§b).
* **tracks3.dat** (trams, used only inside the SF weather rectangle): a dummy loop (`dummy_track`) unless a
  tram line is wanted in that rectangle.
* **tracks2.dat and tracks4.dat**: dummy loops, or extra mission-only lines.

**(ii) No trains at all, safely:** use `tracks.no_train_files()`.
* It produces four identical 8-node square loops (320×320) at (3740..4060, 3740..4060, z −100), outside the map.
  tracks.dat flags 6 of the nodes, so all hard-coded SA stations are overwritten and InitTrains' station lookup
  stays in range.
* No camera can come within 70–100 units, so nothing ever spawns.
* Also call `SWITCH_RANDOM_TRAINS 0` at script start, and never `CREATE_MISSION_TRAIN`.
* **Do not** delete the files, and do not ship 0-node files:
  * A missing file means `LoadFile` on garbage.
  * With N = 0, `rand() % NumTrackNodes[0]` in `DoTrainGenerationAndRemoval` is an integer division by zero,
    and `FindClosestTrackNode` returns an uninitialised index.
* CR ships 1-node files for tracks2 and tracks4 and a 20-node track 0 at z −80, which works because no train
  ever uses them. The 2-node minimum in the validator is deliberately stricter.

---

## f) Hard-wired SA things that misbehave on a new map

| System | What is hard-wired | Effect on Baku / what to do |
|---|---|---|
| Weather | `CWeather::FindWeatherRegion` (0x72A640): LV `x>1000 && y>910`; DESERT `-850<x<1000 && y>1280`; SF `x<-1430 && -580<y<1430`; LA `250<x<3000 && -3000<y<-850`; else DEFAULT. Each region has its own weather list [GR Weather.cpp]. | Different parts of our map get LA smog, SF fog, desert **sandstorms** and LV heat. Control the weather from script (`FORCE_WEATHER(_NOW)` 0x01B5/0x01B6) or accept it. The SF rectangle also enables trams (§e). |
| Outside-world sea | Ocean at z=0 is rendered and is "water" for \|x\| or \|y\| ≥ 3000 [§a]. | Always present. Close or raise the terrain on land edges. |
| Map level | map.zon level drives cops, bikes, ambulances and fire trucks (none at level 0), cab drivers, prices, restarts and station permission [§b]. | Ship map.zon with level 1 over the map. |
| Restart points | Only from script: `ADD_HOSPITAL_RESTART` 0x016C and `ADD_POLICE_RESTART` 0x016D, max 10 each, filtered by `STAT_CITY_UNLOCKED ≥ townId`. **With none, the police restart position is left uninitialised** [GR Restart.cpp]. | main.scm must add ≥ 1 hospital and ≥ 1 police restart with townId 0. Note `OverrideNextRestart`. |
| Stat 181 `STAT_CITY_UNLOCKED` | Used by restarts, train stations and radio DJ banter [GR Restart.cpp, AERadioTrackManager.cpp]. | Set it from script (`SET_INT_STAT` 0x0629, for example 181 = 4) if levels 2 or 3 are used. |
| Train stations | `CTrain::aStationCoors[6]` are the SA stations, unless tracks.dat flags replace them. Random-train speed is halved near (2222, −1750) [§e]. | Flag 6 stations, or use the dummy. |
| Cull zones | `data\maps\cull.ipl` and `tunnels.ipl` (IPL `cull`) hold SA boxes. Their attributes are cam-close-in, no rain, no police helis, **5-star military zone 0x1000**, police abandon cars, tunnel 0x800 (used by train generation and audio), fewer peds and cars, and mirrors [GR CullZones.h, FileLoader `LoadCullZone`]. | **Remove** them from gta.dat, or replace them with Baku zones (tunnels, metro, underpasses). |
| Occlusion | `occluSF/occluveg/occluLA/occluint.ipl` (OCCL) hide everything behind SA buildings. | **Remove** them. Add our own OCCL for big buildings if needed. |
| Audio zones | `audiozon.ipl` (AUZO): ambience boxes at SA places. | Remove or replace. |
| Other IPL sections | ENEX (entrances), GRGE (garages, max 50), PICK, CARS (parked car generators), JUMP (stunt jumps) and TCYC (timecycle boxes) all live in the SA map IPLs [GR LoadScene]. | They disappear with the SA IPLs. Re-create the ones we want in our IPLs. |
| Police scanner | Zone GXT label → scanner audio table (WIKI; not traced in code). | Our labels won't match, so the callouts probably lose the location (unverified). |
| Ped speech | `CGameLogic::FindCityClosestToPoint` uses fixed LS, SF and LV centres. Used for the gang-clothes speech check [GR GameLogic.cpp, AEPedSpeechAudioEntity.cpp]. | Cosmetic. |
| Radar | Airstrip table (plane landing indicator) [§d]. | Wrong airstrip arrows when flying. Cosmetic. |
| Sky | `CPlaneTrails::Update`: three distant plane lights at (2590,2200,550), (2000,−2600,500) and (2100,1300,600) [GR PlaneTrails.cpp]. | Cosmetic. |
| Random planes and helis | Spawned around the player (`CPlane::FindPlaneCreationCoors` 0x6CD090) [ReSA]. **SA has no flight\*.dat** (that was III/VC; WIKI). | Nothing to replace. |
| Emergency services | Spawned on the road network near the incident (`GenerateOneEmergencyServicesCar`), not at stations, and only if the level is not 0 [ReSA]. **No fire-station or hospital data.** | Needs good car path nodes. |
| Peds and cars | Need path nodes (`nodesNN.dat` in gta3.img, streamed by `CStreaming`), popcycle by zone info, and pedgrp/cargrp. Gang peds need zone gang strength from script. | Paths are a separate task. Set zone population types in main.scm. |
| Plants and procobj | Driven by collision **surface** type (plants.dat, procobj.dat, surfinfo.dat), not by position. | Keep vanilla. Grass and stones appear wherever our COL uses SA surfaces. |
| Savegame | Zones, zone infos, restarts and garages are saved [GR `CTheZones::Save`, `CRestart::Load`]. | New game after data changes. |

### gta.dat lines (parser: `CFileLoader::LoadLevel` 0x5B9030 [GR])
* **Keywords.**
  * `#` (comment), `EXIT`, `TEXDICTION`, `IPL`, `IMG` (except `MODELS\GTA_INT.IMG`, added elsewhere),
    `COLFILE <n> <path>`, `MODELFILE`, `HIERFILE` and `IDE`.
  * `COLFILE`'s level number is skipped: the path starts after the first two characters. `SPLASH` is unused.
* **Load order.** `DATA\DEFAULT.DAT` loads first, then gta.dat [GR `CGame::Initialise`].
  * The first `IPL` line triggers object.dat, the vehicle setup, `CStreaming::Init2` and the collision
    bounding boxes.
  * After the last line come `CIplStore::LoadAllRemainingIpls` and `CTrain::InitTrains`.
* For a total conversion:
  * Keep the `IMG` lines (carrec, script, cutscene) and the generic IDEs: vegepart, barriers, dynamic,
    dynamic2, multiobj, procobj, the interior props, veh_mods, txdcut.
  * Replace the city, country and leveldes IDEs and IPLs with ours.
  * Keep `IPL DATA\MAP.ZON` and `IPL DATA\INFO.ZON` before our IPLs.
  * Drop `leveldes\levelmap.ipl`, `seabed.ipl`, `paths*.ipl` (the legacy `path` sections are NOPs),
    `cull.ipl`, `tunnels.ipl`, `occlu*.ipl` and `audiozon.ipl`.
  * Keeping the SA interior IPLs (z ≈ 1000) is harmless but useless without ENEX links.

### Data files to replace for the total conversion
| File | Replace with |
|---|---|
| `data/water.dat` (+ `water1.dat` copy) | `tools/gta/water.py` output: sea and lakes within the limits above |
| `data/map.zon` | 1+ map zones, level 1 (`zones.map_zone`) |
| `data/info.zon` | Baku districts (`zones.navi_zone`), ≤ 379 zones and ≤ 379 names |
| zone texts | FXT (`zones.format_fxt`, including `SAN_AND`), or extend american.gxt with `build_gxt` |
| `data/paths/tracks.dat`, `tracks2.dat`, `tracks3.dat`, `tracks4.dat` | Our loop plus dummies, or `no_train_files()` |
| `gta3.img: radar00…radar143.txd` | `radar.split_radar_image` tiles, 128² DXT1 |
| `data/gta.dat` | Our IDE/IPL list; SA map, cull, occlusion and audio IPLs removed |
| `data/maps/**` IDE/IPL, COL, DFF/TXD in IMG, `nodesNN.dat` | Map generator (other modules) |
| `data/script/main.scm` (+ script.img) | Our script: restarts, zone population, `SWITCH_RANDOM_TRAINS`, weather, stat 181 |
| `data/popcycle.dat`, `pedgrp.dat`, `cargrp.dat` | Optional (vanilla works) |
| `data/timecyc.dat` | Optional (look and feel) |
| Keep | default.dat, object.dat, surfinfo.dat, procobj.dat, plants.dat, fonts.dat, handling, vehicles and peds |

---

## Writer APIs (all under `tools/gta/`, tests in `tests/test_data_files.py`)
* `water.py`:
  * `quad(x0,y0,x1,y1,z,…)` and `triangle(x,y,dx,dy,z,…)` (right angle at (x,y), |dx|=|dy|).
  * `format_water_dat`, `write_water_dat` and `parse_water_dat`.
  * `validate_water` returns `WaterStats`: quads, triangles, vertices, block-list words, warnings and errors.
  * `GameWater`: engine pools, block lists and `level_at`.
  * Helpers: `rects_from_mask` and `snap_even`.
* `zones.py`:
  * `navi_zone`, `map_zone`, `format_zon`, `write_zon` and `parse_zon`.
  * `validate_zone_file` and `validate_zones` (limits).
  * `zone_at` and `level_at` (engine lookups), and `POP_TYPES`.
  * Text: `to_sa_text`, `encode_sa`, `decode_sa`, `make_label`, `gxt_key_hash`, `format_fxt`, `parse_fxt`,
    `build_gxt` and `read_gxt`.
* `tracks.py`:
  * `TrackNode`, `nodes_from_polyline`, `resample_polyline`, `format_tracks_dat`, `write_tracks_dat` and
    `parse_tracks_dat`.
  * `validate_track` (size, length, segments, stations) and `game_distances`.
  * `dummy_track`, `no_train_files`, `TRACK_FILES` and `quantize`.
* `radar.py`:
  * `split_radar_image(img, 128)` returns `[(txd, tex, PIL.Image)]` × 144; `assemble_radar` does the reverse.
  * `tile_name`, `tile_world_rect`, `world_to_tile`, `world_to_pixel`, `SEA_RGB`.
  * `build_radar_txds`, through `tools.rw.txd`, with DXT1 and no mipmaps.

## Uncertainties
1. **Functions taken from ReSA only.** gta-reversed has not reversed `WaterLevelInitialise` parsing,
   `FillQuadsAndTrianglesList`, `GetWaterLevelNoWaves`, the high-detail renderers, `ReadAndInterpretTrackFile`,
   `DoTrainGenerationAndRemoval` or `GenerateEmergencyServicesCar`. These rules rest on the ReSA
   transcriptions, cross-checked against:
   * EUR, for the water parser;
   * MTA, for the water pools, block-list algorithm and outside-world z;
   * the Ghidra pseudocode, for the track reader;
   * the vanilla data, which fits exactly.
2. **The even-coordinate and 45° crash.** It is derived from the code, and vanilla, MTA and CR all agree with
   it. It was not reproduced in game.
3. **Radar raster.** DXT1, one mip level and the filter/addressing flags come from third-party tools, not
   from a vanilla TXD dump. The game draws any raster format.
4. **SA font glyphs for 0x80–0xAF.** They are assumed present in PC `fonts.txd`, which serves 5 languages;
   not inspected.
5. **Police scanner zone table.** Its behaviour with unknown labels is untested.
6. **Original sscanf buffer sizes in `LoadZone` (24/12).** These are gta-reversed's reconstruction. Keeping
   labels at 7 characters or fewer is safe either way.
