"""Tests for tools/gta/scm.py (SA main.scm assembler + free-roam MAIN script).

Nobody can test in-game, so the generated main.scm is checked against:
  1. an independent reader in this file that walks the header the way the PC loaders do
     (VC-style GOTO hops as decompiled in reVC, and gta-reversed's GetSCMChunk walk) and
     decodes the code with its own argument decoder (eScriptParameterType);
  2. REF: opcode/argument table transcribed from Sanny Builder's library sa.json (v1.68),
     cross-checked with Sanny's SASCM.INI and gta3sc's config/gtasa/commands.xml; plus the
     live database files when available under $SCM_REF_DIR;
  3. thelink2012/gta3sc (if a gta3sc binary is available, $GTA3SC): its decompiler must
     parse our file, and its compiler must turn a GTA3script transcription of our
     instruction stream into a byte-identical main.scm.
Engine facts and sources: docs/notes/scm.md.

Run: python3 tests/test_scm.py   (or pytest tests/test_scm.py)
"""
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.gta import scm  # noqa: E402

REF_DIR = os.environ.get(
    "SCM_REF_DIR", "/tmp/claude-0/-home-user-ila/5b6b71aa-4af7-540b-9036-4dec5b516689/scratchpad/scm_ref")
GTA3SC = os.environ.get("GTA3SC", os.path.join(REF_DIR, "gta3sc_build", "gta3sc"))
SA_JSON = os.path.join(REF_DIR, "sblib", "sa_sa.json")
SASCM_INI = os.path.join(REF_DIR, "sbdata", "SASCM.INI")
G3_COMMANDS = os.path.join(REF_DIR, "gta3sc", "config_gtasa_commands.xml")

# opcode -> (gta3sc/R* name, Sanny library name, argument classes in stream order)
# classes: int (int literal or variable), intlit, float, label, gvar, lvar, out (output
# variable), text8 (TEXT_LABEL, type 0x09), string (STRING, type 0x0E)
REF = {
    0x0000: ("NOP", "NOP", []),
    0x0001: ("WAIT", "WAIT", ["int"]),
    0x0002: ("GOTO", "GOTO", ["label"]),
    0x0004: ("SET_VAR_INT", "SET_VAR_INT", ["gvar", "intlit"]),
    0x0006: ("SET_LVAR_INT", "SET_LVAR_INT", ["lvar", "intlit"]),
    0x0019: ("IS_INT_LVAR_GREATER_THAN_NUMBER", "IS_INT_LVAR_GREATER_THAN_NUMBER", ["lvar", "intlit"]),
    0x004D: ("GOTO_IF_FALSE", "GOTO_IF_FALSE", ["label"]),
    0x0053: ("CREATE_PLAYER", "CREATE_PLAYER", ["int", "float", "float", "float", "out"]),
    0x00C0: ("SET_TIME_OF_DAY", "SET_TIME_OF_DAY", ["int", "int"]),
    0x00D6: ("ANDOR", "IF", ["int"]),
    0x0109: ("ADD_SCORE", "ADD_SCORE", ["int", "int"]),
    0x0111: ("SET_DEATHARREST_STATE", "SET_DEATHARREST_STATE", ["int"]),
    0x016A: ("DO_FADE", "DO_FADE", ["int", "int"]),
    0x016C: ("ADD_HOSPITAL_RESTART", "ADD_HOSPITAL_RESTART", ["float", "float", "float", "float", "int"]),
    0x016D: ("ADD_POLICE_RESTART", "ADD_POLICE_RESTART", ["float", "float", "float", "float", "int"]),
    0x0173: ("SET_CHAR_HEADING", "SET_CHAR_HEADING", ["int", "float"]),
    0x0180: ("DECLARE_MISSION_FLAG", "DECLARE_MISSION_FLAG", ["gvar"]),
    0x01B2: ("GIVE_WEAPON_TO_CHAR", "GIVE_WEAPON_TO_CHAR", ["int", "int", "int"]),
    0x01B4: ("SET_PLAYER_CONTROL", "SET_PLAYER_CONTROL", ["int", "int"]),
    0x01B6: ("FORCE_WEATHER_NOW", "FORCE_WEATHER_NOW", ["int"]),
    0x01B7: ("RELEASE_WEATHER", "RELEASE_WEATHER", []),
    0x01EB: ("SET_CAR_DENSITY_MULTIPLIER", "SET_CAR_DENSITY_MULTIPLIER", ["float"]),
    0x01F0: ("SET_MAX_WANTED_LEVEL", "SET_MAX_WANTED_LEVEL", ["int"]),
    0x01F5: ("GET_PLAYER_CHAR", "GET_PLAYER_CHAR", ["int", "out"]),
    0x0247: ("REQUEST_MODEL", "REQUEST_MODEL", ["int"]),
    0x0248: ("HAS_MODEL_LOADED", "HAS_MODEL_LOADED", ["int"]),
    0x0249: ("MARK_MODEL_AS_NO_LONGER_NEEDED", "MARK_MODEL_AS_NO_LONGER_NEEDED", ["int"]),
    0x02EB: ("RESTORE_CAMERA_JUMPCUT", "RESTORE_CAMERA_JUMPCUT", []),
    0x0373: ("SET_CAMERA_BEHIND_PLAYER", "SET_CAMERA_BEHIND_PLAYER", []),
    0x038B: ("LOAD_ALL_MODELS_NOW", "LOAD_ALL_MODELS_NOW", []),
    0x03A4: ("SCRIPT_NAME", "SCRIPT_NAME", ["text8"]),
    0x03CB: ("LOAD_SCENE", "LOAD_SCENE", ["float", "float", "float"]),
    0x03DE: ("SET_PED_DENSITY_MULTIPLIER", "SET_PED_DENSITY_MULTIPLIER", ["float"]),
    0x04BB: ("SET_AREA_VISIBLE", "SET_AREA_VISIBLE", ["int"]),
    0x04E4: ("REQUEST_COLLISION", "REQUEST_COLLISION", ["float", "float"]),
    0x0629: ("SET_INT_STAT", "SET_INT_STAT", ["int", "int"]),
    0x070D: ("BUILD_PLAYER_MODEL", "BUILD_PLAYER_MODEL", ["int"]),
    0x07AF: ("GET_PLAYER_GROUP", "GET_PLAYER_GROUP", ["int", "out"]),
    0x0860: ("SET_CHAR_AREA_VISIBLE", "SET_CHAR_AREA_VISIBLE", ["int", "int"]),
    0x087B: ("GIVE_PLAYER_CLOTHES_OUTSIDE_SHOP", "GIVE_PLAYER_CLOTHES_OUTSIDE_SHOP", ["int", "string", "string", "int"]),
}
# opcodes that need the player ped (FindPlayerPed()/pool handle) to exist
NEEDS_PLAYER = {0x01F5, 0x07AF, 0x0173, 0x0373, 0x02EB, 0x01B4, 0x0860, 0x087B, 0x070D, 0x0109, 0x01B2}

# eScriptParameterType payload sizes (RunningScript.h / CollectParameters)
INT_T = {0x04: "<b", 0x05: "<h", 0x01: "<i"}
VAR_T = {0x02: "global", 0x03: "local"}

MAIN_SCRIPT_SIZE = 200_000   # CTheScripts::MAIN_SCRIPT_SIZE

SPAWN = (812.5, -1203.25, 14.75, 135.0)
HOSP = [(830.0, -1190.0, 14.5, 90.0), (-1500.25, 2500.75, 33.3, 270.0)]
POL = [(790.0, -1250.0, 14.0, 180.0)]


class SkipTest(unittest.SkipTest):
    pass


def build(**kw):
    args = dict(weather=1, hour=9, money=5000, weapons=[(22, 120), (31, 300)])
    args.update(kw)
    return scm.build_freeroam_scm(SPAWN, HOSP, POL, **args)


# --------------------------------------------------------------------------------------
# Independent reader (deliberately not using tools/gta/scm.py's parse_header/disassemble)
# --------------------------------------------------------------------------------------

def u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def read_header_like_vanilla(d):
    """Mirror the PC loaders: GetSizeOfVariableSpace() = u32 at [3]; ReadObjectNamesFromScript
    reads the count at varSpace+8; ReadMultiScriptFileOffsetsFromScript follows the next GOTO
    target ([varSpace+3]) and reads at +8; ReadStreamedScriptData likewise one hop further."""
    var_space = u32(d, 3)
    h = {"var_space": var_space, "num_objects": u32(d, var_space + 8)}
    h["object_names"] = [d[var_space + 12 + 24 * i: var_space + 36 + 24 * i].split(b"\0")[0]
                         for i in range(h["num_objects"])]
    info = u32(d, var_space + 3)
    h["main_size"], h["largest_mission"], h["num_missions"], h["num_exclusive"], h["mission_locals"] = \
        struct.unpack_from("<IIHHI", d, info + 8)
    streamed = u32(d, info + 3)
    h["largest_streamed"], h["num_streamed"] = struct.unpack_from("<II", d, streamed + 8)
    h["streamed"] = [(d[streamed + 16 + 28 * i: streamed + 36 + 28 * i].split(b"\0")[0],)
                     + struct.unpack_from("<II", d, streamed + 36 + 28 * i) for i in range(h["num_streamed"])]
    unk = u32(d, streamed + 3)
    h["unknown3"] = u32(d, unk + 8)
    extra = u32(d, unk + 3)
    h["extra"] = struct.unpack_from("<II", d, extra + 8)
    h["code"] = u32(d, extra + 3)
    h["seg_offsets"] = [0, var_space, info, streamed, unk, extra]
    return h


def get_scm_chunk(d, index):
    """gta-reversed CTheScripts::GetSCMChunk: at most 6 hops, each must be 02 00 01."""
    off = 0
    for _ in range(6):
        assert d[off:off + 3] == b"\x02\x00\x01", f"no GOTO at {off}"
        if d[off + 7] == index:
            return off
        off = u32(d, off + 3)
    raise AssertionError(f"chunk {index} not found")


def read_arg(d, p):
    t = d[p]
    p += 1
    if t in INT_T:
        fmt = INT_T[t]
        return ("int", t, struct.unpack_from(fmt, d, p)[0]), p + struct.calcsize(fmt)
    if t == 0x06:
        return ("float", t, struct.unpack_from("<f", d, p)[0]), p + 4
    if t in VAR_T:
        return (VAR_T[t], t, struct.unpack_from("<H", d, p)[0]), p + 2
    if t == 0x09:
        raw = d[p:p + 8]
        assert b"\0" in raw, "TEXT_LABEL without terminator"
        return ("text8", t, raw.split(b"\0")[0].decode("ascii")), p + 8
    if t == 0x0E:
        n = d[p]
        assert 0 < n <= 127
        return ("string", t, d[p + 1:p + 1 + n].decode("ascii")), p + 1 + n
    raise AssertionError(f"unexpected parameter type 0x{t:02X} at {p - 1}")


def decode(d, start, end=None):
    end = len(d) if end is None else end
    out, p = [], start
    while p < end:
        w = struct.unpack_from("<H", d, p)[0]
        op = w & 0x7FFF
        assert op in REF, f"opcode {op:04X} at {p} not in reference table"
        q = p + 2
        args = []
        for _ in REF[op][2]:
            a, q = read_arg(d, q)
            args.append(a)
        out.append((p, op, bool(w & 0x8000), args))
        p = q
    assert p == end, "instruction stream overruns the file"
    return out


def class_ok(ref_cls, arg):
    kind = arg[0]
    if ref_cls == "int":
        return kind in ("int", "global", "local")
    if ref_cls == "intlit":
        return kind == "int"
    if ref_cls == "float":                     # vanilla CollectParameters does not convert
        return kind in ("float", "global", "local")
    if ref_cls == "label":
        return arg[1] == 0x01                  # GOTO-style absolute int32
    if ref_cls == "gvar":
        return kind == "global"
    if ref_cls == "lvar":
        return kind == "local"
    if ref_cls == "out":
        return kind in ("global", "local")
    if ref_cls == "text8":
        return arg[1] == 0x09
    if ref_cls == "string":
        return arg[1] == 0x0E
    raise AssertionError(ref_cls)


# --------------------------------------------------------------------------------------
# 1. header / engine-reader checks
# --------------------------------------------------------------------------------------

def test_header_layout_default():
    d = build()
    h = read_header_like_vanilla(d)
    assert len(d) <= MAIN_SCRIPT_SIZE
    assert d[7:8] == b"s" and h["var_space"] == 8 + scm.VANILLA_GLOBAL_SPACE
    assert d[8:h["var_space"]] == bytes(h["var_space"] - 8), "global space must ship zeroed"
    assert [d[o + 7] for o in h["seg_offsets"]] == [ord("s"), 0, 1, 2, 3, 4]
    assert h["num_objects"] == 1 and h["object_names"] == [b""]
    assert h["main_size"] == len(d)                       # main block = whole file
    assert (h["largest_mission"], h["num_missions"], h["num_exclusive"], h["mission_locals"]) == (0, 0, 0, 0)
    assert (h["largest_streamed"], h["num_streamed"]) == (0, 0)   # script.img never consulted
    assert h["unknown3"] == 0
    assert h["extra"] == (scm.VANILLA_GLOBAL_SPACE, scm.SEG4_BUILD_NUMBER)
    assert d[h["code"]:h["code"] + 3] != b"\x02\x00\x01"   # chain ends at the first real opcode
    # every segment payload is exactly accounted for (no slack between GOTO target and data)
    so = h["seg_offsets"]
    assert so[2] - so[1] == 8 + 4 + 24 * h["num_objects"]
    assert so[3] - so[2] == 8 + 16 + 4 * h["num_missions"]
    assert so[4] - so[3] == 8 + 8 + 28 * h["num_streamed"]
    assert so[5] - so[4] == 8 + 4 and h["code"] - so[5] == 8 + 8


def test_header_chunk_walk_matches():
    for kw in ({}, {"aaa_stream": True, "global_space": 1632}):
        d = build(**kw)
        h = read_header_like_vanilla(d)
        for idx, off in zip((ord("s"), 0, 1, 2, 3, 4), h["seg_offsets"]):
            assert get_scm_chunk(d, idx) == off
        # the tool's own parser agrees with the independent one
        th = scm.parse_header(d)
        assert th.code_offset == h["code"] and th.main_size == h["main_size"]
        assert [s[0] for s in th.segments] == h["seg_offsets"]


def test_aaa_option_matches_gta3sc_layout():
    d = build(aaa_stream=True)
    h = read_header_like_vanilla(d)
    assert h["num_streamed"] == 1 and h["streamed"] == [(b"AAA", 0, 8)] and h["largest_streamed"] == 0


def test_globals_layout():
    d = build()
    ins = decode(d, read_header_like_vanilla(d)["code"])
    by_op = {}
    for _, op, _, args in ins:
        by_op.setdefault(op, []).append(args)
    # R*/Sanny/gta3sc indices: player $2, scplayer $3, Players_Group $11, ONMISSION $409
    assert by_op[0x0053][0][4] == ("global", 0x02, 8)
    assert by_op[0x01F5][0] == [("global", 0x02, 8), ("global", 0x02, 12)]
    assert by_op[0x07AF][0][1] == ("global", 0x02, 44)
    assert by_op[0x0180][0] == [("global", 0x02, 1636)]
    var_space = read_header_like_vanilla(d)["var_space"]
    for _, _, _, args in ins:
        for a in args:
            if a[0] == "global":
                assert 8 <= a[2] < var_space and a[2] % 4 == 0   # GetPointerToScriptVariable
            if a[0] == "local":
                assert 0 <= a[2] < 34


# --------------------------------------------------------------------------------------
# 2. opcode tables
# --------------------------------------------------------------------------------------

KIND_TO_REF = {"i": {"int"}, "I": {"int", "intlit"}, "f": {"float"}, "l": {"label"}, "g": {"gvar"},
               "v": {"lvar"}, "o": {"out"}, "t": {"text8"}, "s": {"string"}}


def test_module_table_vs_reference():
    for op, d in scm.OPCODES.items():
        assert op in REF, f"{op:04X} missing from REF"
        name, sanny, classes = REF[op]
        assert d.name == name, (op, d.name, name)
        assert len(d.kinds) == len(classes), d.name
        for k, c in zip(d.kinds, classes):
            assert c in KIND_TO_REF[k], (d.name, k, c)


def _check_stream(d):
    h = read_header_like_vanilla(d)
    ins = decode(d, h["code"])
    starts = {p for p, *_ in ins}
    for p, op, neg, args in ins:
        classes = REF[op][2]
        assert len(args) == len(classes)
        for c, a in zip(classes, args):
            assert class_ok(c, a), (REF[op][0], c, a)
            if c == "label":
                assert a[2] in starts and a[2] >= h["code"], "label must hit an instruction"
            if a[0] == "float":
                assert math.isfinite(a[2])
        if neg:
            assert op in (0x0019, 0x0248), "NOT flag only on conditions"
    return h, ins


def test_instruction_stream_types():
    for kw in ({}, {"weather": None, "money": 0, "weapons": (), "clothes": False},
               {"release_weather": True, "aaa_stream": True}):
        _check_stream(build(**kw))
    _check_stream(scm.build_freeroam_scm((SPAWN[0], SPAWN[1], None, 0.0), HOSP, POL))


def test_frame0_order_and_loops():
    d = build()
    h, ins = _check_stream(d)
    # execution starts at offset 0: the header GOTOs chain to the code
    p, seen = 0, set()
    while d[p:p + 3] == b"\x02\x00\x01":
        assert p not in seen
        seen.add(p)
        p = u32(d, p + 3)
    assert p == h["code"]
    ops = [op for _, op, _, _ in ins]
    first_wait = ops.index(0x0001)
    created = ops.index(0x0053)
    assert created < first_wait, "player must exist before the first WAIT (CGame::Initialise)"
    for i, op in enumerate(ops):
        if op in NEEDS_PLAYER:
            assert i > created, REF[op][0]
    assert ops.index(0x04E4) < ops.index(0x03CB) < created    # REQUEST_COLLISION, LOAD_SCENE
    assert ops.index(0x0180) < first_wait                     # mission flag from frame 0
    # screen is faded in again, control given back
    fades = [args for _, op, _, args in ins if op == 0x016A]
    assert fades[0][1][2] == 0 and fades[-1][1][2] == 1
    assert any(op == 0x01B4 and args[1][2] == 1 for _, op, _, args in ins)
    # every backward jump encloses a WAIT (no busy loop that would freeze the game);
    # the stream ends in an unconditional backward GOTO (MAIN never runs off the end)
    index = {p: i for i, (p, *_) in enumerate(ins)}
    for i, (p, op, _, args) in enumerate(ins):
        if op in (0x0002, 0x004D) and args[0][2] <= p:
            assert 0x0001 in ops[index[args[0][2]]:i + 1], f"loop without WAIT at {p}"
    assert ins[-1][1] == 0x0002 and ins[-1][3][0][2] < ins[-1][0]
    # restart points: at least one of each, at most CRestart::MAX_RESTART_POINTS
    assert 1 <= ops.count(0x016C) <= 10 and 1 <= ops.count(0x016D) <= 10


def test_values_round_trip():
    d = build()
    ins = decode(d, read_header_like_vanilla(d)["code"])
    get = {op: args for _, op, _, args in reversed(ins)}
    cp = get[0x0053]
    assert cp[0][2] == 0 and [a[2] for a in cp[1:4]] == [struct.unpack("<f", struct.pack("<f", v))[0]
                                                            for v in SPAWN[:3]]
    assert abs(get[0x0173][1][2] - SPAWN[3]) < 1e-4
    assert [a[2] for a in get[0x00C0]] == [9, 0]
    assert get[0x01B6][0][2] == 1
    assert [a[2] for a in get[0x0629]] == [181, 4]
    hosp = [[a[2] for a in args] for _, op, _, args in ins if op == 0x016C]
    assert len(hosp) == 2 and abs(hosp[1][0] + 1500.25) < 1e-3 and hosp[1][4] == 0
    clothes = [(a[1][2], a[2][2], a[3][2]) for _, op, _, a in ins if op == 0x087B]
    assert clothes == list(scm.DEFAULT_CLOTHES)
    weapons = [(a[1][2], a[2][2]) for _, op, _, a in ins if op == 0x01B2]
    assert weapons == [(22, 120), (31, 300)]
    models = sorted(a[0][2] for _, op, _, a in ins if op == 0x0247)
    assert models == [346, 356]
    # auto ground z: CREATE_PLAYER z = -100 triggers FindGroundZForCoord
    d2 = scm.build_freeroam_scm((SPAWN[0], SPAWN[1], None, 0.0), HOSP, POL)
    cp2 = [a for _, op, _, a in decode(d2, read_header_like_vanilla(d2)["code"]) if op == 0x0053][0]
    assert cp2[3][2] == -100.0


def test_encodings():
    assert scm.encode_int(0) == b"\x04\x00" and scm.encode_int(-128) == b"\x04\x80"
    assert scm.encode_int(128) == b"\x05\x80\x00" and scm.encode_int(-32769)[0] == 0x01
    assert scm.encode_float(0) == b"\x06\x00\x00\x00\x00"
    assert scm.encode_text_label8("main") == b"\x09MAIN\0\0\0\0"
    assert scm.encode_varlen_string("vest") == b"\x0e\x04VEST"
    for bad in (lambda: scm.encode_int(2**31), lambda: scm.encode_int(1.5),
                lambda: scm.encode_float(float("nan")), lambda: scm.encode_text_label8("TOOLONG8"),
                lambda: scm.encode_varlen_string("x" * 128)):
        try:
            bad()
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError("expected failure")


def test_assembler_validation():
    a = scm.ScmAssembler(64)
    a.emit("SET_CHAR_HEADING", scm.GVar(3), 90)          # int literal for a float param -> float
    assert a.assemble(100)[-5:] == b"\x06" + struct.pack("<f", 90.0)
    for call in (lambda: a.emit("WAIT"), lambda: a.emit("WAIT", 1.0), lambda: a.emit("WAIT", 1, 2),
                 lambda: a.emit("CREATE_PLAYER", 0, 1.0, 2.0, 3.0, 5),
                 lambda: a.emit("DECLARE_MISSION_FLAG", scm.LVar(1)), lambda: a.emit(0x1234),
                 lambda: a.emit("WAIT", 0, negate=True), lambda: a.gvar("x", 1000)):
        try:
            call()
        except (TypeError, ValueError, KeyError):
            pass
        else:
            raise AssertionError("expected failure")
    b = scm.ScmAssembler(64)
    b.goto("nowhere")
    try:
        b.assemble(100)
    except KeyError:
        pass
    else:
        raise AssertionError("undefined label accepted")


def test_freeroam_validation():
    bad = [dict(hospitals=[]), dict(police=[]), dict(hospitals=[HOSP[0]] * 11),
           dict(weather=23), dict(hour=24), dict(max_wanted_level=7),
           dict(spawn=(4000.0, 0.0, 0.0, 0.0)), dict(spawn=(0.0, 0.0, float("nan"), 0.0)),
           dict(hospitals=[(0.0, 0.0, None, 0.0)]), dict(weapons=[(99, 10)]), dict(ped_density=-1)]
    for kw in bad:
        args = dict(spawn=SPAWN, hospitals=HOSP, police=POL)
        args.update(kw)
        try:
            scm.build_freeroam_scm(args.pop("spawn"), args.pop("hospitals"), args.pop("police"), **args)
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"accepted {kw}")
    # exactly 10 restart points is fine
    scm.build_freeroam_scm(SPAWN, [HOSP[0]] * 10, [POL[0]] * 10)


def test_extras():
    def extra(asm, v):
        asm.emit("SET_PLAYER_CONTROL", v.player, 1)
    d = scm.build_freeroam_scm(SPAWN, HOSP, POL, extras=[("RELEASE_WEATHER",), (0x04BB, 0), extra])
    ins = decode(d, read_header_like_vanilla(d)["code"])
    ops = [op for _, op, _, _ in ins]
    i = len(ops) - 2                                     # MAIN_LOOP: WAIT, GOTO
    assert ops[i - 3:i] == [0x01B7, 0x04BB, 0x01B4] and ops[i:] == [0x0001, 0x0002]


# --------------------------------------------------------------------------------------
# 3. live opcode databases (Sanny Builder sa.json + SASCM.INI, gta3sc commands.xml)
# --------------------------------------------------------------------------------------

def _need(path):
    if not os.path.exists(path):
        raise SkipTest(f"reference file not available: {path}")


def _sa_json_classes(c):
    out = []
    for p in c.get("input", []):
        t, s = p["type"], p.get("source", "")
        if t in ("float", "label", "string"):
            out.append(t)
        else:
            out.append({"var_global": "gvar", "var_local": "lvar", "literal": "intlit"}.get(s, "int"))
    out += ["out"] * len(c.get("output", []))
    return out


def test_against_sanny_sa_json():
    _need(SA_JSON)
    with open(SA_JSON, encoding="utf-8") as f:
        db = json.load(f)
    cmds = {int(c["id"], 16): c for c in db["extensions"][0]["commands"]}
    for op, (name, sanny, classes) in REF.items():
        c = cmds[op]
        assert c["name"] == sanny and c["num_params"] == len(classes), (op, c["name"])
        assert not c.get("attrs", {}).get("is_unsupported"), sanny
        got = [("string" if k in ("text8", "string") else k) for k in classes]
        assert _sa_json_classes(c) == got, (sanny, _sa_json_classes(c), got)
        assert bool(c.get("attrs", {}).get("is_condition")) == (op in (0x0019, 0x0248)), sanny


def test_against_sascm_ini():
    _need(SASCM_INI)
    counts = {}
    with open(SASCM_INI, encoding="latin-1") as f:
        for line in f:
            m = re.match(r"^([0-9A-Fa-f]{4})=(-?\d+),", line.strip())
            if m:
                counts[int(m.group(1), 16)] = int(m.group(2))
    for op, (_, _, classes) in REF.items():
        assert counts[op] == len(classes), (f"{op:04X}", counts[op], len(classes))


def test_against_gta3sc_commands_xml():
    _need(G3_COMMANDS)
    root = ET.parse(G3_COMMANDS).getroot()
    cmds = {int(c.get("ID"), 16): c for c in root.iter("Command") if c.get("ID")}
    for op, (name, _, classes) in REF.items():
        c = cmds[op]
        assert c.get("Name") == name and c.get("Supported") != "false", name
        args = list(c.iter("Arg"))
        assert len(args) == len(classes), name
        for a, k in zip(args, classes):
            t, out = a.get("Type"), a.get("Out") == "true"
            exp = {"float": ("FLOAT",), "label": ("LABEL",), "text8": ("TEXT_LABEL",),
                   "string": ("STRING",)}.get(k, ("INT",))
            assert t in exp and out == (k == "out"), (name, t, k)
            if k in ("gvar", "lvar"):
                assert a.get("AllowConst") == "false", (name, k)
            if k == "intlit":
                assert a.get("AllowGlobalVar") == "false" and a.get("AllowLocalVar") == "false"


# --------------------------------------------------------------------------------------
# 4. gta3sc (thelink2012) decompiler + compiler cross-checks
# --------------------------------------------------------------------------------------

def _need_gta3sc():
    if not (os.path.isfile(GTA3SC) and os.access(GTA3SC, os.X_OK)):
        raise SkipTest(f"gta3sc binary not available: {GTA3SC}")


def _run_gta3sc(args, cwd):
    r = subprocess.run([GTA3SC] + args, cwd=cwd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def _parse_ir2_arg(tok):
    if tok.startswith("&"):
        return ("global", int(tok[1:]))
    if tok.endswith("@"):
        return ("local", int(tok[:-1]))
    if tok.startswith("@"):
        return ("label", tok[1:])
    if tok.startswith("'") or tok.startswith('"'):
        return ("str", tok[1:-1])
    m = re.fullmatch(r"(-?\d+)i(8|16|32)", tok)
    if m:
        return ("int", int(m.group(1)))
    if tok.endswith("f"):
        return ("float", float.fromhex(tok[:-1]))
    raise AssertionError(f"unparsed IR2 token {tok}")


def test_gta3sc_decompiles_our_file():
    _need_gta3sc()
    for kw in ({}, {"weather": None, "weapons": (), "money": 0}):
        d = build(**kw)
        ins = decode(d, read_header_like_vanilla(d)["code"])
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "main.scm")
            with open(src, "wb") as f:
                f.write(d)
            # config/gtasa enables -fstreamed-scripts, so the decompiler opens script.img
            # next to main.scm; an empty VER2 archive is enough (our header lists none)
            with open(os.path.join(tmp, "script.img"), "wb") as f:
                f.write(b"VER2" + struct.pack("<I", 0))
            _run_gta3sc(["decompile", src, "--config=gtasa", "--guesser", "-emit-ir2",
                         "-o", os.path.join(tmp, "out.ir2")], tmp)
            with open(os.path.join(tmp, "out.ir2"), encoding="latin-1") as f:
                lines = [l.strip() for l in f if l.strip()]
        label_at, cmds, pending = {}, [], []
        for l in lines:
            if l.endswith(":"):
                pending.append(l[:-1])
                continue
            toks = re.findall(r"'[^']*'|\"[^\"]*\"|\S+", l)
            for lab in pending:
                label_at[lab] = len(cmds)
            pending = []
            cmds.append((toks[0], [_parse_ir2_arg(t) for t in toks[1:]]))
        assert len(cmds) == len(ins), (len(cmds), len(ins))
        index = {p: i for i, (p, *_) in enumerate(ins)}
        for (name, gargs), (p, op, neg, args) in zip(cmds, ins):
            assert name == ("NOT " if neg else "") + REF[op][0], (name, REF[op][0])
            assert len(gargs) == len(args)
            for (gk, gv), a in zip(gargs, args):
                if gk == "label":
                    assert label_at[gv] == index[a[2]]
                elif gk == "str":
                    assert gv == a[2]
                elif gk == "float":
                    assert a[0] == "float" and gv == a[2]
                else:
                    assert gk == a[0] and gv == a[2], (name, gk, gv, a)


def _fmt_float(v):
    s = "{:.9g}".format(v)
    if "e" in s:
        s = "{:.12f}".format(v)
    return s if "." in s else s + ".0"


def transcribe_to_gta3script(d, names):
    """GTA3script source reproducing our instruction stream 1:1 (R* command names), all
    globals declared in index order so gta3sc allocates the same offsets."""
    h = read_header_like_vanilla(d)
    ins = decode(d, h["code"])
    used = {a[2] // 4 for _, _, _, args in ins for a in args if a[0] == "global"}
    top = max(used)
    var = {i: names.get(i, f"gv{i}") for i in range(2, top + 1)}
    targets = sorted({a[2] for _, op, _, args in ins for c, a in zip(REF[op][2], args) if c == "label"})
    labname = {t: f"lab_{i}" for i, t in enumerate(targets)}
    out = [f"VAR_INT {var[i]}" for i in range(2, top + 1)] + ["{"]
    for p, op, neg, args in ins:
        if p in labname:
            out.append(labname[p] + ":")
        toks = [("NOT " if neg else "") + REF[op][0]]
        for c, a in zip(REF[op][2], args):
            if c == "label":
                toks.append(labname[a[2]])
            elif a[0] == "global":
                toks.append(var[a[2] // 4])
            elif a[0] == "local":
                toks.append({32: "TIMERA", 33: "TIMERB"}[a[2]])
            elif a[0] == "float":
                toks.append(_fmt_float(a[2]))
            else:
                toks.append(str(a[2]))
        out.append(" ".join(toks))
    out.append("}")
    return "\n".join(out) + "\n", top


def test_gta3sc_compiles_transcription_byte_identical():
    _need_gta3sc()
    names = {2: "player", 3: "scplayer", 11: "player_group", 409: "flag_player_on_mission"}
    probe = build()
    src, top = transcribe_to_gta3script(probe, names)
    # gta3sc: globals [8, 8 + 4*(top-1)), always one 'AAA' streamed entry
    ours = build(global_space=4 * top - 4, aaa_stream=True)
    src, _ = transcribe_to_gta3script(ours, names)
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "main.sc"), "w") as f:
            f.write(src)
        r = _run_gta3sc(["compile", "main.sc", "--config=gtasa", "--guesser", "-o", "main.scm"], tmp)
        assert "error" not in r.stderr.lower(), r.stderr
        with open(os.path.join(tmp, "main.scm"), "rb") as f:
            theirs = f.read()
    if theirs != ours:
        i = next((k for k, (x, y) in enumerate(zip(ours, theirs)) if x != y), min(len(ours), len(theirs)))
        raise AssertionError(f"differs at {i}: ours {ours[i:i + 16].hex()} gta3sc {theirs[i:i + 16].hex()}")
    # the default build (vanilla-sized globals, no AAA) has the same code modulo the
    # header-size shift of absolute label offsets
    d = build()
    a_ins = decode(d, read_header_like_vanilla(d)["code"])
    b_ins = decode(ours, read_header_like_vanilla(ours)["code"])
    assert len(a_ins) == len(b_ins)
    shift = read_header_like_vanilla(d)["code"] - read_header_like_vanilla(ours)["code"]
    for (pa, oa, na, aa), (pb, ob, nb, ab) in zip(a_ins, b_ins):
        assert (oa, na) == (ob, nb) and pa - pb == shift
        for c, x, y in zip(REF[oa][2], aa, ab):
            assert (x[0], x[1]) == y[:2] and (x[2] - shift if c == "label" else x[2]) == y[2]


# --------------------------------------------------------------------------------------

if __name__ == "__main__":
    fails = 0
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)):
        try:
            fn()
            print("ok  ", name)
        except unittest.SkipTest as e:
            print("skip", name, "-", e)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print("FAIL", name, "-", type(e).__name__, e)
    sys.exit(1 if fails else 0)
