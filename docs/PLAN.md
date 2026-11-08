# Baku for GTA San Andreas (PC, classic) — project plan

Total-conversion map mod that replaces the San Andreas world with a part of Baku
(Biləcəri / 9-cu mikrorayon / Dərnəgül / Nəsimi / Yasamal / 28 May /
Heydər Əliyev Mərkəzi / Ağ Şəhər / Dənizkənarı Bulvar).

## Area & projection
* Source area (lon/lat): W 49.7877, E 49.8927, S 40.3586, N 40.4369 (~8.9 x 8.7 km)
* Data: Overture Maps 2026-10-21 (OpenStreetMap-derived, ODbL) in `data/raw/*.parquet`,
  elevation from AWS terrain tiles / Copernicus DEM.
* Game world: SA units (1 unit ~ 1 m), +X = east, +Y = north, Z up. Map must stay inside ±3000.
* Horizontal scale factor ~0.67 (real metres -> game units) so the whole area fits.
  Road widths use game-friendly lane widths, building models are original SA models
  chosen to fit the (scaled) footprints. Caspian sea level == game Z 0.

## Code layout (Python 3.13, numpy, shapely 2.2, pyproj, scipy, pillow)
* `tools/rw/`      RenderWare / GTA binary writers+readers: DFF, TXD, COL3, IMG (VER2)
* `tools/gta/`     game data writers: IDE, IPL (text+binary), paths (nodes*.dat), water.dat,
                   zones, radar, main.scm, tracks
* `tools/catalog/` SA model catalog builder (sizes, IDE/TXD, LOD pairs, categories)
* `tools/geo/`     geodata processing (projection, roads graph, terrain)
* `tools/build/`   map generator & packager
* `data/sa/`       SA reference metadata (model ids, sizes, IPL slot names, ...)
* `docs/notes/`    verified engine/format notes (with sources)

## Verified engine facts (gta-reversed source)
* IPL pool 256 slots; vanilla uses 191 -> ~60 free for our stream IPLs. IPL name <= 17 chars.
* COL pool 255 slots. TXD 5000, DFF 20000 model ids (vanilla max id 18630).
* Atomic (objs) model infos: 14000, vanilla uses ~13.9k -> must drop unused SA IDEs.
* Stream IPLs (`<name>_streamN.ipl` in IMG) whose text IPL is not in gta.dat are still
  loaded by `CIplStore::LoadAllRemainingIpls` and crash on LOD indices -> SA exterior
  stream IPLs must be neutralised (empty replacements / renamed entries).
* `IplDef::disableDynamicStreaming` defaults to true; only stream IPLs related to a
  gta.dat text IPL get dynamic streaming.
* COL models whose model name is unknown are skipped (safe to drop IDEs).
* COL3 vertices are int16/128 -> each collision model must stay within ±256 units.
