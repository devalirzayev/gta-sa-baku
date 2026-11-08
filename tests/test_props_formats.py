"""Format tests for the data added with props / signage / landmarks / parked cars."""
import math
import os
import re
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np

from tools.gta import ipl as IPL
from tools.rw import col


def test_binary_ipl_with_car_generators():
    insts = [IPL.Inst(1294, "mlamppost", 10.0, -20.0, 5.0, 1.0)]
    cars = [IPL.CarGen(1.5, 2.5, 3.5, -0.75, -1, -1, -1, 2, 0, 30, 0, 0)]
    data = IPL.binary_ipl(insts, cars)
    # tBinaryIplFile (GR tBinaryIplFile.h): 'bnry', 6 counts, 6 (offset, size) pairs, 0x4C bytes
    assert data[:4] == b"bnry"
    counts = struct.unpack_from("<6I", data, 4)
    assert counts == (1, 0, 0, 0, 1, 0)
    pairs = struct.unpack_from("<12I", data, 28)
    off_inst, off_cars = pairs[0], pairs[8]
    assert off_inst == 0x4C
    x, y, z, qx, qy, qz, qw, mid, flags, lod = struct.unpack_from("<3f4fiIi", data, off_inst)
    assert (mid, lod) == (1294, -1) and abs(x - 10) < 1e-6
    # CFileCarGenerator (GR CarGenerator.h): pos, angle (radians), model, colours, flags, alarm, lock, delays
    v = struct.unpack_from("<4f8i", data, off_cars)
    assert v[3] == -0.75 and v[4] == -1 and v[7] == 2 and v[9] == 30
    i2, c2 = IPL.read_binary_ipl(data)
    assert len(i2) == 1 and len(c2) == 1
    assert c2[0].force_spawn == 2 and abs(c2[0].angle + 0.75) < 1e-6     # flags word, angle in radians


def test_bounds_only_col():
    m = col.ColModel("bk_testlod", 19999, bounds=((-250, -250, -5), (250, 250, 40)))
    b = col.write_col3(m)
    r = col.read_col(b)[0]
    assert r["n_faces"] == 0 and r["n_boxes"] == 0 and r["n_spheres"] == 0
    assert r["flags"] & 2 == 0                      # no volumes -> SetUsesCollision(false)
    assert np.allclose(r["min"], (-250, -250, -5)) and np.allclose(r["max"], (250, 250, 40))


def test_object_dat_lines_match_bussign1():
    from tools.build import signs
    vanilla = ("bussign1,\t\t30.0,\t\t50.0   \t\t0.99,\t\t0.05,\t\t50.0,\t\t100.0,  1.0,\t200,\t1,\t1,\t0,\t0,\t0.0, 0.0, 0.0,"
               "\t\tnone\t\t\t115.0, \t0.0, \t0.0, \t0.1, \t0.07,\t1,\t0")

    def fields(line):
        # CFileLoader::LoadLine turns ',' into ' '; CObjectData::Initialise sscanf's the rest
        return re.sub(r"[,\s]+", " ", line).strip().split(" ")[1:]
    for line in signs.object_dat_lines():
        assert fields(line) == fields(vanilla), line
        assert not line.lstrip().startswith(("*", ";", "#"))


def test_traffic_light_heading_rule():
    from tools.build.build_paths import light_group_for_dir
    from tools.gta import paths as P
    # FindTrafficLightTypeFromOrientation: 60-150 / 240-330 deg -> group 1
    for deg, grp in ((0, 2), (59, 2), (61, 1), (90, 1), (149, 1), (151, 2), (241, 1), (329, 1), (331, 2)):
        a = math.radians(deg)
        assert light_group_for_dir(math.cos(a), math.sin(a)) == (P.LIGHT_NORTH_SOUTH if grp == 1 else P.LIGHT_WEST_EAST)


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
            print("ok", k)
