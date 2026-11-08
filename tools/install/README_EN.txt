BAKU — map for GTA San Andreas (PC, original 1.0)
===================================================

This total conversion replaces the whole San Andreas world with this part of
Baku, Azerbaijan: Bilajari, Darnagul, 9th microdistrict, Nasimi, Yasamal,
Yeni Yasamal, 28 May, Heydar Aliyev Center, Baku White City and the Seaside
Boulevard (Caspian coast, Boyukshor lake and Qanli gol included).

Features
  * Real road network (OpenStreetMap): avenues, streets, courtyard lanes,
    medians, roundabouts, elevated viaducts with piers, tunnel/underpass
    ramps, sidewalks and kerbs.
  * Real terrain (the Baku amphitheatre, Yasamal escarpment) and water:
    Caspian Sea, Boyukshor, Qanli gol, Yasamal lake.
  * Buildings: ORIGINAL GTA San Andreas building models chosen and placed to fit
    the real Baku building footprints (34,000+ buildings).
  * Landmarks: Heydar Aliyev Center, Flame Towers (glowing like flames at
    night), Heydar Mosque, Maiden Tower.
  * Real shop / market names (Bravo, Araz, Bazarstore, pharmacies, banks,
    cafes ...), blue street-name plates, zebra crossings.
  * Azerbaijani road signs (STOP, give way, pedestrian crossing, no entry, speed
    limits, bus stop, Baku Metro "M" ...) - breakable like the vanilla ones.
  * Street lamps, 40,000+ trees, bus shelters, benches, parked cars in
    courtyards and parking lots.
  * Car and pedestrian AI paths, traffic lights at the real signalised
    junctions (the lights you see are the phases the AI obeys).
  * Radar / pause-menu map of Baku.
  * District names on screen (Nasimi, Yasamal, Bilajari, 9th microdistrict, ...).
  * Free roam: you start next to the Heydar Aliyev Center; hospitals and police
    respawn points are at real locations.

INSTALL
  1. GTA San Andreas PC 1.0 (a clean, unmodded copy is recommended), ~500 MB free.
  2. Run INSTALL.bat (use "Run as administrator" if the game is in Program Files).
  3. Pick your GTA San Andreas folder (the one with gta_sa.exe).
  4. Start the game and choose "Start New Game". Do NOT load old saves.

Everything the installer changes (gta3.img directory, data\object.dat, the
GXT files ...) is backed up to <game>\baku_backup.
UNINSTALL.bat restores the original game byte for byte.

Alternative with Python:
  python installer\baku_install.py install   "C:\...\GTA San Andreas"
  python installer\baku_install.py uninstall "C:\...\GTA San Andreas"

NOTES
  * The map is built at 0.675 scale so the 8.9 x 8.7 km area fits the 6 x 6 km
    San Andreas world; roads use San Andreas' 5.4-unit AI lanes.
  * No story missions: this is a free-roam total conversion.
  * modloader users: disable other map mods.
  * The San Andreas font has no ə ş ğ ı; district names use ä / sh / gh / i.

Sources / licences
  * Map data (c) OpenStreetMap contributors, ODbL, via Overture Maps.
  * Elevation: AWS Terrain Tiles (Mapzen/Tilezen, SRTM).
  * Building models: GTA San Andreas (Rockstar Games) — not redistributed; the
    mod uses the original files of your own game.
  * All new models, textures and tools were created for this project.
