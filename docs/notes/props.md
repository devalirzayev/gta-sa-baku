# Props, signage, landmarks, parked cars — engine facts and conventions

Sources: gta-reversed (GR, `source/game_sa/…`, master, fetched 2026-11-08) and the vanilla
placements (text + binary IPLs converted to text by `Fernando-A-Rocha/mta-binary-ipl-to-text`,
web-gta IPLs) compared with the vanilla car path navis (`ziggi/FCNPC` NODES*.DAT, verified against
the 1.0 hashes, see paths.md).

## 1. Dynamic objects and their budgets

* `CFileLoader::LoadObjectInstance` (GR FileLoader.cpp 0x538090): a model with an object.dat entry
  (`m_nObjectInfoIndex != -1`) becomes a `CDummyObject`, every other model a `CBuilding`.
* Pools (GR Pools.cpp): buildings 13000, dummies 2500, objects 350, PtrNode single 70000,
  PtrNode double 3200.  `CSector` keeps buildings in a single-link list and dummies in a
  **double-link** list (GR Sector.h); repeat sectors (vehicles, peds, objects) are double-link
  too.  BIG buildings go into the 200-unit LOD sector lists.
* `CPopulation::ManageDummy` (0x616000) turns dummies within `FindDummyDistForModel` = 80 units
  (750 for SAM sites) into `CObject`s; when the object pool is full they stay dummies.
* A stream IPL is loaded while the player is inside its bounding box: the union of the
  instances' bound rects (`CEntity::GetBoundRect`, the col bounding box corners), inflated by
  350 units when one model has a draw distance > 150, else 200 (`CIplStore::LoadIpl`,
  `LoadIplBoundingBox`, `SetIfInteriorIplIsRequired`, `LoadIpls`; vehicles look ahead ≤ 40 units).
  All our cells are "large" → up to a **3 × 3 window** of 500-unit cells is loaded at once.
* Vanilla, same simulation over its 158 exterior stream IPLs: peak 1616 loaded dynamic
  instances (1757 sector nodes), 1958 static, 128 car generators; max 144 dynamic within 100 units.
* Our budgets (props.py `apply_budgets`): ≤ 1650 dynamic per 3 × 3 window, ≤ 120 within 100
  units, ≤ 4200 static props per window, ≤ 20 trees per 50-unit sector (visible lists, §8);
  parking.py ≤ 440 car generators per window.  `tools/validate.py` simulates the loading exactly (the maximum of a
  sum of rectangle indicators is reached at a pair of left / bottom edges) and checks the
  building pool, dummies, double-link nodes (exact sector counts), car generators and the
  single-link nodes from the packaged IPLs.
* `CFileLoader::LoadScene` keeps every text IPL instance plus every LOD-linked instance of its
  related stream IPLs in `gpLoadedBuildings[4096]` (no bounds check), and every text IPL with
  instances takes one of `IplEntityIndexArrays[40]` (ours 23 + 5 kept vanilla).  Both checked.

## 2. Model axes measured from the vanilla placements

`docs/notes` scratch scripts compared every vanilla instance with the nearest vanilla car navi:

| model | finding |
|---|---|
| CJ_TRAFFIC_LIGHT4 (1350), trafficlight1 (1315), CJ_TRAFFIC_LIGHT3/5, vgsstriptlights1 | forward (+y) axis along the controlled travel direction in 95–98 %, the road always on the local −x side → lamps face −y, mast arm along −x, pole on the right kerb |
| mlamppost (1294), lamppost1 (1297), lamppost3 (1226) | road on the local −x side in 94–96 % → arm along −x |
| lamppost2 (1290) | road on ±x → double arm |
| bustopm (1257), bussign1 (1229) | road on −x (78 %, 70 %) |
| MTraffic1/2 | ambiguous facing (lights on both sides) — not used |

IPL rotation: quaternion (0, 0, −sin(h/2), cos(h/2)) for a CCW heading h; the engine reads
`acos(w) * (z < 0 ? 2 : −2)` (ipl.py).  Local +x = (cos h, sin h), local +y = (−sin h, cos h).

## 3. Traffic lights: AI phase = visible phase

* `CTrafficLights::FindTrafficLightTypeFromOrientation` (0x49D4D0): heading of the object's
  forward axis 60–150° or 240–330° → group 1 (`LightForCars1`), otherwise group 2.
* build_paths.py gives every light link the group of `light_group_for_dir(travel direction)`;
  props.py places each mast with its forward axis exactly along that direction, and raises an
  error if the two ever disagree.  Signals are set per layout junction (all member nodes of a
  dual-carriageway junction), with no lights on the links inside a junction.

## 4. Collision models are mandatory

* `CEntity::GetBoundRect` (Entity.cpp 0x534120) dereferences the model's `CColModel`; `CWorld::Add`
  calls it for every IPL instance.
* `LinkLods` (FileLoader.cpp 0x5B51E0) hands a LOD the child's collision only when it has exactly
  one child; with several children a child that owns its col model (only BIG buildings do) gets
  draw distance 400, every other child is unlinked from the LOD (both drawn → z-fighting).
  Vanilla: 246 of 4339 LOD models have their own COL entries (the multi-child ones).
  → every LOD of ours has exactly one child (ground LODs `bk_lXXYY` are per 250-unit tile, the
  texture is shared by the four tiles of a 500-unit cell); validate.py checks it.
* BIG building = LOD children or `m_fLODDistMultiplier * dd > 300`; the multiplier is 1.0 while
  the level loads (TheCamera.Init, CCamera::Process runs later), so HD tiles with dd < 300 keep
  their collision at every draw distance setting.
* `CCollision::ProcessColModels` gathers the triangles touching the other model's bounding
  sphere in static `collTrisB[600]`; with face groups the inner `break` does not stop the next
  group from writing past the array.  Our collision is a coarse twin of the visual mesh (ground
  grid 18, simplified kerbs, decimated decks, single-sided walls): exact sphere–triangle count
  ≤ 230 within 12 units, ≤ ~470 within 20 (validate.py: error > 560 at 12 units, warning at 20).
* Therefore every custom model has a COL3 entry: real collision for ground tiles, road signs and
  landmarks, bounds-only entries (no volumes → `SetUsesCollision(false)`) for the ground LOD
  tiles, landmark LODs and the merged signage tiles.  validate.py checks every placed instance.
* `CColStore::LoadAllBoundingBoxes` runs at the first `IPL` line of gta.dat, so col bounds exist
  before the text IPLs are loaded; object.dat is read at the same moment (after all IDEs).

## 5. object.dat (breakable road signs)

* `CObjectData::Initialise` (ObjectData.cpp 0x5B5360) stops at the first line starting with `*`
  (vanilla has `*********melee weapons*****` before the end), skips `;` lines, looks models up by
  name, and reuses an existing slot for an identical parameter set (160 slots).
* Our 14 `bk_rs_*` lines copy vanilla `noparkingsign1` exactly (damage effect 0: the sign is
  knocked over; effect 200 = breakable needs a Breakable plugin in the DFF, without it
  `BreakObject_c::Init` fails and the object vanishes) → they share its slot.  The installers
  insert them before the first `*` line and restore the original on uninstall.

## 6. Car generators (parked cars)

* 500 generators (`CTheCarGenerators::CarGeneratorArray`); a binary stream IPL creates its own
  when it loads and `CIplStore::RemoveIpl` removes them (`RemoveCarGenerators(iplId)`).
* `tBinaryIplFile` (0x4C header): counts inst, u1, u2, u3, cars, u4; then (offset, size) pairs in
  the same order.  `CFileCarGenerator` 0x30: pos, angle (radians, converted with
  `RadiansToDegrees`), model (−1 = random from the zone), colours, flags (bit 0 force spawn,
  bit 1 ignore the parked-car population limit of 5 by day / 10 at night), alarm %, lock %, delays.
* IPL generators are switched on (`SwitchOn`); a car appears when the player is 140–160 units
  away (× generation multiplier); the ground is found with a vertical line from z + 1.

## 7. Signage and landmarks

* Road signs: 14 models (`bk_rs_*`, shared TXD `bk_rsign`), plate faces −y like the traffic
  lights, pivot at mid height like vanilla bussign1.
* Shop boards / street plates / zebra crossings: one merged static model per 250-unit tile
  (`bk_sgXXYY`, dd 290 < 300 so it stays a normal building).  Board text is rendered with the
  vendored fonts in `tools/tex/fonts` (Azerbaijani ə ğ ı ş supported; `az_upper` handles İ/I).
* Landmarks: HD model (dd 299, collision) + LOD (dd 1800).  Heights use LANDMARK_SCALE 0.75.
  SOCAR Tower, the TV tower and Baku Eye lie outside the ±3000 world (x = 3309, y = −3462,
  y = −3227) and are not built.

## 8. Rendering lists and streaming memory

* `CRenderer::AddEntityToRenderList` fills `ms_aVisibleEntityPtrs[1000]` / `ms_aVisibleLodPtrs[1000]`
  without a bounds check in the release game (only `assert`s in gta-reversed).  An entity is
  added when its bounding sphere is on screen and it lies within
  `min(m_fLODDistMultiplier × dd, bound radius + far clip)`; the multiplier is
  `70 / FOV × ms_lodDistScale`, up to 1.8 at the maximum draw distance setting
  (Camera.cpp, `SetupMapEntityVisibility`).
* validate.py counts, for camera positions every 50 units and headings every 5°, the map
  entities inside a 90° horizontal field of view at multiplier 1.8: vanilla 259 / 642 LODs, ours
  484 / 247 (the densest park, after the tree cap of props.md §1 and without the dd-299 tree
  `tree_hipoly19`).  Error above 820, warning above 650.
* Streaming memory is fixed at 50 MB (`CStreaming::Init2`); when it is full the streamer evicts
  the least used models (`MakeSpaceFor`, `DeleteRwObjectsBehindCamera`), it does not fail.  The
  heaviest custom files: HD ground tiles 400 KB average (1 MB max), the shared ground TXD
  3.8 MB, sign-tile atlases capped at 1024 × 1024 DXT1 (683 KB with mip maps; dense tiles are
  rendered at a lower pixel density), LOD tiles 16 KB + one 45 KB TXD per 2 × 2 LOD tiles.

## 9. Kept vanilla interiors

* The interior IPLs stay in gta.dat (KEEP_IPL in package.py) for the interior models, but their
  `enex` sections place entrance markers at San Andreas coordinates, i.e. at random spots in
  Baku.  The installers remove the `enex` sections of the files listed in
  `text/strip_enex.txt` (backup in `baku_backup/enex`, restored on uninstall).
