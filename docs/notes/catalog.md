# SA model catalog (`data/sa/`) — sources, method, caveats, IDE keep/drop

Built by `tools/catalog/build_catalog.py` (stdlib only, cached downloads, deterministic).
State of the data: 2026-11-08.

| File | Rows | Content |
|---|---|---|
| `data/sa/gta_dat_vanilla.txt` | 146 lines | vanilla `data/gta.dat`, byte copy (CRLF) of the majority version |
| `data/sa/models.csv` | 14 832 | every model id defined by the vanilla IDEs of `default.dat` + `gta.dat` (incl. 276 peds, 212 vehicles, 50 weapons with a `type` column) |
| `data/sa/ide_summary.csv` | 57 | per IDE (3 from default.dat, 54 from gta.dat): slot usage, LODs, placements, candidates, engine dependencies, recommendation |
| `data/sa/building_candidates.csv` | 1 253 | SA building models usable for Baku footprints, with `style`, `ground_risk`, LOD info |

Reproduce: `python3 -I tools/catalog/build_catalog.py` (cache default:
`/tmp/claude-0/-home-user-ila/5b6b71aa-4af7-540b-9036-4dec5b516689/scratchpad/catalog_cache/`;
`--offline` to forbid network, `--report FILE` writes the verification log, `--scan-gtarev`
re-derives the hard-coded model-id list, see below). A cold run downloads ~715 files from
raw.githubusercontent.com plus 319 prineside pages at 0.6 s spacing (≈5 min); a warm run makes
no requests. Verified: a run into an empty cache (1 034 downloads) produced byte-identical outputs.

## 1. Sources (pinned) and how they were verified

| What | Source | Verification |
|---|---|---|
| gta.dat | gtastories/gta@ecfea467, Sir-Teo/web-gta@77ce6581, Alf21/samp@8c174cd4, Google61/gta3sa.app@67f70cb7, CyberSys/SA-MP@318bcd63, Just-Archives/SA-MP@b7bc4017, xdatsec/GTA_2.11.311@c7266cf5 | git blob SHA of every download checked against GitHub code search; **all 7 identical** after CRLF normalisation. Control copy dashr9230/SA-MP differs (SA-MP lines `IMG SAMP\SAMPCOL.IMG`, `IDE SAMP\samp.IDE`, `IPL SAMP\samp.ipl`). md5 CRLF `2d2e4f7f05e2d82b25c88707096d3393`, LF `285670be664bc3d4a47606ae4bd9b9fd`. |
| default.dat | 5 copies (same repos) | identical (loads `DEFAULT.IDE`, `VEHICLES.IDE`, `PEDS.IDE`) |
| IDE files (57) | gtastories/gta (primary), Sir-Teo/web-gta, gta191977649/sa_object_finder `data/all_ide` | 53 files byte-identical (normalised) in all 3 repos, default/peds/vehicles in 2; only `TXDCUT.IDE` (txdp only, 0 slots) is single-source |
| text IPLs (49 incl. cull/occlusion/audio-zone/path files) | gtastories/gta, Sir-Teo/web-gta, Fernando-A-Rocha/mta-binary-ipl-to-text@a234eb28 `other_stuff/normal_ipls` | all 49 identical in the 3 repos |
| binary stream IPLs (190 in gta3.img) | Fernando-A-Rocha/mta-binary-ipl-to-text `output/*.ipl` (primary), gta191977649/sa_object_finder@b7831cac `data/all/*.ipl` | two independent text conversions agree for all 190 files (id, flags, LOD index exact; pos/rot within 0.01); 41 667 instances. Internal check: all 6 103 LOD references of the 50 935 placements resolve, every placed id is defined, 4 082 of the 4 292 LOD targets are named `lod*` (209 more contain "lod" inside the name, 1 is `desN_baitshop`). |
| collision bounds | crayder/model-sizes-plus `modelsizes.inc` (ColAndreas export of the game's COL data) | sha256 pinned (`ca7a03d5…`); 12 074 of 14 294 object models have bounds |
| names/ids | gta-reversed `Enums/eModelID.h` (fastman92 enum from unmodified data) | 14 819 / 14 832 IDE (id, name) pairs match `MODEL_<NAME>`; the 13 others are aliases (`clothes01` ×10 → `MODEL_CLOTHES01_IDxxx`, id 0 `null`=`MODEL_PLAYER`, 400 `MODEL_VEHICLE_FIRST`, 134 `SBMOTR2`). Enum ids 374-381 are runtime `MODEL_TEMPCOL_*` without IDE lines. |
| engine name look-ups | gta-reversed `ModelIndices.cpp` `MatchAllModelStrings` | 157 names, 138 exist in vanilla IDEs |
| engine numeric ids | gta-reversed@afbc26d7, all 375 files containing `MODEL` (code search) | only 10 object ids outside peds/vehicles/weapons: 1, 300, 319, 384, 394-397 (default.ide), 630 `veg_palmkb8` (vegepart), 1211 `fire_hydrant` (dynamic) |
| data files naming models | procobj.dat, object.dat, carmods.dat (gtastories + web-gta, identical) | 56 / 995 / 190 names |
| categories, titles, DFF-like sizes | dev.prineside.com category pages (44 categories, 319 pages, ≤1.7 req/s) | 9 652 vanilla ids categorised; 0 name mismatches vs IDEs |

Engine code facts used (gta-reversed): `CFileLoader::LoadObject` allocates from
`CDamageAtomicModelInfo` (70) when IDE flag 0x1000 is set, otherwise from the atomic store
(14 000); `tobj` → time store (169); `anim` and `hier` → clump store (92); `LoadLine` turns
`,` and control chars into blanks and `#` is a comment only at line start;
`CIplStore::SetupRelatedIpls` links `<text ipl>_stream*` by case-insensitive prefix and the
binary LOD index points into the text IPL's `inst` list; `LoadObjectInstance` returns
nullptr for an undefined model id.

## 2. Vanilla slot usage (default.dat + gta.dat)

| Store | Used / capacity |
|---|---|
| atomic (`objs`) | **13 976 / 14 000** (24 free) |
| damage atomic (`objs` + flag 0x1000) | 69 / 70 |
| time (`tobj`) | 160 / 169 |
| clump (`anim`, `hier`) | 89 / 92 |
| weapon / vehicle / ped | 50/51, 212/212, 276/278 |

Notes: no id is defined twice. `dynamic2.IDE:164` (`1489 DYN_SALE_POST BREAK_STREET2 1 0`)
is malformed but accepted by the engine (`LoadObject` only VERIFYs ≥5 values for mesh
count 1) → it is counted (draw distance 0). `leveldes.ide`, `data/maps/txd.ide`,
`leveldes.ipl`, `vegaxref.ipl`, `occlu.ipl` exist in the game folder but are not referenced
by gta.dat. `paths*.ipl`/`occluSF.ipl` from gta.dat are not in any repo (no inst data).

## 3. Method

### models.csv
* One row per defined id (last definition wins; none are duplicated). `type` = store used
  (`atomic`, `damage_atomic`, `time`, `clump`, `weapon`, `vehicle`, `ped`), `section` = IDE
  section, `ide_file` exactly as written in gta.dat/default.dat.
* `flags` (int) + `flag_names` (gtamods SA flag table), `timed`/`time_on`/`time_off` for tobj,
  `txd_parent` from txdp.
* `aabb_*`, `sphere_*` = collision bounds (model space). Derived `footprint_x/y`, `height`,
  `z_min`. `has_col_bounds=0` for 2 220 models without collision (decals, alpha/night layers,
  small props, vehicle parts, many interior items); 1 914 of those have prineside sizes
  `ps_width` (= x), `ps_length` (= y), `ps_height`, `ps_radius`. prineside sizes look
  DFF-based: they agree with the COL box (2 %/5 cm) for 5 409 of 7 738 models with both;
  big differences are trees/plants (COL = trunk only) and models with non-colliding parts.
* Placements: all `inst` lines of the gta.dat text IPLs (30 of its 52 `IPL` lines carry
  inst data: 9 268 instances) + the 190 binary IPLs (41 667)
  (`num_placements_text/stream/scripted`; *scripted* = barriers1/2, carter, crack,
  truthsfarm: no text IPL, switched on/off by the mission script). `placement_ipls` = top 4.
  `placement_region` = majority IPL group, `placed_in_interior_share` = share with area code ≠ 0.
* LODs: for every placement with LOD index ≥ 0 the LOD instance is resolved (same text IPL,
  or the related text IPL for `_streamN`). `lod_model_id` = majority LOD model,
  `lod_share` = share of this model's placements using it, `lod_alternatives` = number of
  other LOD models seen. `lod_same_pivot_share` = LOD instance at the same position/rotation,
  `lod_exclusive` = the LOD serves only this HD model, `lod_reusable` = both (≥0.9) → the LOD
  can be placed with the HD model anywhere (3 955 of 4 300 HD models with a LOD). For LOD
  models: `lod_parent_of` (HD ids using it, most frequent first), `lod_n_children`,
  `lod_target_uses`. `is_lod` = used as a LOD target **or** name starts with `lod`
  (4 350; 62 `lod*`-named models are never used as LOD).
* `companions` = other non-LOD models placed at exactly the same pivot (≤1 cm) in ≥50 % of
  this model's placements (alpha layers, night-window `tobj`, details) — place them together.
* `engine_refs`: `MI` (looked up by name in `MatchAllModelStrings`), `procobj.dat`,
  `carmods.dat`, `object.dat`, `hardcoded(<file>)`.
* `region` by IDE folder: LA / SF / LV(`vegas`) / country / interior (`int_*`, `gen_int*`,
  `savehous`, `stadint`, `props`, `props2` (furniture, only placed inside interiors),
  `levelmap` (Liberty City set)) / generic (`generic/*`, `propext`, `levelxre`, default.ide,
  txdcut) / `vehicle_upgrade` (veh_mods) / `vehicle` / `ped`. `seabed` → country.
* `prineside_category` = all scraped categories containing the id (parent;leaf),
  `prineside_title` = their human description (often empty).
* `emodelid_name_ok` = (id, name) matches gta-reversed `eModelID.h`;
  `ide_malformed_line` marks the one engine-tolerated malformed IDE line (id 1489).

### building_candidates.csv
Pool: `atomic`/`time` models of LA/SF/LV/country IDEs. Rejected (counts from the run):
LOD 4 316, terrain/road/sea/rail prineside category 2 028, prop categories (trees, lights,
street items, signs, fences, wires, alpha, crates, …) 2 002, IDE flag IS_ROAD 647, not a
building by category+name 236, placed in interiors 87, too small (<5 m long, <2.5 m wide or
<3 m high) 67, slender structures 28, overlays 13 (`alpha|shad|night|neon|sign|glass|…`,
`…Tr_` transparent parts), sports grounds 12, graveyard 4, piers 4, terrain-like without a
building category 59, and **branding/landmarks 182**: Burger Shot (`burgsht`, `boiga…`),
Cluckin' Bell, Well Stacked/Zip pizza, Jim's Sticky Ring, 24/7/Supa Save, Ammu-Nation
(`ammu|amun|gunclub`), Binco/ProLaps/Victim/ZIP/SubUrban/Didier Sachs, Pay'n'Spray,
mod garages (Transfender/Loco Low/WAA, `modshop`, Xenon/Xoomer fuel), tattoo/barber/sex
shops, casinos and LV strip themes (Caligula's `csrs`, Four Dragons `triad`, Come-a-Lot
`excal`, Camel's Toe `pyrmd/luxor/sphinx`, Pirates `pirat`, V-Rock, Royal, Clown's Pocket,
Visage, Starfish, Emerald Isle, Tiki, circus, Fremont…), bars/clubs (Pig Pen, Alhambra,
strip/night clubs, saloons), Chinatown (Chinese signage), landmarks (LS/SF city hall,
Library Tower, Pershing Sq., LA Forum, Vinewood, Ferris wheel, lighthouses, cathedral,
Big Pointy, Sherman Dam, Area 69, Big Ear, TV/film studios, drive-in, cinemas, driving/bike
schools, wedding chapels, Griffith observatory, Ferry Building). Regexes are in
`BRANDING_PATTERNS`; they test model name + prineside title, and the TXD name for shop
brands (shared branded TXDs).

`style` (rules in `_style_and_use`, inputs: prineside building sub-category → `use`,
name/TXD tokens, collision size; `floors_est = round((height − 1.5)/3.3)`):
`detached_house` (residential, ≤32×26 m, <12 m), `rowhouse/terrace` (names row/terrace/
Victorian/barrio/house-block or long narrow low footprints), `low_apartment` (<19 m ≈ 2–5
floors), `mid_apartment` (19–36 m ≈ 6–10), `tower` (≥36 m ≈ 11+, only residential/office/
mixed/hotel use), `office` (office use <36 m), `shop/retail` (stores, restaurants, hotels
<15 m; taller ones become apartment/tower styles with use `mixed_*`), `industrial/warehouse`,
`garage` (garages, lock-ups, car ports, multi-storey car parks; petrol stations → `misc`
use `fuel_station`), `church/religious`, `stadium` (≥70 m), `misc` (civic: hospital,
police, station, school…, unknown). `use`, `style_reason`, `block_like` (both sides ≥45 m:
probably several buildings / a city block), `aspect`, `footprint_long/short` help matching.

`ground_risk` (score → low <2, medium 2–3, high ≥4; reasons in `ground_risk_reasons`):
terrain words in name/TXD (+3 land/grnd/ground/hill/cliff/beach/road/plaza, +2 others such
as sand, rock, grass, lawn, park(ing), bit, pier, dock, rail, track, golf), weak words +1
(park, yard, plot, block, base, stuff…), `lod` in the name +2, terrain prineside categories
+3, prineside titles mentioning landscape/plot/driveway/yard/garden/ground/road +2,
footprint ≥150 m +3 (≥90 m +1), flat shapes (h/len <0.12 with len ≥40 m +2, <0.2 with len ≥25
m +1), area ≥8 000 m² +1, SF residential/mixed blocks ≥19 m high and ≥40 m long +2 (hill
slopes), and `z_min` far below the pivot (−z_min ≥ 0.7·height +1, ≥ height +2). A negative
`z_min` alone is normal: SA map models mostly have the pivot at the box centre
(`pivot_height_share` = −z_min/height, median 0.5), so the instance z must be
`ground_z − z_min` (+ a little burial). Result: low 746, medium 382, high 125.

Candidates by style (all / low ground risk):

| style | all | low risk |
|---|---|---|
| detached_house | 154 | 128 |
| rowhouse/terrace | 16 | 12 |
| low_apartment | 178 | 105 |
| mid_apartment | 141 | 42 |
| tower | 146 | 49 |
| office | 55 | 39 |
| shop/retail | 239 | 145 |
| industrial/warehouse | 185 | 137 |
| garage | 23 | 15 |
| church/religious | 6 | 5 |
| stadium | 6 | 0 |
| misc | 104 | 69 |
| **total** | **1 253** | **746** |

Rows also carry the LOD to place with the model (`lod_model_id`, `lod_ide_file`,
`lod_same_pivot_share`, `lod_reusable` — 1 012 of the 1 121 candidates with a LOD have a
reusable one), `companions`, vanilla placements, the collision AABB, prineside
category/title and `preview_url`. Sorted by style, risk, then footprint area.
`preview_url` links the prineside model page (the image host files.prineside.com was not
reachable from here, so no model was checked visually).

### ide_summary.csv
Per IDE: section counts (`n_objs` … `n_2dfx`, `n_txdp`), slots per store, id range, LOD
count, placed models, vanilla placements, candidates (+low risk, per style), generic props
(prineside trees/plants/lights/street items/signs/fences/trash/rocks),
`engine_name_lookups` (+ `engine_name_models` id:name list), procobj/carmods/object.dat
models, hard-coded ids, `critical` (`yes` = default.dat / hard-coded id / carmods.dat (not
null-checked in `LoadVehicleUpgrades`) / procobj.dat (`ProcSurfaceInfo_c::Init` returns
without a valid model index and `AddObjects` later dereferences it → likely crash when that
surface is near); `soft` = only `MatchAllModelStrings` names;
`no`), `recommendation`, `role_note`.

## 4. Recommendation for the Baku conversion

Generic props are almost all in the generic IDEs: lamps (`lamppost1-3`, `mlamppost`,
`Streetlamp1/2`), traffic lights (`trafficlight1`, `MTraffic1/2/4`, `CJ_TRAFFIC_LIGHT3-5`),
bus stop `BUSTOPM`, `parkbench1`, `telgrphpole02`, bins, hydrants, phone booths, barriers
are in `dynamic`/`dynamic2`/`barriers`; 150 of 174 trees and most plants in `vegepart`.
Only `vegaslampost`/`vgsstriptlights1` (vegaxref) and `gay_lamppost`/`gay_traffic_light`
(SFxref) live in xref files.

### Option A — keep whole IDE files (only change gta.dat `IDE` lines)
Greedy selection (in `recommend()`): candidates weighted by ground risk (1 / 0.5 / 0.1),
per-style targets (`STYLE_TARGETS`, apartments/towers/houses weighted ×2), gain per atomic
slot, city budget `CITY_SLOT_BUDGET = 4 500`.

* **Keep — required/generic (1 216 atomic):** `DEFAULT.IDE`, `VEHICLES.IDE`, `PEDS.IDE`
  (default.dat), `generic\vegepart`, `generic\barriers`, `generic\dynamic`,
  `generic\dynamic2`, `generic\multiobj`, `generic\procobj`, `interior\propext`,
  `veh_mods\veh_mods` (carmods.dat), `TXDCUT.IDE` (0 slots) and `leveldes\levelxre`
  (237 atomic + 14 clump: mission props, but it also holds 19 models the exe looks up by
  name — `para_pack` (parachute backpack), `kmb_atm3` (ATM), crane containers/magnets,
  wrecking ball, harvester body parts, pool cue ball, basketball; dropping it is possible
  only if those features are cut or the 19 lines are copied, see Option B).
* **Keep — buildings (16 city IDEs, 4 366 atomic):** LA: `LAxref`, `LAw`, `LAn2`, `LAw2`,
  `LAn`, `LAe`, `LAwn`; SF: `SFxref`, `SFe`, `SFn`, `SFw`; LV: `vegaxref`, `vegasS`;
  country: `counxref`, `countryN`, `countryW`. → 707 candidates (421 low risk): detached 127,
  row 12, low apt 114, mid apt 88, tower 117, office 34, shop 73, industrial 70, garage 14,
  church 3, stadium 3, misc 52. The LOD of all 635 LOD-using kept candidates is in a kept IDE.
  The four `*xref` files come first: they hold the re-used generic houses/blocks of each
  city at low slot cost.
* **Drop:** LA `LAs`, `LAs2`, `LAe2`, `LAhills`; SF `SFs`, `SFse`; LV `vegasN`, `vegasE`,
  `vegasW`; country `countN2`, `countryS`, `countryE`; all interiors (`int_LA`, `int_SF`,
  `int_veg`, `int_cont`, `gen_int1-5`, `gen_intb`, `savehous`, `stadint`, `props`,
  `props2`); `leveldes\levelmap` (Liberty City set), `leveldes\seabed`. Exe name look-ups
  lost by this: `quarry_cranearm`, `bonyrd_windsock` (countN2), `punchbagnu` (props),
  `ab_carcass` (props2) — mission/minigame only.
* **Result: atomic 5 582 / 14 000 used → 8 418 free** (time 70/169, clump 59/92, damage
  69/70). 13 682 model ids < 20 000 are then unused (largest gaps 11682-16999,
  17475-19999, 6526-7977, 1699-2884, 10311-11419).

Other budgets (same greedy order, nested sets; required/generic core 1 216 included):

| city budget | city IDEs kept | atomic used | **free** | candidates (low risk) |
|---|---|---|---|---|
| 0 (generic only) | – | 1 216 | 12 784 | 0 |
| 1 500 | LAxref, SFxref, counxref, vegaxref, LAw, SFe, LAn2 | 2 669 | 11 331 | 330 (209) |
| 3 000 | + SFn, vegasS, LAw2, LAn, countryN | 4 106 | 9 894 | 502 (340) |
| **4 500** (default) | + SFw, countryW, LAe, LAwn | **5 582** | **8 418** | 707 (421) |
| 6 000 | + countryS, SFs, SFse | 7 104 | 6 896 | 847 (504) |
| 8 000 | + LAs, LAe2, vegasW (targets saturated) | 8 408 | 5 592 | 968 (556) |

### Option B (preferred) — project IDE with only the SA models we place
IDE files are plain text and DFF/TXD/COL are matched by *name* in gta3.img
(PLAN: COL entries of unknown models are skipped; `LoadObjectInstance` ignores undefined
ids), so the generator can write one IDE that copies the vanilla lines (same id, name, txd,
draw distance, flags) of exactly the models it places, plus their `lod_model_id` and
`companions`, instead of keeping whole SA city files. Keep the generic core files (979
atomic without levelxre) and copy the 23 exe-looked-up models that live in otherwise
dropped files (`ide_summary.engine_name_models`: levelxre 19, countN2 2, props 1,
props2 1). Even using **all** 1 253 candidates + 1 117 LODs + 52 companions costs 2 422
slots (2 401 atomic + 21 time) → 14 000 − 979 − 23 − 2 401 ≈ **10 600 free**; a realistic
selection of ~500 buildings (+LODs, companions ≈ 1 000 slots) leaves ≈ 12 000 free.

## 5. Caveats / uncertainties
* Sizes are **collision** boxes. Visual meshes can be larger (trees, awnings, antennas,
  non-colliding parts); 2 220 models have no collision (use `ps_*`). prineside sizes are
  probably DFF bounds (not documented).
* Heights include hill slopes and foundations (SF and LA hills especially): `floors_est`
  and the low/mid/tower split are rough; many SF "towers" are hillside Victorian blocks
  (`block_like`, `ground_risk` reasons flag most of them). Many SA "buildings" are whole
  city blocks with pavement; `ground_risk` is a heuristic from names, categories and shape,
  not from mesh analysis — check `preview_url` before relying on a model.
* Style/branding are heuristic: prineside categories are community-curated; branding is
  detected from names/TXD names/titles, not from textures, so generic SA shop signs
  (fake brands on strip malls, billboards baked into facades) can remain. Some excluded
  landmarks are a judgement call (stadiums were kept as the `stadium` style).
* `MatchAllModelStrings`: in gta-reversed a missing name yields index 0
  (`GetModelInfoUInt16` initialises 0); the original exe's behaviour for missing names was
  not verified. Vanilla already has 19 missing names (e.g. `newsstand1`, `doc_crane_cab0`,
  `2player`) and runs, but compare with the list above before dropping more.
* The hard-coded id scan only covers code reversed in gta-reversed (functions still
  calling the original via `plugin::Call` are not visible) and only `MODEL_*` names, not raw
  numbers; it is a strong indication, not proof.
* Binary IPL data comes from third-party conversions (two tools agree), not from gta3.img
  directly. TXDCUT.IDE has a single source (txdp only).
* LOD "majority" ignores placements without LOD; `lod_reusable` assumes the vanilla
  relative transform (same pivot) — LODs at another pivot are merged/area LODs.
* prineside pages were scraped on 2026-11-08 (cached); categories and titles can change.
  9 652 of 14 294 object ids are in the 44 scraped categories (interior/vehicle-part/pickup
  categories were not scraped).
