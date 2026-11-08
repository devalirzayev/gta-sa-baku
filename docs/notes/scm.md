# GTA SA (PC 1.0) `main.scm` — verified spec for our free-roam script

Scope: the container format the PC 1.0 exe reads, the argument encoding, every opcode
`tools/gta/scm.py` emits, and what the engine itself needs from MAIN when a game starts.
Implementation: `tools/gta/scm.py`; tests: `tests/test_scm.py`.

Rule used throughout: **game code wins** (gta-reversed, which hooks the 1.0 US exe function
by function; addresses below are 1.0 US).  Where gta-reversed has not reimplemented a
handler, the claim rests on (a) the three community opcode databases agreeing, (b) the
GTA VC decompilation (reVC) for the shared logic and (c) R*'s own SA script source using
the command the same way.  Such handlers are marked "vanilla" in §4.

## Sources

| Tag | What | Used for |
|---|---|---|
| GR | gta-reversed/gta-reversed `master` (fetched 2026-11-08): `source/game_sa/Scripts/{TheScripts,RunningScript,SCMChunks,ScriptParam}.*`, `Scripts/CommandParser/{Parser,ReadArg,StoreArg}.hpp`, `Scripts/Commands/*.cpp`, `StreamedScripts.cpp`, `ScriptsForBrains.cpp`, `Game.cpp`, `app/app_game.cpp`, `app/platform/win/WinMain.cpp`, `Restart.*`, `GameLogic.cpp`, `PlayerInfo.cpp`, `Entity/Ped/{Ped,PlayerPed}.cpp`, `Hud.cpp`, `Camera.*`, `Streaming.cpp`, `Collision/ColStore.cpp`, `Stats.cpp`, `Wanted.cpp`, `Weather.cpp`, `Population.cpp`, `CarCtrl.cpp`, `World.cpp`, `Enums/*` | engine behaviour (primary) |
| VC | reVC (`mrxenginner/reVC`, `src/control/Script*.cpp`), decompiled GTA VC | vanilla header readers, handlers not reversed in GR |
| G3 | thelink2012/gta3sc (`src/codegen.cpp`, `src/disassembler.cpp`, `src/compiler.cpp`, `src/main_compile.cpp`, `config/gtasa/{commands.xml,commandline.txt,default.xml}`) — GTA3script compiler/decompiler built to reproduce R*'s output | header layout as R*'s compiler writes it, arg types, independent decompiler/compiler used by the tests |
| SB | Sanny Builder: `sannybuilder/library` `sa/sa.json` v1.68; `sannybuilder/data` `sa/SASCM.INI` (2023-07-01), `sa/stripped.txt`, `sa_sbl/stripped scm/main.txt`, `sa/CustomVariables.ini` | opcode DB, conventional minimal script, variable numbering |
| RS | R*'s SA source as reconstructed in `Sergeanur/UndefinifiedGrove`: `main.sc`, `main/misc/initial.sc` | what the original MAIN does at new game |
| WK | gtamods wiki (raw): `SA_SCM`, `SCM_Instruction`, `Script.img`, opcode pages 0001 0002 0053 00C0 0111 016A 016C 0180 01B4 01B6 04BB … | cross-check |
| EN | TsyVM/SAEncyclopedia C18 (measurements of the retail `main.scm`/`script.img`) | secondary cross-check of the retail header |

All downloaded copies (data only) are in
`/tmp/claude-0/-home-user-ila/5b6b71aa-4af7-540b-9036-4dec5b516689/scratchpad/scm_ref/`
(`gtarev/`, `revc/`, `gta3sc/`, `gta3sc_src/`, `sblib/`, `sbdata/`, `examples/`, `wiki/`, `misc/`),
plus a locally built gta3sc binary in `gta3sc_build/`.

---

## 1. How the engine loads and runs `main.scm`

### 1.1 Loading — `CTheScripts::Init` (0x468D50, GR TheScripts.cpp)
* `ScriptSpace` is one static 269 000-byte array at 0xA49960: the first
  `MAIN_SCRIPT_SIZE = 200 000` bytes hold `main.scm`, the next `MISSION_SCRIPT_SIZE = 69 000`
  one mission at a time (TheScripts.h). It is zero-filled, then
  `data\script\main.scm` is read into it (`Read(ScriptSpace, MAIN_SCRIPT_SIZE)`): a longer file is
  silently truncated (missions are later read from the file by offset).
  Mission-pack games read `MPACK\MPACKn\SCR.SCM` instead (not used by us).
* 96 `CRunningScript` slots are reset and put on the idle list; `OnAMissionFlag = 0`.
* Header readers, in this order: `ReadObjectNamesFromScript` (0x486720) +
  `UpdateObjectIndices` (0x486780), `ReadMultiScriptFileOffsetsFromScript` (0x4867C0), and —
  unless mission pack — `CStreamedScripts::ReadStreamedScriptData` (0x470750). Then lots of
  script-side state is reset (HUD/text flags, `bDisplayHud = true`, `bPlayerIsOffTheMap = false`,
  sphere/checkpoint/search-light arrays, …).

### 1.2 When MAIN first runs
* New game from the start-up menu: `WinMain`/`ProcessGameLogic` → `InitialiseGame()`
  (GR app_game.cpp) → `CGame::Initialise("DATA\\GTA.DAT")` (0x53BC80):
  `Init1 → LoadLevel(default.dat, gta.dat) → Init2` (→ `CTheScripts::Init`) →
  **`CTheScripts::StartTestScript()`** (0x464D40, `StartNewScript(&ScriptSpace[0])`) →
  **`CTheScripts::Process()`** → **`TheCamera.Process()`** → `Init3`.
* "New game" while a game is running: `CGame::ShutDownForRestart` +
  `CGame::InitialiseWhenRestarting` (0x53C680) → `ReInitGameObjectVariables` (0x53BCF0), which
  re-runs `CTheScripts::Init` and — if not loading a save — `CCranes::InitCranes`,
  `StartTestScript`, `CTheScripts::Process`, `TheCamera.Process`, `CTrain::InitTrains`.
* Loading a save: (after `CGame::Initialise` if the game was not initialised yet — MAIN runs
  once there too) `ShutDownForRestart` + `InitialiseWhenRestarting` with `m_bLoadingData` set:
  `ReInitGameObjectVariables` skips `StartTestScript` and `GenericLoad` → `CTheScripts::Load`
  (0x5D4FD0, see 1.4) restores the saved threads instead.
* So MAIN executes from offset 0 of `ScriptSpace` (the header GOTOs are executed, §2) **once,
  synchronously, during loading, until its first `WAIT`**, then the camera is processed and the
  frame loop (`CGame::Process` 0x53BEE0) starts. Consequence: everything the first frame needs
  (the player ped!) must be created before MAIN's first `WAIT` (§5 item 1).
* The thread started this way has `m_BaseIP = nullptr` (`StartNewScript(uint8*)` only sets `m_IP`),
  so all jumps in MAIN use **positive absolute ScriptSpace offsets** (`CRunningScript::UpdatePC`
  0x464DA0: `newIP >= 0 ? &ScriptSpace[newIP] : m_BaseIP + abs(newIP)`).

### 1.3 Per frame — `CTheScripts::Process` (0x46A000), `CRunningScript::Process` (0x469F00)
* Runs every active thread; adds the frame time to each thread's locals 32/33 (TIMERA/TIMERB)
  and to the mission locals; a thread runs commands until one returns `OR_WAIT`
  (`WAIT` sets `m_WakeTime = now + ms`). A loop without `WAIT` never returns → game hangs.
* `DoDeathArrestCheck` (0x485A50) is only called for threads with `m_UsesMissionCleanup`
  (missions started by `LOAD_AND_LAUNCH_MISSION`), and only acts when
  `IsPlayerOnAMission()` and the player is restarting after death/arrest. MAIN is never reset.

### 1.4 Save / load — `CTheScripts::Save` (0x5D4C40) / `Load` (0x5D4FD0)
* Save writes `ScriptSpace[0 .. first GOTO target)` (= the 8 header bytes + global space), then
  brain table, `OnAMissionFlag`, building swaps, …, then every non-external thread with its IP
  as a ScriptSpace offset. Load copies those bytes back over `ScriptSpace` and restarts the
  threads at the saved offsets. **A savegame made with another `main.scm` (e.g. vanilla) is
  incompatible** with ours (its globals overwrite our space and its threads resume at offsets
  that are not code in our file). Our script creates no save points, so it cannot be saved.

---

## 2. Container format (header)

`main.scm` starts with six `GOTO` instructions (`02 00 01 <int32 target>`, i.e. opcode 0002 with
one type-0x01 int32 argument). Each jumps over a data block; the byte after each GOTO is a
segment id. The engine both *executes* this chain (MAIN starts at offset 0) and *walks* it to
find the data. Layout (GR `SCMChunks.hpp` structs, G3 `codegen.cpp generate_code(CompiledScmHeader)`,
WK `SA_SCM`, EN C18.1/C18.2 — all agree):

Segments are named by their **id byte** below (`'s'`, 0, 1, 2, 3, 4 — position 1..6 in the file):

| Id | Offset | Bytes | Content | Read by |
|---|---|---|---|---|
| `'s'` (0x73) | 0 | `02 00 01 <i32 S1>` `73` + global space (`S1 - 8` bytes, zeros) | global variables live at `[8, S1)`; first global offset is 8 | `GetSizeOfVariableSpace()` = u32 at [3] (= S1); `GetPointerToScriptVariable` asserts `8 <= off < S1`; `Save` |
| 0 | S1 | `02 00 01 <i32 S2>` `00` + `u32 n` + `n × char[24]` | used-object (model) names; entry 0 is empty and ignored; `n >= 1`, `n < 395` | `ReadObjectNamesFromScript`, `UpdateObjectIndices` (names → model ids for negative model refs) |
| 1 | S2 | `02 00 01 <i32 S3>` `01` + `u32 mainSize, u32 largestMission, u16 numMissions, u16 numExclusive, u32 largestMissionLocals, i32 missionOffset[numMissions]` | script multifile info | `ReadMultiScriptFileOffsetsFromScript` (stored in statics; missions loaded by `LOAD_AND_LAUNCH_MISSION` from the file offsets) |
| 2 | S3 | `02 00 01 <i32 S4>` `02` + `u32 largestStreamed, u32 numStreamed, numStreamed × {char name[20]; u32 offset; u32 size}` | streamed scripts (`script.img`) | `CStreamedScripts::ReadStreamedScriptData` |
| 3 | S4 | `02 00 01 <i32 S5>` `03` + `u32` | unknown, 0 | nothing (struct `tSCMUnknownChunk3` is never used in GR) |
| 4 | S5 | `02 00 01 <i32 CODE>` `04` + `u32 globalSpaceSize, u32 buildNumber` | "extra info" | nothing (`tSCMExtraInfoChunk` never used in GR) |
| – | CODE | instructions | MAIN starts executing here after the chain | |

Details and rules:
* **Ids/order.** gta-reversed's `GetSCMChunk` (NOTSA generalisation) looks a chunk up by id
  within at most 6 hops and asserts every hop starts with `02 00 01`; the vanilla readers (VC
  `ReadObjectNamesFromScript`: `varSpace = GetSizeOfVariableSpace(); count at varSpace+8`;
  `ReadMultiScriptFileOffsetsFromScript`: `next = u32[varSpace+3]; data at next+8`) simply hop
  from GOTO target to GOTO target. Emitting exactly `'s',0,1,2,3,4` in that order with each
  target = next segment satisfies both. G3's decompiler header parser does the same hops.
* **Global space size.** The retail file has 43 800 bytes (S1 = 43 808, EN; the first 8 bytes of
  retail and of our default build are identical: `02 00 01 20 ab 00 00 73`). Globals are byte
  offsets (Sanny `$N` = offset 4N). Ships zeroed (EN); initialisation is done by code.
* **mainSize** = size of the main block from offset 0 including the header = offset of the first
  mission (EN: 194 125 == first mission offset). Without missions = file size. G3's decompiler
  uses it to bound MAIN.
* **Streamed scripts / `script.img`.** `CStreaming::LoadCdDirectory` registers every `*.scm`
  entry of every IMG listed in gta.dat (`IMG DATA\SCRIPT\SCRIPT.IMG` is in vanilla gta.dat) via
  `CStreamedScripts::RegisterScript` (fixed 82-slot array, no bounds check). Then
  `ReadStreamedScriptData` loops over the **header** entries:
  `if (streamIdx >= m_nCountOfScripts) break; i = FindStreamedScript(name); m_aScripts[i].size = …`
  — a header name that is not in script.img gives `i = -1` and the vanilla code writes
  `m_aScripts[-1]` (GR comment "Find returning -1 ends up with OOB access"). Streamed scripts are
  only ever loaded when a script command requests them or when a *script-registered* brain
  fires: `CScriptsForBrains` (GR ScriptsForBrains.cpp) is cleared by `CTheScripts::Init` and
  only filled by script commands (`REGISTER_SCRIPT_BRAIN_FOR_CODE_USE` 07D3,
  `REGISTER_OBJECT_SCRIPT_BRAIN_FOR_CODE_USE` 07D4, `REGISTER_ATTRACTOR_SCRIPT_BRAIN_FOR_CODE_USE`
  0884, `ALLOCATE_*SCRIPT_TO_*` 0711/0712/0805/0899/0927–0929); the engine-side users (peds,
  objects, dummies, car generators, `TaskComplexUseAttractor`) only look names up there, and
  GR has no engine caller of the `AddNew…ScriptBrain…` functions.
  **Decision: 0 streamed scripts** (`largestStreamed = 0, numStreamed = 0`). Then nothing in
  `script.img` is ever looked up or loaded, so the user's original `script.img` (or none, or a
  modded one) is harmless and can stay in gta.dat. (R*/gta3sc/Sanny's default writes one extra
  `AAA` entry `{ "AAA", 0, 8 }`; the retail script.img does contain `aaa.scm` (G3 main_compile.cpp
  writes it, EN measured it), but a modded script.img might not, so we do not emit it by default;
  `aaa_stream=True` reproduces it.)
* **Segment id 4 values** (unused at runtime): we write `globalSpaceSize = S1 - 8` (retail and
  G3 agree) and `0x0000023E` (bytes `3E 02 00 00`) exactly as G3 does (retail has 569 there per
  EN; irrelevant).
* **Missions**: none (`numMissions = 0`, `largestMission = 0`, `numExclusive = 0`,
  `largestMissionLocals = 0`).
* **Used objects**: just the empty entry 0 (we reference models by positive id).

What `scm.build_main_scm()` emits by default (globals 43 800 B, no models/missions/streams):
`S1 = 43808, S2 = 43844, S3 = 43868, S4 = 43884, S5 = 43896, CODE = 43912`, header =
43 912 bytes; a typical free-roam script is ~44.3 KB total (limit 200 000).

---

## 3. Instruction and argument encoding

* Instruction = `u16` little-endian opcode word; bit 15 = NOT flag (inverts a condition result;
  `CRunningScript::ProcessOneCommand`, `scm::Instruction { Command:15; NotFlag:1 }`), then the
  arguments. **The argument count is not stored**: each handler reads a fixed number
  (`CollectParameters(n)` in vanilla, the C++ signature in GR's typed handlers), so a wrong count
  desynchronises the stream and crashes (WK SCM_Instruction).
* Each argument = 1 type byte + payload (`eScriptParameterType`, GR RunningScript.h;
  `CollectParameters` 0x464080, `ReadTextLabelFromScript` 0x463D50; WK table identical):

| Type | Payload | Meaning |
|---|---|---|
| 00 | – | end of variadic arg list (START_NEW_SCRIPT etc.; not used) |
| 01 | i32 | immediate int32 (also used for labels/jump targets) |
| 02 | u16 | global variable, **byte offset** into ScriptSpace |
| 03 | u16 | local variable index (0–31, 32 = TIMERA, 33 = TIMERB) |
| 04 | i8 | immediate int, sign-extended |
| 05 | i16 | immediate int, sign-extended |
| 06 | f32 | immediate IEEE float (SA/VC; III used 16-bit fixed point) |
| 07 / 08 | u16 base, u16 index var, u8 size, u8 (type:7, indexIsGlobal:1) | global / local number array element |
| 09 | 8 bytes | immediate TEXT_LABEL (NUL padded) |
| 0A / 0B | u16 | global / local 8-byte string variable |
| 0C / 0D | 6 bytes | global / local 8-byte string array |
| 0E | u8 len + bytes | immediate variable-length STRING, no NUL (len read as *signed* char → ≤ 127) |
| 0F | 16 bytes | immediate 16-byte string |
| 10 / 11 | u16 | global / local 16-byte string variable |
| 12 / 13 | 6 bytes | global / local 16-byte string array |

* **int vs float is not converted by the vanilla game.** `CollectParameters` stores the raw 4
  bytes of an int (types 01/04/05, sign-extended) or float (06) into `ScriptParams[i]` and the
  handler reads `.iParam` or `.fParam`; an int literal passed to a float parameter is
  reinterpreted as a denormal float (only 0 survives). (GR's typed `Read<float>` converts, but
  that is NOTSA.) gta3sc rejects `90` where a FLOAT is expected ("expected float"). The assembler
  therefore types every argument from its opcode table and always writes floats as type 06.
* Int literals use the smallest encoding (04/05/01), like R*'s compiler, gta3sc and Sanny.
* Labels: type 01 + absolute int32 offset (MAIN). Missions/streamed scripts use negative
  (base-relative) offsets; not used here (G3 refuses a base-relative target 0 since −0 = 0; in
  MAIN offset 0 is the header, so no label can be there).
* Strings: `SCRIPT_NAME` takes a TEXT_LABEL (09, ≤ 7 chars so there is a NUL —
  `CRunningScript::SetName` asserts `size < 8`); `GIVE_PLAYER_CLOTHES_OUTSIDE_SHOP` takes STRING
  (0E) — G3 compiles STRING-typed args as 0E and TEXT_LABEL as 09, upper-cased; verified by
  compiling with gta3sc. Clothes names are hashed with `CKeyGen::GetUppercaseKey`, so case is
  irrelevant.
* Conditions: `ANDOR` (00D6, Sanny "IF") `0` = one condition; `k` in 1..8 = k+1 conditions
  joined by AND, `20+k` = k+1 conditions joined by OR (GR `AndOr`, `UpdateCompareFlag`,
  `LogicalOpType`; R*/Sanny use at most 8 conditions); the following conditions update
  `m_CondResult`, then `GOTO_IF_FALSE` (004D) branches. We only use single conditions.

---

## 4. Opcodes used (exact parameter types)

Kinds: **int** = int literal (04/05/01) or int variable; **int-lit** = literal only;
**float** = type 06 literal or variable; **gvar**/**lvar** = global/local variable;
**out** = variable receiving a result; **label** = 01 + absolute offset; **TEXT_LABEL** = 09;
**STRING** = 0E. Every row was checked (tests) against sa.json (name, `num_params`, input/output
types and sources), SASCM.INI (count) and gta3sc commands.xml (Type/Out/Allow* attributes); all
three agree for every opcode. Because those databases partly derive from each other, the
argument counts were also checked against R*'s own SA scripts (RS `main.sc`, `initial.sc`,
`debug.sc`, `camera.sc`, `vcrash1.sc`): every R* command we emit (34 of 40; the other 6 —
NOP, SET_VAR_INT, SET_LVAR_INT, IS_INT_LVAR_GREATER_THAN_NUMBER, ANDOR, GOTO_IF_FALSE — are
what the compiler generates for `x = 0`, `IF`, … and are reimplemented in GR) is used there with
exactly our argument count, including all eleven "vanilla" ones.
"GR" = handler reimplemented in gta-reversed (signature checked); "vanilla" = handler not
reimplemented (see note at top).

| Op | R* name (Sanny syntax) | Params | Handler / semantics |
|---|---|---|---|
| 0001 | WAIT (`wait`) | int ms | GR `Wait`: `m_WakeTime = now + ms`, `OR_WAIT` |
| 0002 | GOTO (`jump`) | label | GR `GoTo` → `UpdatePC` |
| 0004 | SET_VAR_INT | gvar, int-lit | GR `AssignTo<int32,int32>` |
| 0006 | SET_LVAR_INT | lvar, int-lit | GR `AssignTo<int32,int32>` (used for TIMERA = 0) |
| 0019 | IS_INT_LVAR_GREATER_THAN_NUMBER | lvar, int-lit (condition) | GR `IsGreater<int32>` |
| 004D | GOTO_IF_FALSE (`jf`) | label | GR `GoToIfFalse` |
| 0053 | CREATE_PLAYER | int playerIndex, float x, float y, float z, out player | GR `CreatePlayer`: loads special model 0 "player" if needed, `CPlayerPed::SetupPlayerPed` (0x60D790), `PLAYERSTATE_PLAYING`; **z ≤ −100 → `CWorld::FindGroundZForCoord`**; z is lifted by `GetDistanceFromCentreOfMassToBaseOfModel()` (pass the ground/feet z); `ClearSpaceForMissionEntity`; returns playerIndex (0) |
| 00C0 | SET_TIME_OF_DAY | int h, int m | GR `SetTimeOfDay` → `CClock::SetGameClock(h, m, 0)` |
| 00D6 | ANDOR (Sanny IF) | int (we always pass literal 0) | GR `AndOr` |
| 0109 | ADD_SCORE | int player, int money | GR `AddScore`: `CPlayerInfo.m_nMoney += money` |
| 0111 | SET_DEATHARREST_STATE | int (0/1) | vanilla; VC: `m_bDeatharrestEnabled = (p == 1)`; only matters for mission threads (§1.3); R* main.sc and Sanny stripped set 0 in MAIN |
| 016A | DO_FADE | int ms, int dir (0 = FADE_OUT to colour, 1 = FADE_IN to game) | GR `DoCamFade` → `TheCamera.Fade(ms/1000, dir)`. Engine enum `eFadeFlag{FADE_IN=0,FADE_OUT=1}` is named from the overlay's view; numerically script 0 = black, 1 = visible (consistent with `CGameLogic` respawn fades; WK 016A) |
| 016C | ADD_HOSPITAL_RESTART | float x, y, z, heading°, int townId | vanilla (SA added townId vs VC's 4 params; R* initial.sc uses 5 args); → `CRestart::AddHospitalRestartPoint` (0x460730) |
| 016D | ADD_POLICE_RESTART | float x, y, z, heading°, int townId | vanilla; → `CRestart::AddPoliceRestartPoint` (0x460780) |
| 0173 | SET_CHAR_HEADING | int char, float deg | GR `SetCharHeading` (ignored while in a vehicle) |
| 0180 | DECLARE_MISSION_FLAG | gvar | vanilla; VC: `OnAMissionFlag = u16 after the type byte` (must be a type-02 global) |
| 01B2 | GIVE_WEAPON_TO_CHAR | int char, int weaponType, int ammo | vanilla (GR registration commented out); VC: `SetCurrentWeapon(GiveWeapon(type, ammo))`; SASCM: "Load the weapon model before using this" |
| 01B4 | SET_PLAYER_CONTROL | int player, int on | GR `SetPlayerControl` → `CPlayerInfo::MakePlayerSafe(!on, 10)` (0x56E870) |
| 01B6 | FORCE_WEATHER_NOW | int eWeatherType 0..22 | vanilla → `CWeather::ForceWeatherNow` (0x72A4F0): forced + old + new type |
| 01B7 | RELEASE_WEATHER | – | vanilla → `CWeather::ReleaseWeather` (0x72A510): `ForcedWeatherType = -1` |
| 01EB | SET_CAR_DENSITY_MULTIPLIER | float | vanilla; VC: `CCarCtrl::CarDensityMultiplier = f` (reset to 1.0 by `CCarCtrl::Init/ReInit`) |
| 01F0 | SET_MAX_WANTED_LEVEL | int 0..6 | GR → `CWanted::SetMaximumWantedLevel` (0x561E70, asserts ≤ 6) |
| 01F5 | GET_PLAYER_CHAR | int player, out char | GR `GetPlayerChar`: ped-pool handle |
| 0247 | REQUEST_MODEL | int model | GR `LoadModel`: `RequestModel(MISSION_REQUIRED|KEEP_IN_MEMORY)`; negative = used-object index |
| 0248 | HAS_MODEL_LOADED | int model (condition) | GR `HasModelLoaded` |
| 0249 | MARK_MODEL_AS_NO_LONGER_NEEDED | int model | GR `MarkModelNotNeeded` |
| 02EB | RESTORE_CAMERA_JUMPCUT | – | GR → `CCamera::RestoreWithJumpCut` (0x50BAB0) — dereferences `FindPlayerPed()`, needs the player |
| 0373 | SET_CAMERA_BEHIND_PLAYER | – | GR → `SetCameraDirectlyBehindForFollowPed_CamOnAString` (0x50BD40), uses player heading |
| 038B | LOAD_ALL_MODELS_NOW | – | GR: `CStreaming::LoadAllRequestedModels` |
| 03A4 | SCRIPT_NAME | TEXT_LABEL | GR `ScriptName` → `SetName` (< 8 chars) |
| 03CB | LOAD_SCENE | float x, y, z | GR `LoadScene` → `CStreaming::LoadScene` (0x40EB70), synchronous, safe without a player (`FindPlayerCoors` returns 0,0,0) |
| 03DE | SET_PED_DENSITY_MULTIPLIER | float | GR → `CPopulation::PedDensityMultiplier` (reset to 1.0 by `CPopulation::Initialise`) |
| 04BB | SET_AREA_VISIBLE | int area | vanilla; VC: `CGame::currArea = a; RemoveBuildingsNotInArea(a)` |
| 04E4 | REQUEST_COLLISION | float x, y | vanilla; VC: `CColStore::RequestCollision(pos)` (SA 0x410C00) |
| 0629 | SET_INT_STAT | int statId, int value | GR `SetStat<int32>` → `CStats::SetStatValue` (0x55A070) + `DisplayScriptStatUpdateMessage` (no message for stat 181) |
| 070D | BUILD_PLAYER_MODEL | int player | GR → `CClothes::RebuildPlayer` |
| 07AF | GET_PLAYER_GROUP | int player, out group | GR `GetPlayerGroup` |
| 0860 | SET_CHAR_AREA_VISIBLE | int char, int area | GR `SetCharAreaVisible`; area 0 also clears the ped's enex and `CEntryExitManager::ms_entryExitStackPosn` |
| 087B | GIVE_PLAYER_CLOTHES_OUTSIDE_SHOP | int player, STRING texture, STRING model, int bodyPart | vanilla; used by R* main.sc at new game with exactly our four pairs; buffer size not known → we limit strings to ≤ 15 chars (R*'s longest is 14) |

---

## 5. What the engine expects from MAIN at a new game (and why each command is there)

1. **Player before the first WAIT.** `CGameLogic::Update` (0x442AD0) runs every frame and starts
   with `SetPlayerWantedLevelForForbiddenTerritories` (0x441770), which dereferences
   `FindPlayerPed()` unconditionally; `TheCamera.Process()` runs right after MAIN's first slice
   (§1.2); `RestoreWithJumpCut` dereferences the player. R* main.sc creates the player long
   before its first WAIT. → `CREATE_PLAYER` at frame 0.
2. **World/collision at the spawn first.** R* main.sc: `REQUEST_COLLISION x y` then
   `LOAD_SCENE x y z` then `CREATE_PLAYER` (`RequestCollision` requests the COL slots around the
   point, `LoadScene` loads them plus IPLs/models synchronously). Spawn z = ground (feet) height;
   `None` in our API = z −100 → ground lookup (requires the collision just loaded).
3. **Camera.** `SET_CAMERA_BEHIND_PLAYER` after `SET_CHAR_HEADING` at frame 0 (as R*), then after
   the first WAIT `RESTORE_CAMERA_JUMPCUT` + `SET_CAMERA_BEHIND_PLAYER`.
4. **Fade.** R* fades to black at once (`DO_FADE 0 FADE_OUT`) and the intro fades back in. With
   no intro we must fade in ourselves (`DO_FADE 1000 FADE_IN`), otherwise the screen stays
   black. Death/arrest fades are done by the engine (`CGameLogic`).
5. **Death/arrest = engine.** `CGameLogic::Update`: on `PEDSTATE_DEAD`/`ARRESTED` → after 3–4 s
   and the fade: penalty (`$100`; arrest fee by wanted level; `ClearWeapons`), position =
   `CRestart::FindClosestHospitalRestartPoint` / `FindClosestPoliceRestartPoint`
   (0x460850 / 0x460A50), `RestorePlayerStuffDuringResurrection` (0x442060: teleport to
   `point + (0,0,1)`, heading in degrees, area 0, camera behind player) and
   `SortOutStreamingAndMemory` (0x441440: `LoadSceneCollision` + `LoadScene`). MAIN keeps running
   untouched. Requirements:
   * **≥ 1 hospital and ≥ 1 police point**: with none (or none whose `townId <=
     STAT_CITY_UNLOCKED`), the out-parameters stay uninitialised (vanilla stack garbage;
     GR adds `NOTSA_UNREACHABLE` for police) → respawn at a random position.
   * **≤ 10 each**: `CRestart::MAX_RESTART_POINTS = 10`; `Add*RestartPoint` does not bound-check
     (overflow corrupts the following statics). WK says "max 8" — GR arrays are 10; we enforce 10.
   * townId: we pass 0 (always eligible). Distance is ×6 when the point is in another
     `CTheZones` level than the player (from our zone data).
   * Restart z = ground height; the engine adds 1 m. Points are not ground-snapped by us
     (`FindGroundZForCoord` without loaded collision returns a default), so z must be real.
6. **Cities unlocked.** `SetPlayerWantedLevelForForbiddenTerritories` gives 4 stars inside two
   hard-coded polygons (LV: whole north band, if `STAT_CITY_UNLOCKED (181) <= 1`; SF: west part,
   if `<= 0`), checked every 32 frames below z 950. Our Baku map covers the whole ±3000 area, so
   without unlocking the player would be hunted in large parts of it. `SET_INT_STAT 181 4` (value
   4 = all cities, also what the Sanny stripped template uses; GR AERadioTrackManager treats 4 as
   "finished"). Also disables the train-crossing logic tied to locked cities
   (`CalcDistanceToForbiddenTrainCrossing`). Restart `townId` compares against this stat.
7. **ONMISSION.** `DECLARE_MISSION_FLAG $409` + `$409 = 0` (R* `flag_player_on_mission` is $409).
   `IsPlayerOnAMission()` (0x464D50) = `OnAMissionFlag && ScriptSpace[OnAMissionFlag] == 1`; it is
   consulted by popcycle, radar contact blips, pickups (properties/kill-frenzy/2-player),
   gang wars, ped groups, busted audio. With the flag declared and 0 everything behaves as
   free roam (undeclared would behave the same; declaring keeps CLEO missions working).
8. **HUD/radar need no command.** `CHud::ReInitialise` (0x588880, also called from
   `CHud::Initialise` and `ReInitGameObjectVariables`) clears
   `bScriptDontDisplayRadar/AreaName/VehicleName`; `CTheScripts::Init` sets `bDisplayHud = true`,
   `bPlayerIsOffTheMap = false`. `CHud::Draw` hides HUD/radar only for those flags, the
   frontend "HUD off" option, or `TheCamera.m_bWideScreenOn`, which is only switched on by
   `CCamera::SetWideScreenOn` (script `SWITCH_WIDESCREEN`, cutscenes) — never used by our script.
   (Not verified: whether `CCamera::Init` itself clears it; GR's `Init` does not touch it.)
   No DISPLAY_RADAR/DISPLAY_HUD needed.
9. **Interior/area.** `ReInitGameObjectVariables` sets `CGame::currArea = 0`; we still set
   `SET_AREA_VISIBLE 0` (R* after intro, Sanny template) and `SET_CHAR_AREA_VISIBLE player 0`.
10. **Clock/weather.** `SET_TIME_OF_DAY` at frame 0 (R*). `FORCE_WEATHER_NOW` keeps the weather
    forced until `RELEASE_WEATHER`; when released the weather cycles per *weather region*, which
    SA derives from hard-coded rectangles over the camera position (`CWeather::FindWeatherRegion`,
    based on 0x72A640: LV `x>1000,y>910`; desert `-850<x<1000,y>1280`; SF `x<-1430,
    -580<y<1430`; LA `250<x<3000,y<-850`; else countryside) — on our map that would mean e.g.
    sandstorm weather in the former desert rectangle. Default: forced weather
    (option `release_weather=True`). Valid types 0..22 (`eWeatherType`, `NUM_WEATHERS = 23`).
11. **Densities.** `PedDensityMultiplier`/`CarDensityMultiplier` are reset to 1.0 on every new
    game (`CPopulation::Initialise`, `CCarCtrl::Init/ReInit`); we set them explicitly anyway
    (configurable).
12. **Player model.** `CPedClothesDesc::Initialise` zeroes all clothes keys; R* gives
    VEST/VEST(0), JEANSDENIM/JEANS(2), SNEAKERBINCBLK/SNEAKER(3), PLAYER_FACE/HEAD(1) +
    `BUILD_PLAYER_MODEL` (from the original `player.img`). We do the same (option).
13. **Max wanted level.** `CWanted::InitialiseStaticVariables` already sets 6; explicit.
14. **Weapons/money (optional).** Weapon models (data/weapon.dat column 5, e.g. pistol 346,
    M4 356, see `scm.WEAPON_MODELS`) are requested, loaded (`LOAD_ALL_MODELS_NOW`) and polled with
    `HAS_MODEL_LOADED` behind a TIMERA timeout (5 s) so a missing model cannot block MAIN; then
    `GIVE_WEAPON_TO_CHAR`, `MARK_MODEL_AS_NO_LONGER_NEEDED`. Weapons are lost on death/arrest
    (engine `ClearWeapons`). Requires vanilla `default.ide`/weapon models to stay.
15. **Global numbering (CLEO compatibility).** player `$2`, scplayer `$3`, Players_Group `$11`,
    flag_player_on_mission `$409` — R*'s indices (G3 `commandline.txt --expect-var`, SB
    `CustomVariables.ini`); CLEO scripts read `$PLAYER_CHAR/$PLAYER_ACTOR/$ONMISSION` by these
    offsets. The global space is kept at the vanilla 43 800 bytes so stray CLEO writes to other
    vanilla globals land in zeros, not in our code.
16. **Not needed:** missions, streamed scripts, GXT text (we print nothing), stats totals
    (`CStats::GetPercentageProgress` uses a constant 187), `SET_TOTAL_NUMBER_OF_MISSIONS`.

### 5.1 The emitted MAIN (default options; `[...]` = only with the corresponding option)

```
frame 0 (runs inside CGame::Initialise):
03A4 SCRIPT_NAME 'MAIN'          016A DO_FADE 0 0 (black)      01F0 SET_MAX_WANTED_LEVEL 6
0111 SET_DEATHARREST_STATE 0     00C0 SET_TIME_OF_DAY h m      0629 SET_INT_STAT 181 4
04E4 REQUEST_COLLISION x y       03CB LOAD_SCENE x y z
0053 CREATE_PLAYER 0 x y z -> $2 01F5 GET_PLAYER_CHAR $2 -> $3 07AF GET_PLAYER_GROUP $2 -> $11
0173 SET_CHAR_HEADING $3 hdg     0373 SET_CAMERA_BEHIND_PLAYER 01B6 FORCE_WEATHER_NOW w
0180 DECLARE_MISSION_FLAG $409   0004 $409 = 0                 0001 WAIT 0
frame 1:
04BB SET_AREA_VISIBLE 0          0860 SET_CHAR_AREA_VISIBLE $3 0
016C ADD_HOSPITAL_RESTART x y z hdg 0  (1..10)   016D ADD_POLICE_RESTART x y z hdg 0  (1..10)
03DE SET_PED_DENSITY_MULTIPLIER 1.0    01EB SET_CAR_DENSITY_MULTIPLIER 1.0
087B GIVE_PLAYER_CLOTHES_OUTSIDE_SHOP $2 "VEST" "VEST" 0 (×4)   070D BUILD_PLAYER_MODEL $2
02EB RESTORE_CAMERA_JUMPCUT      0373 SET_CAMERA_BEHIND_PLAYER 01B4 SET_PLAYER_CONTROL $2 1
016A DO_FADE 1000 1 (fade in)    [01B7 RELEASE_WEATHER]        [0109 ADD_SCORE $2 money]
[weapons: 0247 REQUEST_MODEL m.. 038B; 0006 32@=0; :W 0001 WAIT 0; IF 32@>5000 -> GOTO R;
 IF HAS_MODEL_LOADED m.. else GOTO W; 01B2 GIVE_WEAPON_TO_CHAR $3 t ammo..; :R 0249 m..]
[extras]
:MAIN_LOOP 0001 WAIT 250; 0002 GOTO MAIN_LOOP
```
It contains every command of Sanny Builder's official SA `stripped.txt` (the community's
standard minimal main.scm) except its `CLEAR_HELP` and the `GET_TIME_OF_DAY` polling (unused);
the frame-0 part follows R* main.sc's order (`DO_FADE 0 FADE_OUT`, stats, `REQUEST_COLLISION`,
`LOAD_SCENE`, `CREATE_PLAYER`, `GET_PLAYER_GROUP/CHAR`, `SET_CAMERA_BEHIND_PLAYER`,
`SET_CHAR_HEADING`, `DECLARE_MISSION_FLAG`), the frame-1 part follows `stripped.txt`.
A hex/disassembly listing of a sample build is easiest to get with
`scm.disassemble(scm.build_freeroam_scm(...))`.

---

## 6. Validation performed (tests/test_scm.py)

* Independent reader (in the test) walks the header both ways the engine does (VC-style hops and
  GR `GetSCMChunk`), checks ids, exact segment sizes, `mainSize == len`, empty streamed table,
  globals within `[8, S1)`, size < 200 000.
* Instruction stream decoded with the test's own decoder; every argument's type byte matches
  the class from the reference table (floats are 06, labels are 01 and hit instruction starts,
  TEXT_LABEL 09, STRING 0E, outputs are variables).
* Frame-0 simulation from offset 0: header chain reaches the code; CREATE_PLAYER precedes the
  first WAIT and every player-dependent command; every backward jump encloses a WAIT; the script
  ends in an unconditional backward GOTO; 1–10 restart points of each kind; final fade-in and
  player control.
* Opcode tables vs live `sa.json` (names, counts, input/output types and sources, condition
  flags, none "unsupported"), `SASCM.INI` (counts) and gta3sc `commands.xml` (types, Out,
  AllowConst/AllowGlobalVar/AllowLocalVar), and argument counts vs R*'s own `.sc` sources.
* `CStreamedScripts::ReadStreamedScriptData` simulated against a vanilla-like, a modded
  (no aaa.scm) and an empty script.img: the default header is safe with all three; the optional
  `AAA` entry is only safe with a script.img that contains aaa.scm.
* Mutation checks (manual): wrong arity in the reference table, int-typed float parameter,
  corrupted segment id — each is caught.
* gta3sc (built from source) **decompiles** our file with its own header parser and command DB:
  identical instruction sequence, arguments and label targets.
* gta3sc **compiles** a GTA3script transcription of our instruction stream (globals declared
  so they get the same indices) to a main.scm that is **byte-identical** to ours (header + code)
  when our builder uses gta3sc's header options (`global_space = 4·409−4`, `aaa_stream=True`);
  the default build differs only by the header size/streamed table.

## 7. Open risks / uncertainties

* No in-game test. The vanilla-only handlers (0111, 016C, 016D, 0180, 01B2, 01B6, 01B7, 01EB,
  04BB, 04E4, 087B) are verified for count/types by three community databases and R*'s own
  usage, and their logic (except SA-only 087B and the SA `townId` argument of 016C/016D) by the
  VC decompilation, but not by SA code.
* `087B` string buffer size is unknown (we stay ≤ 15 chars; R* uses ≤ 14).
* Vanilla savegames must not be loaded with our main.scm (§1.4); we offer no saving.
* Restart/spawn z must be the real ground height of our map; if the player ends up below
  z −100, `CWorld::RemoveFallenPeds` (0x565CB0) teleports him to the nearest **ped path node**
  (needs our paths), else to z −95.
* Weapon models must exist (keep vanilla default.ide / weapon.dat / gta3.img weapon models).
* Weather regions (if `release_weather`) are SA's hard-coded rectangles.
* `SET_INT_STAT 181 4`: other unreversed consumers of STAT_CITY_UNLOCKED may exist in the exe;
  4 is the value the finished original game has, so it is a state the engine is known to handle.
* Segment id 4, second dword: retail 569 (EN), gta3sc 574, unused at runtime per GR.
* Brains/attractors: ped attractors on map objects that name streamed-script brains simply do
  nothing (no brains registered); vending machines, casinos, gyms etc. are inert.
