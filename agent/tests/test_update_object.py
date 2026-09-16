"""Regression tests against real captured SMSG_UPDATE_OBJECT payloads.

See agent/tests/fixtures/update_object/README.md for how these were captured
and what's known about each one.

Run from the repo root: python3 -m unittest discover -s agent/tests
"""

import pathlib
import struct
import unittest

from agent import update_fields as uf
from agent import update_object as uo

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "update_object"

# login_self_create.bin's block, decoded, should be within this many yards of
# session.player_position at the same login (from SMSG_LOGIN_VERIFY_WORLD) —
# see the fixtures README.
LOGIN_VERIFY_WORLD_POS = (10344.900390625, -6354.1201171875, 32.60350036621094, 0.0)

# file -> expected uint32 block_count (first 4 bytes), per the fixtures README.
EXPECTED_BLOCK_COUNTS = {
    "login_self_create.bin": 1,
    "login_sunstrider.bin": 55,
    "busy_zone.bin": 74,
    "gameobject_cluster.bin": 6,
    "gameobject_create.bin": 1,
    "idle_values.bin": 1,
}


def load_fixture(name: str) -> bytes:
    """Return the raw (already-decompressed) update-object payload bytes for
    a fixture file in agent/tests/fixtures/update_object/."""
    return (FIXTURES_DIR / name).read_bytes()


class FixtureInventoryTest(unittest.TestCase):
    def test_at_least_five_fixtures_committed(self):
        files = sorted(p.name for p in FIXTURES_DIR.glob("*.bin"))
        self.assertGreaterEqual(len(files), 5, files)
        self.assertEqual(set(files), set(EXPECTED_BLOCK_COUNTS), set(files) ^ set(EXPECTED_BLOCK_COUNTS))


class BlockCountTest(unittest.TestCase):
    """The only thing parseable without agent/update_object.py: the header."""

    def test_block_count_matches_readme(self):
        for name, expected in EXPECTED_BLOCK_COUNTS.items():
            with self.subTest(fixture=name):
                data = load_fixture(name)
                self.assertGreaterEqual(len(data), 4)
                block_count = struct.unpack_from('<I', data, 0)[0]
                self.assertEqual(block_count, expected)


# login_sunstrider.bin's block 3 has MOVEMENTFLAG_SPLINE_ENABLED set (a real
# creature actively pathing at capture time) — spline data isn't implemented,
# so parsing this one fixture is expected to raise UnhandledMovementFlags
# partway through rather than consume the whole payload. Every other fixture
# has no spline blocks and should fully round-trip.
EXPECT_SPLINE_RAISE = "login_sunstrider.bin"


class FramingTest(unittest.TestCase):
    """UM-32 acceptance criteria against the real fixtures."""

    def test_every_other_fixture_consumes_exactly_its_length(self):
        for name in EXPECTED_BLOCK_COUNTS:
            if name == EXPECT_SPLINE_RAISE:
                continue
            with self.subTest(fixture=name):
                data = load_fixture(name)
                blocks = uo.parse_update_object(data)
                self.assertEqual(len(blocks), EXPECTED_BLOCK_COUNTS[name])
                for block in blocks:
                    self.assertIn(block.update_type, range(6))

    def test_login_sunstrider_spline_block_raises(self):
        data = load_fixture(EXPECT_SPLINE_RAISE)
        with self.assertRaises(uo.UnhandledMovementFlags) as ctx:
            uo.parse_update_object(data)
        self.assertTrue(ctx.exception.move_flags & uo.MOVEMENTFLAG_SPLINE_ENABLED)

    def test_self_position_matches_login_verify_world(self):
        data = load_fixture("login_self_create.bin")
        blocks = uo.parse_update_object(data)
        self.assertEqual(len(blocks), 1)
        movement = blocks[0].movement
        for actual, expected in zip((movement["x"], movement["y"], movement["z"], movement["o"]),
                                     LOGIN_VERIFY_WORLD_POS):
            self.assertAlmostEqual(actual, expected, delta=0.1)


class FieldMappingTest(unittest.TestCase):
    """UM-33: decode_fields() against fixtures cross-checked against
    world.creature_template / world.gameobject_template (see the README).

    login_sunstrider.bin isn't used here even though its "Cat" critter
    (entry 6368) is the README's headline example: that fixture raises
    UnhandledMovementFlags partway through (the spline block), so
    parse_update_object never returns blocks for it. busy_zone.bin and
    gameobject_cluster.bin fully round-trip and carry the same real data.
    """

    def test_creature_entries_and_stats(self):
        data = load_fixture("gameobject_cluster.bin")
        blocks = uo.parse_update_object(data)
        decoded = [uf.decode_fields(b.object_type, b.fields) for b in blocks]
        # All 6 blocks are "[DND] Shaker" / "Shaker - Small" (entry 37543 /
        # 37574), level 60, health == max_health == 3052.
        self.assertEqual({d["entry"] for d in decoded}, {37543, 37574})
        for d in decoded:
            self.assertEqual(d["level"], 60)
            self.assertEqual(d["health"], d["max_health"])
            self.assertGreater(d["health"], 0)

    def test_named_mobs_and_gameobjects_in_busy_zone(self):
        data = load_fixture("busy_zone.bin")
        blocks = uo.parse_update_object(data)
        decoded = {}
        for b in blocks:
            fields = uf.decode_fields(b.object_type, b.fields)
            decoded[fields.get("entry")] = (b.object_type, fields)

        # "Bergrisst" and "Chief Thunder-Skins", level 70.
        for entry in (25148, 25149):
            self.assertIn(entry, decoded)
            object_type, fields = decoded[entry]
            self.assertEqual(object_type, uo.TYPEID_UNIT)
            self.assertEqual(fields["level"], 70)

        # Zone signage gameobjects: "The Royal Exchange", "Court of the Sun",
        # "Farstrider Square", "The Bazaar", two "Chair"s.
        for entry in (182323, 182324, 182325, 182326, 182623, 182624):
            self.assertIn(entry, decoded)
            object_type, _fields = decoded[entry]
            self.assertEqual(object_type, uo.TYPEID_GAMEOBJECT)


if __name__ == '__main__':
    unittest.main()
