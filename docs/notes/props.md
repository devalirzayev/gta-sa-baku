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
* A stream IPL is required while the player is inside its bounding box inflated by 200 units
  (`CIplStore::LoadIpl`, 350 for "large" IPLs) → at most a 2 × 2 block of our 500-unit cells.
* Vanilla densities (measured over all vanilla exterior IPLs): max 1537 dynamic instances in a
  1 km block, max 144 within 100 units, p99 82.
* Our budgets (props.py `apply_budgets`): ≤ 1450 dynamic per 2 × 2 block, ≤ 120 within 100 units,
  ≤ 2700 static props per block.  `tools/validate.py` re-checks dummies, double-link nodes,
  objects within 100 units and the building pool from the packaged IPLs.

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
  one child; with several children the child gets draw distance 400 and the LOD keeps none.
  Vanilla: 246 of 4339 LOD models have their own COL entries (the multi-child ones).
* Therefore every custom model has a COL3 entry: real collision for ground tiles, road signs and
  landmarks, bounds-only entries (no volumes → `SetUsesCollision(false)`) for the ground LOD
  tiles, landmark LODs and the merged signage tiles.  validate.py checks every placed instance.
* `CColStore::LoadAllBoundingBoxes` runs at the first `IPL` line of gta.dat, so col bounds exist
  before the text IPLs are loaded; object.dat is read at the same moment (after all IDEs).

## 5. object.dat (breakable road signs)

* `CObjectData::Initialise` (ObjectData.cpp 0x5B5360) stops at the first line starting with `*`
  (vanilla has `*********melee weapons*****` before the end), skips `;` lines, looks models up by
  name, and reuses an existing slot for an identical parameter set (160 slots).
* Our 14 `bk_rs_*` lines copy vanilla `bussign1` exactly → they share its slot.  The installers
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
