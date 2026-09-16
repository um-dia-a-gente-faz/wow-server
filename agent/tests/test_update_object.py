"""Regression tests against real captured SMSG_UPDATE_OBJECT payloads.

See agent/tests/fixtures/update_object/README.md for how these were captured
and what's known about each one.

Run from the repo root: python3 -m unittest discover -s agent/tests
"""

import pathlib
import struct
import unittest

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


@unittest.skip("agent/update_fields.py doesn't exist yet (UM-33)")
class FieldMappingTest(unittest.TestCase):
    def test_expected_entries_present(self):
        # login_sunstrider.bin should decode a "Cat" critter (entry 6368,
        # level 1, health 1); busy_zone.bin should decode "[DND] Shaker"
        # (entry 37543, level 60) and gameobject entries 182323-182326/
        # 182623/182624 — see fixtures README for the full table.
        self.fail("pending agent.update_fields.decode_fields")


if __name__ == '__main__':
    unittest.main()
