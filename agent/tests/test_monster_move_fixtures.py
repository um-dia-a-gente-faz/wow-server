"""Regression tests against real captured SMSG_MONSTER_MOVE payloads.

See agent/tests/fixtures/monster_move/README.md for how these were captured
and what's known about each one.

Run from the repo root: python3 -m unittest discover -s agent/tests
"""

import pathlib
import unittest

from agent import perception as per
from agent import update_object as uo

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "monster_move"

# guid/entry the fixtures README documents for both fixtures (same NPC,
# "Broom", world.creature_template entry 17213, minlevel=maxlevel=1).
BROOM_GUID = 0xF13000433D0027ED


def load_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


class RealFixtureParseTest(unittest.TestCase):
    def test_walking_npc_single_point(self):
        info = uo.parse_monster_move(load_fixture("walking_npc.bin"))
        self.assertEqual(info["mover_guid"], BROOM_GUID)
        self.assertEqual(info["move_type"], uo.MONSTER_MOVE_NORMAL)
        self.assertEqual(len(info["points"]), 1)
        self.assertEqual(info["points"][0], info["destination"])

    def test_walking_npc_with_packed_waypoint(self):
        info = uo.parse_monster_move(load_fixture("walking_npc_with_waypoint.bin"))
        self.assertEqual(info["mover_guid"], BROOM_GUID)
        self.assertEqual(len(info["points"]), 2)
        # points[0] is the raw destination; points[1] is reconstructed from a
        # packed delta relative to the pos/destination midpoint — sanity
        # check it landed somewhere between the two rather than garbage.
        px, py, pz = info["pos"]
        dx, dy, dz = info["destination"]
        wx, wy, wz = info["points"][1]
        self.assertLess(min(px, dx) - 5, wx)
        self.assertLess(wx, max(px, dx) + 5)
        self.assertLess(min(py, dy) - 5, wy)
        self.assertLess(wy, max(py, dy) + 5)


class RealFixtureIntegrationTest(unittest.TestCase):
    """Feeds a real captured MONSTER_MOVE through WorldState.apply_monster_move
    for an object already known via a (synthetic) CREATE block."""

    def test_apply_monster_move_starts_spline_from_real_bytes(self):
        ws = per.WorldState()
        ws.set_my_map(530)
        ws.update_object(uo.UpdateBlock(
            update_type=uo.UPDATETYPE_CREATE_OBJECT2, guid=BROOM_GUID, object_type=uo.TYPEID_UNIT,
            movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": 9508.958, "y": -7303.668, "z": 14.119, "o": 0.0},
            fields={0x03: 17213},
        ))
        info = uo.parse_monster_move(load_fixture("walking_npc.bin"))
        ws.apply_monster_move(info)

        obj = ws.get_object(BROOM_GUID)
        self.assertIsNotNone(obj.spline)
        self.assertEqual(obj.spline["destination"], info["destination"])
        # Right after the update, current_position() should be close to the
        # spline's start (pos), not yet interpolated toward the destination.
        pos = obj.current_position()
        self.assertAlmostEqual(pos[1], info["pos"][0], delta=0.5)


if __name__ == '__main__':
    unittest.main()
