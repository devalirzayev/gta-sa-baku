"""GTA San Andreas (PC 1.0) main.scm assembler and minimal free-roam script builder.

The engine facts this module relies on (header chunk chain, parameter type codes, opcode
argument lists, what the game expects from MAIN at new-game start) are documented with
sources in docs/notes/scm.md.  Short version:

* main.scm = a chain of six GOTO "segments" followed by code.  The engine starts the MAIN
  thread at offset 0, so the header itself is executed (each segment begins with
  ``02 00 01 <int32 next>``, i.e. GOTO next) and the loaders find the data by walking the
  same chain: 's' globals, 0 used objects, 1 script-file info (missions), 2 streamed
  scripts, 3 unknown, 4 extra info.  (gta-reversed Scripts/SCMChunks.hpp,
  CTheScripts::GetSCMChunk/ReadObjectNamesFromScript/ReadMultiScriptFileOffsetsFromScript,
  CStreamedScripts::ReadStreamedScriptData; layout identical to thelink2012/gta3sc codegen.)
* Every argument is ``<type byte><payload>`` (eScriptParameterType).  The argument *count*
  is not stored; it is hard-coded per opcode in the game, so emitting a wrong count or a
  wrong numeric class (int vs float) breaks the script.  Every opcode emitted here is
  therefore checked against the OPCODES table below (cross-checked in tests/test_scm.py
  against Sanny Builder's sa.json + SASCM.INI and gta3sc's commands.xml).

Public API:
    ScmAssembler            -- labels, instructions, globals/locals, typed argument encoding
    build_main_scm(asm,...) -- header + code -> bytes (no missions, no streamed scripts)
    build_freeroam_scm(...) -- the complete free-roam MAIN script for the Baku map
    parse_header(data), disassemble(data) -- inspection helpers (same opcode table)
"""
from __future__ import annotations

import math
import struct
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

# ---------------------------------------------------------------------------------------
# Parameter data types (eScriptParameterType, gta-reversed source/game_sa/Scripts/RunningScript.h)
# ---------------------------------------------------------------------------------------
PT_END_OF_ARGUMENTS = 0x00
PT_INT32 = 0x01
PT_GLOBAL_VAR = 0x02          # uint16 byte offset into ScriptSpace
PT_LOCAL_VAR = 0x03           # uint16 local variable index
PT_INT8 = 0x04
PT_INT16 = 0x05
PT_FLOAT = 0x06               # IEEE-754 float32 (SA/VC; GTA III: int16 fixed point / 16)
PT_GLOBAL_NUMBER_ARRAY = 0x07
PT_LOCAL_NUMBER_ARRAY = 0x08
PT_TEXT_LABEL8 = 0x09         # 8 raw bytes, NUL padded (GTA3script TEXT_LABEL)
PT_GLOBAL_TEXT_LABEL8_VAR = 0x0A
PT_LOCAL_TEXT_LABEL8_VAR = 0x0B
PT_GLOBAL_TEXT_LABEL8_ARRAY = 0x0C
PT_LOCAL_TEXT_LABEL8_ARRAY = 0x0D
PT_VARLEN_STRING = 0x0E       # uint8 length + bytes, no NUL (GTA3script STRING)
PT_TEXT_LABEL16 = 0x0F        # 16 raw bytes, NUL padded
PT_GLOBAL_TEXT_LABEL16_VAR = 0x10
PT_LOCAL_TEXT_LABEL16_VAR = 0x11
PT_GLOBAL_TEXT_LABEL16_ARRAY = 0x12
PT_LOCAL_TEXT_LABEL16_ARRAY = 0x13

# payload sizes of the fixed-size types (variable-length 0x0E handled separately)
PT_SIZES = {0x01: 4, 0x02: 2, 0x03: 2, 0x04: 1, 0x05: 2, 0x06: 4, 0x07: 6, 0x08: 6, 0x09: 8,
            0x0A: 2, 0x0B: 2, 0x0C: 6, 0x0D: 6, 0x0F: 16, 0x10: 2, 0x11: 2, 0x12: 6, 0x13: 6}
PT_INTS = (PT_INT8, PT_INT16, PT_INT32)
PT_NUMBER_VARS = (PT_GLOBAL_VAR, PT_LOCAL_VAR, PT_GLOBAL_NUMBER_ARRAY, PT_LOCAL_NUMBER_ARRAY)

# ---------------------------------------------------------------------------------------
# Engine limits / conventions
# ---------------------------------------------------------------------------------------
MAIN_SCRIPT_SIZE = 200_000      # CTheScripts::MAIN_SCRIPT_SIZE: bytes of main.scm read at Init
MISSION_SCRIPT_SIZE = 69_000    # ScriptSpace tail used for one mission at a time
MAX_USED_OBJECTS = 395          # CTheScripts::UsedObjectArray (asserts count < 395)
MAX_RESTART_POINTS = 10         # CRestart arrays; Add*RestartPoint has NO bounds check
NUM_LOCAL_VARS = 32             # CRunningScript::m_LocalVars = 32 locals + 2 timers
TIMERA, TIMERB = 32, 33         # SCRIPT_VAR_TIMERA/B, auto-incremented every frame
VANILLA_GLOBAL_SPACE = 43_800   # size of the retail global variable block (bytes)
SEG4_BUILD_NUMBER = 0x023E      # 2nd dword of segment id 4 as written by gta3sc (unused at runtime)
MAP_Z_LOW_LIMIT = -100.0        # CREATE_PLAYER with z <= this uses CWorld::FindGroundZForCoord

# Global variable indices used by R*'s main.scm (gta3sc config/gtasa/commandline.txt
# --expect-var, Sanny Builder data/sa/CustomVariables.ini).  CLEO scripts address these
# by index, so a replacement main.scm keeps them where the original had them.
GV_PLAYER_CHAR = 2      # player1      ($PLAYER_CHAR)   player index returned by CREATE_PLAYER
GV_PLAYER_ACTOR = 3     # scplayer     ($PLAYER_ACTOR)  ped pool handle
GV_PLAYER_GROUP = 11    # Players_Group($PLAYER_GROUP)
GV_ONMISSION = 409      # flag_player_on_mission ($ONMISSION)

# Weapon type (eWeaponType) -> weapon model id (data/weapon.dat column 5 / default.ide);
# REQUEST_MODEL + wait for it before GIVE_WEAPON_TO_CHAR (SASCM.INI note on 01B2).
WEAPON_MODELS = {
    1: 331, 2: 333, 3: 334, 4: 335, 5: 336, 6: 337, 7: 338, 8: 339, 9: 341,  # melee
    16: 342, 17: 343, 18: 344,                                               # thrown
    22: 346, 23: 347, 24: 348,                                               # pistols
    25: 349, 26: 350, 27: 351,                                               # shotguns
    28: 352, 29: 353, 32: 372,                                               # SMGs
    30: 355, 31: 356,                                                        # assault rifles
    33: 357, 34: 358,                                                        # rifles
    35: 359, 36: 360, 37: 361, 38: 362,                                      # heavy
    41: 365, 42: 366, 43: 367,                                               # spray/extinguisher/camera
}
NUM_WEATHERS = 23               # eWeatherType 0..22 (timecyc.dat columns)

# ---------------------------------------------------------------------------------------
# Opcode table.  Argument kinds:
#   i  int input (literal or int variable)     f  float input (literal or variable)
#   I  int literal only                        l  label (absolute offset in MAIN)
#   g  global variable (input, e.g. flag var)  v  local variable (input)
#   o  output variable (global or local)       t  TEXT_LABEL literal (0x09, 8 bytes)
#   s  STRING literal (0x0E, var-length)
# Counts/types verified against Sanny Builder sa.json + SASCM.INI and gta3sc commands.xml
# (tests/test_scm.py) and, where reversed, against the gta-reversed handler signature.
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OpDef:
    name: str
    kinds: tuple
    condition: bool = False
    str_max: int = 127          # longest STRING ('s') argument accepted


def _op(name: str, kinds: str = "", condition: bool = False, str_max: int = 127) -> OpDef:
    return OpDef(name, tuple(kinds.split()) if kinds else (), condition, str_max)


OPCODES: dict[int, OpDef] = {
    0x0000: _op("NOP"),
    0x0001: _op("WAIT", "i"),
    0x0002: _op("GOTO", "l"),
    0x0004: _op("SET_VAR_INT", "g I"),
    0x0006: _op("SET_LVAR_INT", "v I"),
    0x0019: _op("IS_INT_LVAR_GREATER_THAN_NUMBER", "v I", True),
    0x004D: _op("GOTO_IF_FALSE", "l"),
    0x0053: _op("CREATE_PLAYER", "i f f f o"),
    0x00C0: _op("SET_TIME_OF_DAY", "i i"),
    0x00D6: _op("ANDOR", "I"),                      # Sanny: IF
    0x0109: _op("ADD_SCORE", "i i"),
    0x0111: _op("SET_DEATHARREST_STATE", "i"),
    0x016A: _op("DO_FADE", "i i"),
    0x016C: _op("ADD_HOSPITAL_RESTART", "f f f f i"),
    0x016D: _op("ADD_POLICE_RESTART", "f f f f i"),
    0x0173: _op("SET_CHAR_HEADING", "i f"),
    0x0180: _op("DECLARE_MISSION_FLAG", "g"),
    0x01B2: _op("GIVE_WEAPON_TO_CHAR", "i i i"),
    0x01B4: _op("SET_PLAYER_CONTROL", "i i"),
    0x01B6: _op("FORCE_WEATHER_NOW", "i"),
    0x01B7: _op("RELEASE_WEATHER"),
    0x01EB: _op("SET_CAR_DENSITY_MULTIPLIER", "f"),
    0x01F0: _op("SET_MAX_WANTED_LEVEL", "i"),
    0x01F5: _op("GET_PLAYER_CHAR", "i o"),
    0x0247: _op("REQUEST_MODEL", "i"),
    0x0248: _op("HAS_MODEL_LOADED", "i", True),
    0x0249: _op("MARK_MODEL_AS_NO_LONGER_NEEDED", "i"),
    0x02EB: _op("RESTORE_CAMERA_JUMPCUT"),
    0x0373: _op("SET_CAMERA_BEHIND_PLAYER"),
    0x038B: _op("LOAD_ALL_MODELS_NOW"),
    0x03A4: _op("SCRIPT_NAME", "t"),
    0x03CB: _op("LOAD_SCENE", "f f f"),
    0x03DE: _op("SET_PED_DENSITY_MULTIPLIER", "f"),
    0x04BB: _op("SET_AREA_VISIBLE", "i"),
    0x04E4: _op("REQUEST_COLLISION", "f f"),
    0x0629: _op("SET_INT_STAT", "i i"),
    0x070D: _op("BUILD_PLAYER_MODEL", "i"),
    0x07AF: _op("GET_PLAYER_GROUP", "i o"),
    0x0860: _op("SET_CHAR_AREA_VISIBLE", "i i"),
    # vanilla handler not reversed: its string buffer size is unknown, R*'s own longest
    # argument is "SNEAKERBINCBLK" (14) -> stay below 16
    0x087B: _op("GIVE_PLAYER_CLOTHES_OUTSIDE_SHOP", "i s s i", str_max=15),
}
OPCODE_BY_NAME = {d.name: op for op, d in OPCODES.items()}

# ---------------------------------------------------------------------------------------
# Argument values
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GVar:
    """Global variable, Sanny notation $index; stored as byte offset 4*index (>= 8)."""
    index: int
    name: str = ""

    @property
    def offset(self) -> int:
        return 4 * self.index


@dataclass(frozen=True)
class LVar:
    """Local variable of the current script, Sanny notation index@ (32 = TIMERA)."""
    index: int


@dataclass(frozen=True)
class Label:
    name: str


@dataclass(frozen=True)
class Raw:
    """Pre-typed argument for opcodes outside OPCODES: (type byte, payload bytes)."""
    ptype: int
    payload: bytes = b""


def _check_int32(v: int) -> int:
    if isinstance(v, bool):
        v = int(v)
    if not isinstance(v, int):
        raise TypeError(f"int argument expected, got {v!r}")
    if not -2**31 <= v < 2**31:
        raise ValueError(f"int argument out of int32 range: {v}")
    return v


def encode_int(v: int) -> bytes:
    """Smallest immediate int encoding, as R*'s compiler/gta3sc/Sanny emit it.
    CollectParameters sign-extends 0x04/0x05 to int32 (RunningScript.cpp)."""
    v = _check_int32(v)
    if -128 <= v <= 127:
        return bytes([PT_INT8]) + struct.pack("<b", v)
    if -32768 <= v <= 32767:
        return bytes([PT_INT16]) + struct.pack("<h", v)
    return bytes([PT_INT32]) + struct.pack("<i", v)


def encode_float(v: float) -> bytes:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError(f"float argument expected, got {v!r}")
    v = float(v)
    if not math.isfinite(v) or abs(v) > 3.4e38:
        raise ValueError(f"float argument not representable: {v}")
    return bytes([PT_FLOAT]) + struct.pack("<f", v)


def encode_text_label8(s: str) -> bytes:
    """TEXT_LABEL: type 0x09 + 8 bytes.  At most 7 chars so the engine always finds a NUL
    (SCRIPT_NAME asserts name.size() < 8, CRunningScript::SetName)."""
    b = _ascii(s).upper()
    if not 1 <= len(b) <= 7:
        raise ValueError(f"text label must be 1..7 ASCII chars: {s!r}")
    return bytes([PT_TEXT_LABEL8]) + b.ljust(8, b"\0")


def encode_varlen_string(s: str, max_len: int = 127) -> bytes:
    """STRING: type 0x0E + uint8 length + bytes (no NUL).  Length is read as a signed byte
    in ReadTextLabelFromScript, hence <= 127."""
    b = _ascii(s).upper()
    if not 1 <= len(b) <= min(max_len, 127):
        raise ValueError(f"string must be 1..{min(max_len, 127)} ASCII chars: {s!r}")
    return bytes([PT_VARLEN_STRING, len(b)]) + b


def _ascii(s: str) -> bytes:
    if not isinstance(s, str):
        raise TypeError(f"string argument expected, got {s!r}")
    b = s.encode("ascii")
    if b"\0" in b:
        raise ValueError("NUL in string argument")
    return b


def _encode_var(v) -> bytes:
    if isinstance(v, GVar):
        return bytes([PT_GLOBAL_VAR]) + struct.pack("<H", v.offset)
    if isinstance(v, LVar):
        return bytes([PT_LOCAL_VAR]) + struct.pack("<H", v.index)
    raise TypeError(f"variable expected, got {v!r}")


# ---------------------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------------------


@dataclass
class _Ins:
    opcode: int
    negate: bool
    parts: list          # bytes or ("label", name)


class ScmAssembler:
    """Collects MAIN-script instructions; labels resolve to absolute ScriptSpace offsets
    (positive GOTO targets are absolute in MAIN, CRunningScript::UpdatePC)."""

    LABEL_SIZE = 5       # type 0x01 + int32 (always the 4-byte form, like gta3sc)

    def __init__(self, global_space: int = VANILLA_GLOBAL_SPACE):
        if global_space % 4 or not 4 <= global_space <= 65536 - 8 - 4:
            raise ValueError("global_space must be a multiple of 4 and fit uint16 offsets")
        self.global_space = global_space
        self._items: list = []
        self._labels: set[str] = set()
        self._globals: dict[str, GVar] = {}
        self._auto_label = 0

    # -- variables ------------------------------------------------------------------
    def gvar(self, name: str, index: int | None = None) -> GVar:
        """Named global ($index).  Index 0/1 are header bytes (offset >= 8 is required by
        GetPointerToScriptVariable).  Without an index the lowest free one is used."""
        if name in self._globals:
            g = self._globals[name]
            if index is not None and g.index != index:
                raise ValueError(f"global {name} already at ${g.index}")
            return g
        used = {g.index for g in self._globals.values()}
        if index is None:
            index = 2
            while index in used:
                index += 1
        if index in used:
            raise ValueError(f"global index ${index} already used")
        if not 2 <= index or 4 * index + 4 > 8 + self.global_space:
            raise ValueError(f"global ${index} outside the variable space")
        g = GVar(index, name)
        self._globals[name] = g
        return g

    @property
    def globals(self) -> dict[str, GVar]:
        return dict(self._globals)

    @staticmethod
    def lvar(index: int) -> LVar:
        if not 0 <= index < NUM_LOCAL_VARS + 2:
            raise ValueError("local variable index must be 0..33 (32/33 = TIMERA/TIMERB)")
        return LVar(index)

    # -- labels -----------------------------------------------------------------------
    def new_label(self, prefix: str = "L") -> Label:
        self._auto_label += 1
        return Label(f"__{prefix}_{self._auto_label}")

    def label(self, lab: Label | str) -> Label:
        lab = lab if isinstance(lab, Label) else Label(lab)
        if lab.name in self._labels:
            raise ValueError(f"duplicate label {lab.name}")
        self._labels.add(lab.name)
        self._items.append(("label", lab.name))
        return lab

    # -- instructions -----------------------------------------------------------------
    def emit(self, opcode: int | str, *args, negate: bool = False) -> None:
        """Emit one instruction; arguments are validated/encoded per OPCODES[opcode]."""
        if isinstance(opcode, str):
            opcode = OPCODE_BY_NAME[opcode]
        if opcode not in OPCODES:
            raise KeyError(f"opcode {opcode:04X} not in OPCODES (use emit_raw)")
        d = OPCODES[opcode]
        if negate and not d.condition:
            raise ValueError(f"NOT flag on non-condition {d.name}")
        if len(args) != len(d.kinds):
            raise TypeError(f"{d.name} takes {len(d.kinds)} arguments, got {len(args)}")
        parts = [self._encode(k, a, d) for k, a in zip(d.kinds, args)]
        self._items.append(("ins", _Ins(opcode, negate, parts)))

    def emit_raw(self, opcode: int, *args: Raw) -> None:
        """Escape hatch for opcodes not in OPCODES: caller supplies typed arguments and is
        responsible for the count/types the game expects."""
        if not 0 <= opcode <= 0x7FFF:
            raise ValueError("opcode must be 0..0x7FFF")
        parts = []
        for a in args:
            if not isinstance(a, Raw):
                raise TypeError("emit_raw takes Raw arguments")
            parts.append(bytes([a.ptype]) + a.payload)
        self._items.append(("ins", _Ins(opcode, False, parts)))

    def _encode(self, kind: str, a, d: OpDef):
        opname = d.name
        if kind == "i":
            if isinstance(a, (GVar, LVar)):
                return _encode_var(a)
            return encode_int(a)
        if kind == "I":
            return encode_int(a)
        if kind == "f":
            if isinstance(a, (GVar, LVar)):
                return _encode_var(a)
            return encode_float(a)
        if kind == "l":
            if isinstance(a, str):
                a = Label(a)
            if not isinstance(a, Label):
                raise TypeError(f"{opname}: label expected, got {a!r}")
            return ("label", a.name)
        if kind == "g":
            if not isinstance(a, GVar):
                raise TypeError(f"{opname}: global variable expected, got {a!r}")
            return _encode_var(a)
        if kind == "v":
            if not isinstance(a, LVar):
                raise TypeError(f"{opname}: local variable expected, got {a!r}")
            return _encode_var(a)
        if kind == "o":
            if not isinstance(a, (GVar, LVar)):
                raise TypeError(f"{opname}: output variable expected, got {a!r}")
            return _encode_var(a)
        if kind == "t":
            return encode_text_label8(a)
        if kind == "s":
            return encode_varlen_string(a, d.str_max)
        raise AssertionError(kind)

    # -- convenience --------------------------------------------------------------------
    def wait(self, ms) -> None:
        self.emit(0x0001, ms)

    def goto(self, lab: Label | str) -> None:
        self.emit(0x0002, lab)

    def if_false_goto(self, opcode: int | str, *args, target: Label | str, negate=False):
        """ANDOR 0 (single condition) + condition + GOTO_IF_FALSE target."""
        self.emit(0x00D6, 0)
        self.emit(opcode, *args, negate=negate)
        self.emit(0x004D, target)

    # -- assembly ---------------------------------------------------------------------
    def code_size(self) -> int:
        n = 0
        for kind, it in self._items:
            if kind == "ins":
                n += 2 + sum(self.LABEL_SIZE if isinstance(p, tuple) else len(p) for p in it.parts)
        return n

    def assemble(self, base: int) -> bytes:
        """Return the code bytes as they will sit at ScriptSpace[base:]."""
        addr: dict[str, int] = {}
        pos = base
        for kind, it in self._items:
            if kind == "label":
                addr[it] = pos
            else:
                pos += 2 + sum(self.LABEL_SIZE if isinstance(p, tuple) else len(p) for p in it.parts)
        out = bytearray()
        for kind, it in self._items:
            if kind != "ins":
                continue
            out += struct.pack("<H", it.opcode | (0x8000 if it.negate else 0))
            for p in it.parts:
                if isinstance(p, tuple):
                    if p[1] not in addr:
                        raise KeyError(f"undefined label {p[1]}")
                    target = addr[p[1]]
                    if target <= 0:
                        raise ValueError("label at offset 0")
                    out += bytes([PT_INT32]) + struct.pack("<i", target)
                else:
                    out += p
        assert len(out) == pos - base
        return bytes(out)


# ---------------------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------------------


def _goto(target: int) -> bytes:
    return b"\x02\x00\x01" + struct.pack("<i", target)


def header_size(global_space: int, n_models: int = 0, aaa_stream: bool = False) -> int:
    return (8 + global_space                      # 's' globals
            + 8 + 4 + 24 * (1 + n_models)         # 0 used objects
            + 8 + 16                              # 1 script file info, no missions
            + 8 + 8 + (28 if aaa_stream else 0)   # 2 streamed scripts
            + 8 + 4                               # 3 unknown
            + 8 + 8)                              # 4 extra info


def build_main_scm(asm: ScmAssembler, *, models: Sequence[str] = (), aaa_stream: bool = False,
                   build_number: int = SEG4_BUILD_NUMBER) -> bytes:
    """Header + MAIN code.  No mission scripts.  ``aaa_stream=False`` (default) writes an
    empty streamed-script table, so CStreamedScripts::ReadStreamedScriptData never looks
    anything up in script.img (whatever script.img the user has).  ``aaa_stream=True``
    reproduces gta3sc/Sanny's default single 'AAA' entry (requires aaa.scm in script.img,
    otherwise the lookup returns -1 and the vanilla code writes m_aScripts[-1])."""
    gs = asm.global_space
    if len(models) + 1 >= MAX_USED_OBJECTS:
        raise ValueError("too many used objects")
    hsize = header_size(gs, len(models), aaa_stream)
    code = asm.assemble(hsize)
    total = hsize + len(code)
    if total > MAIN_SCRIPT_SIZE:
        raise ValueError(f"main.scm is {total} bytes > MAIN_SCRIPT_SIZE {MAIN_SCRIPT_SIZE}")

    # Each segment: GOTO <offset of the next segment>, id byte, payload.
    off_objects = 8 + gs
    off_info = off_objects + 8 + 4 + 24 * (1 + len(models))
    off_streamed = off_info + 8 + 16
    off_unknown = off_streamed + 8 + 8 + (28 if aaa_stream else 0)
    off_extra = off_unknown + 8 + 4
    code_off = off_extra + 8 + 8
    out = bytearray()
    # id 's': global variable space [8, off_objects), zero-filled
    out += _goto(off_objects) + b"s" + bytes(gs)
    # id 0: used objects; count includes the unused empty entry 0
    out += _goto(off_info) + b"\x00" + struct.pack("<I", 1 + len(models)) + bytes(24)
    for m in models:
        b = _ascii(m).upper()
        if not 1 <= len(b) <= 23:
            raise ValueError(f"model name length: {m!r}")
        out += b.ljust(24, b"\0")
    # id 1: main size, largest mission, #missions, #exclusive, largest mission locals
    out += _goto(off_streamed) + b"\x01" + struct.pack("<IIHHI", total, 0, 0, 0, 0)
    # id 2: largest streamed script, #streamed scripts, [name[20], offset, size]
    out += _goto(off_unknown) + b"\x02" + struct.pack("<II", 0, 1 if aaa_stream else 0)
    if aaa_stream:
        out += b"AAA".ljust(20, b"\0") + struct.pack("<II", 0, 8)
    # id 3: unknown dword (0)
    out += _goto(off_extra) + b"\x03" + struct.pack("<I", 0)
    # id 4: global space size, "build number" (both unused at runtime)
    out += _goto(code_off) + b"\x04" + struct.pack("<II", gs, build_number)
    assert len(out) == hsize == code_off
    out += code
    return bytes(out)


# ---------------------------------------------------------------------------------------
# Inspection helpers (engine-style header walk + linear disassembly)
# ---------------------------------------------------------------------------------------


@dataclass
class ScmHeader:
    segments: list            # [(offset, id, next_offset)]
    global_space_end: int     # == GetSizeOfVariableSpace(): first segment's GOTO target
    models: list
    main_size: int
    largest_mission: int
    num_missions: int
    num_exclusive: int
    largest_mission_locals: int
    mission_offsets: list
    largest_streamed: int
    streamed: list            # [(name, offset, size)]
    unknown3: int
    seg4_global_space: int
    seg4_build: int
    code_offset: int


def parse_header(data: bytes) -> ScmHeader:
    """Walk the six GOTO segments exactly like the PC loaders (target of each GOTO is the
    next segment; ids 's',0,1,2,3,4 in that order)."""
    segs = []
    off = 0
    for expect in (ord("s"), 0, 1, 2, 3, 4):
        if data[off:off + 3] != b"\x02\x00\x01":
            raise ValueError(f"segment at {off} does not start with GOTO int32")
        nxt = struct.unpack_from("<i", data, off + 3)[0]
        if data[off + 7] != expect or not off + 8 <= nxt <= len(data):
            raise ValueError(f"bad segment at {off}: id {data[off + 7]} next {nxt}")
        segs.append((off, data[off + 7], nxt))
        off = nxt
    (s0, s1, s2, s3, s4, s5) = segs
    nm = struct.unpack_from("<I", data, s1[0] + 8)[0]
    models = [data[s1[0] + 12 + 24 * i:s1[0] + 36 + 24 * i].split(b"\0")[0].decode("latin-1")
              for i in range(nm)]
    main_size, largest, nmis, nexc, locs = struct.unpack_from("<IIHHI", data, s2[0] + 8)
    mis = list(struct.unpack_from(f"<{nmis}i", data, s2[0] + 24)) if nmis else []
    lst, nst = struct.unpack_from("<II", data, s3[0] + 8)
    streamed = []
    for i in range(nst):
        o = s3[0] + 16 + 28 * i
        streamed.append((data[o:o + 20].split(b"\0")[0].decode("latin-1"),)
                        + struct.unpack_from("<II", data, o + 20))
    unk = struct.unpack_from("<I", data, s4[0] + 8)[0]
    gsz, bld = struct.unpack_from("<II", data, s5[0] + 8)
    return ScmHeader(segs, s0[2], models, main_size, largest, nmis, nexc, locs, mis, lst,
                     streamed, unk, gsz, bld, s5[2])


@dataclass
class Instruction:
    offset: int
    opcode: int
    negate: bool
    args: list                # [(ptype, value)]

    @property
    def name(self) -> str:
        d = OPCODES.get(self.opcode)
        return d.name if d else f"OP_{self.opcode:04X}"


def decode_arg(data: bytes, pos: int):
    """-> ((ptype, value), new_pos), following CollectParameters/ReadTextLabelFromScript."""
    t = data[pos]
    pos += 1
    if t == PT_INT8:
        return (t, struct.unpack_from("<b", data, pos)[0]), pos + 1
    if t == PT_INT16:
        return (t, struct.unpack_from("<h", data, pos)[0]), pos + 2
    if t == PT_INT32:
        return (t, struct.unpack_from("<i", data, pos)[0]), pos + 4
    if t == PT_FLOAT:
        return (t, struct.unpack_from("<f", data, pos)[0]), pos + 4
    if t in (PT_GLOBAL_VAR, PT_LOCAL_VAR, 0x0A, 0x0B, 0x10, 0x11):
        return (t, struct.unpack_from("<H", data, pos)[0]), pos + 2
    if t in (0x07, 0x08, 0x0C, 0x0D, 0x12, 0x13):
        return (t, struct.unpack_from("<HHBB", data, pos)), pos + 6
    if t == PT_TEXT_LABEL8:
        return (t, data[pos:pos + 8].split(b"\0")[0].decode("latin-1")), pos + 8
    if t == PT_TEXT_LABEL16:
        return (t, data[pos:pos + 16].split(b"\0")[0].decode("latin-1")), pos + 16
    if t == PT_VARLEN_STRING:
        n = data[pos]
        return (t, data[pos + 1:pos + 1 + n].decode("latin-1")), pos + 1 + n
    raise ValueError(f"unknown parameter type 0x{t:02X} at {pos - 1}")


def disassemble(data: bytes, start: int | None = None, end: int | None = None) -> list:
    """Linear sweep of MAIN code using the OPCODES argument counts."""
    if start is None:
        start = parse_header(data).code_offset
    end = len(data) if end is None else end
    out = []
    pos = start
    while pos < end:
        word = struct.unpack_from("<H", data, pos)[0]
        op = word & 0x7FFF
        if op not in OPCODES:
            raise ValueError(f"unknown opcode {op:04X} at {pos}")
        p = pos + 2
        args = []
        for _ in OPCODES[op].kinds:
            a, p = decode_arg(data, p)
            args.append(a)
        out.append(Instruction(pos, op, bool(word & 0x8000), args))
        pos = p
    return out


# ---------------------------------------------------------------------------------------
# Free-roam MAIN script
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FreeroamVars:
    player: GVar    # player index (0) as returned by CREATE_PLAYER
    actor: GVar     # player's ped handle (GET_PLAYER_CHAR)
    group: GVar     # player's group (GET_PLAYER_GROUP)
    onmission: GVar


DEFAULT_CLOTHES = (  # exactly what R*'s main.sc gives CJ at a new game (texture, model, body part)
    ("VEST", "VEST", 0),
    ("JEANSDENIM", "JEANS", 2),
    ("SNEAKERBINCBLK", "SNEAKER", 3),
    ("PLAYER_FACE", "HEAD", 1),
)

Extra = Callable[[ScmAssembler, FreeroamVars], None] | Sequence


def _point(p, what: str, allow_auto_z: bool = False):
    if len(p) != 4:
        raise ValueError(f"{what}: expected (x, y, z, heading)")
    x, y, z, h = p
    for v in (x, y, h) + (() if (z is None and allow_auto_z) else (z,)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{what}: non-finite or non-numeric coordinate {p!r}")
    if not (-3000.0 <= x <= 3000.0 and -3000.0 <= y <= 3000.0):
        raise ValueError(f"{what}: x/y outside the SA world (+-3000): {p!r}")
    if z is not None and not MAP_Z_LOW_LIMIT < z < 2000.0:
        raise ValueError(f"{what}: z must be above {MAP_Z_LOW_LIMIT}: {p!r}")
    return float(x), float(y), (None if z is None else float(z)), float(h) % 360.0


def build_freeroam_scm(spawn: Sequence[float], hospitals: Sequence[Sequence[float]],
                       police: Sequence[Sequence[float]], weather: int | None = 0,
                       hour: int = 12, money: int = 0, extras: Iterable[Extra] = (), *,
                       minute: int = 0, weapons: Sequence[Sequence[int]] = (),
                       max_wanted_level: int = 6, ped_density: float = 1.0,
                       car_density: float = 1.0, release_weather: bool = False,
                       clothes: bool | Sequence[tuple] = True, unlock_all_cities: bool = True,
                       fade_in_ms: int = 1000, loop_wait_ms: int = 250,
                       weapon_load_timeout_ms: int = 5000,
                       global_space: int = VANILLA_GLOBAL_SPACE,
                       aaa_stream: bool = False) -> bytes:
    """Build the free-roam main.scm (bytes).

    spawn      (x, y, z, heading): z = ground height at the spawn (CREATE_PLAYER lifts the
               ped by its centre-of-mass offset); z=None lets the game find the ground
               (z = -100 -> CWorld::FindGroundZForCoord after LOAD_SCENE).
    hospitals  1..10 (x, y, z, heading) wasted-respawn points (ground z, required).
    police     1..10 (x, y, z, heading) busted-respawn points.
    weather    eWeatherType 0..22 forced with FORCE_WEATHER_NOW (None: leave the game's
               weather cycle alone); release_weather=True releases it again right away
               (start in that weather, then cycle by weather region).
    hour, minute  in-game clock.
    money      ADD_SCORE at start (CJ starts with $0).
    weapons    [(weapon_type, ammo)] or [(weapon_type, ammo, model_id)]; models are
               requested/loaded first, with a TIMERA timeout so a missing model cannot
               block the script.
    extras     items run once after setup, before the main loop: callables
               ``fn(asm, vars)`` or instruction tuples ``(opcode_or_name, *args)``.
    """
    sx, sy, sz, sh = _point(spawn, "spawn", allow_auto_z=True)
    hosp = [_point(p, "hospital") for p in hospitals]
    pol = [_point(p, "police") for p in police]
    for what, pts in (("hospitals", hosp), ("police", pol)):
        if not 1 <= len(pts) <= MAX_RESTART_POINTS:
            raise ValueError(f"{what}: need 1..{MAX_RESTART_POINTS} restart points "
                             f"(none -> CRestart returns an uninitialised position; "
                             f">{MAX_RESTART_POINTS} overflows CRestart's arrays)")
    if weather is not None and not (isinstance(weather, int) and 0 <= weather < NUM_WEATHERS):
        raise ValueError("weather must be None or an eWeatherType 0..22")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("hour 0..23, minute 0..59")
    if not 0 <= max_wanted_level <= 6:
        raise ValueError("max_wanted_level 0..6 (CWanted::SetMaximumWantedLevel)")
    for v in (ped_density, car_density):
        if not (isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 10):
            raise ValueError("density multipliers must be finite, 0..10")
    if not (0 <= fade_in_ms <= 60_000 and 0 <= loop_wait_ms <= 60_000):
        raise ValueError("fade_in_ms / loop_wait_ms out of range")
    _check_int32(money)

    wlist = []
    for w in weapons:
        if len(w) == 2:
            wt, ammo = w
            if wt not in WEAPON_MODELS:
                raise ValueError(f"no model known for weapon type {wt}; pass (type, ammo, model)")
            model = WEAPON_MODELS[wt]
        elif len(w) == 3:
            wt, ammo, model = w
        else:
            raise ValueError("weapons: (weapon_type, ammo[, model_id])")
        if not (1 <= wt <= 46 and 0 < ammo < 100_000 and 0 < model < 20_000):
            raise ValueError(f"bad weapon entry {w!r}")
        wlist.append((int(wt), int(ammo), int(model)))

    if clothes is True:
        clothes = DEFAULT_CLOTHES
    elif clothes is False or clothes is None:
        clothes = ()

    a = ScmAssembler(global_space)
    v = FreeroamVars(a.gvar("PLAYER_CHAR", GV_PLAYER_CHAR), a.gvar("PLAYER_ACTOR", GV_PLAYER_ACTOR),
                     a.gvar("PLAYER_GROUP", GV_PLAYER_GROUP), a.gvar("ONMISSION", GV_ONMISSION))

    # --- frame 0: CGame::Initialise / ReInitGameObjectVariables run MAIN once
    # (StartTestScript + Process) right before TheCamera.Process() and the first
    # CGame::Process(); the player must exist before the first WAIT.
    a.emit("SCRIPT_NAME", "MAIN")
    a.emit("DO_FADE", 0, 0)                      # 0 ms, FADE_OUT: black while streaming
    a.emit("SET_MAX_WANTED_LEVEL", max_wanted_level)
    a.emit("SET_DEATHARREST_STATE", 0)           # as R* main.sc (MAIN is not a mission)
    a.emit("SET_TIME_OF_DAY", hour, minute)
    if unlock_all_cities:
        # STAT_CITY_UNLOCKED (181) = 4: CGameLogic::SetPlayerWantedLevelForForbiddenTerritories
        # gives 4 stars inside the hard-coded SF/LV polygons while it is <= 1 / <= 0.
        a.emit("SET_INT_STAT", 181, 4)
    lz = 0.0 if sz is None else sz
    a.emit("REQUEST_COLLISION", sx, sy)
    a.emit("LOAD_SCENE", sx, sy, lz)
    a.emit("CREATE_PLAYER", 0, sx, sy, MAP_Z_LOW_LIMIT if sz is None else sz, v.player)
    a.emit("GET_PLAYER_CHAR", v.player, v.actor)
    a.emit("GET_PLAYER_GROUP", v.player, v.group)
    a.emit("SET_CHAR_HEADING", v.actor, sh)
    a.emit("SET_CAMERA_BEHIND_PLAYER")
    if weather is not None:
        a.emit("FORCE_WEATHER_NOW", weather)
    a.emit("DECLARE_MISSION_FLAG", v.onmission)
    a.emit("SET_VAR_INT", v.onmission, 0)
    a.wait(0)

    # --- frame 1+: world is running, player/camera exist
    a.emit("SET_AREA_VISIBLE", 0)
    a.emit("SET_CHAR_AREA_VISIBLE", v.actor, 0)
    for x, y, z, h in hosp:
        a.emit("ADD_HOSPITAL_RESTART", x, y, z, h, 0)
    for x, y, z, h in pol:
        a.emit("ADD_POLICE_RESTART", x, y, z, h, 0)
    a.emit("SET_PED_DENSITY_MULTIPLIER", float(ped_density))
    a.emit("SET_CAR_DENSITY_MULTIPLIER", float(car_density))
    for tex, model, part in clothes:
        a.emit("GIVE_PLAYER_CLOTHES_OUTSIDE_SHOP", v.player, tex, model, part)
    if clothes:
        a.emit("BUILD_PLAYER_MODEL", v.player)
    a.emit("RESTORE_CAMERA_JUMPCUT")
    a.emit("SET_CAMERA_BEHIND_PLAYER")
    a.emit("SET_PLAYER_CONTROL", v.player, 1)
    a.emit("DO_FADE", fade_in_ms, 1)             # FADE_IN
    if weather is not None and release_weather:
        a.emit("RELEASE_WEATHER")
    if money:
        a.emit("ADD_SCORE", v.player, money)

    if wlist:
        models = sorted({m for _, _, m in wlist})
        wait_l, check_l, release_l = Label("WPN_WAIT"), Label("WPN_CHECK"), Label("WPN_RELEASE")
        for m in models:
            a.emit("REQUEST_MODEL", m)
        a.emit("LOAD_ALL_MODELS_NOW")
        a.emit("SET_LVAR_INT", LVar(TIMERA), 0)
        a.label(wait_l)
        a.wait(0)
        a.if_false_goto("IS_INT_LVAR_GREATER_THAN_NUMBER", LVar(TIMERA),
                        weapon_load_timeout_ms, target=check_l)
        a.goto(release_l)
        a.label(check_l)
        for m in models:
            a.if_false_goto("HAS_MODEL_LOADED", m, target=wait_l)
        for wt, ammo, _ in wlist:
            a.emit("GIVE_WEAPON_TO_CHAR", v.actor, wt, ammo)
        a.label(release_l)
        for m in models:
            a.emit("MARK_MODEL_AS_NO_LONGER_NEEDED", m)

    for ex in extras:
        if callable(ex):
            ex(a, v)
        else:
            a.emit(*ex)

    loop = a.label("MAIN_LOOP")
    a.wait(loop_wait_ms)
    a.goto(loop)
    return build_main_scm(a, aaa_stream=aaa_stream)


def write_freeroam_scm(path: str, *args, **kwargs) -> int:
    """build_freeroam_scm(...) and write it to ``path`` (data/script/main.scm); returns size."""
    data = build_freeroam_scm(*args, **kwargs)
    with open(path, "wb") as f:
        f.write(data)
    return len(data)
