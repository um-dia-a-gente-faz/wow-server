"""Regression tests against real captured SMSG_UPDATE_OBJECT payloads.

See agent/tests/fixtures/update_object/README.md for how these were captured
and what's known about each one. `agent/update_object.py` (the real parser)
doesn't exist yet — these are block-count placeholders per UM-31; UM-32/33
turn the expectedFailure/skip markers below into real assertions.

Run from the repo root: python3 -m unittest discover -s agent/tests
"""

import pathlib
import struct
import unittest

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "update_object"

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


class FutureParserTest(unittest.TestCase):
    """Placeholders for UM-32 (framing) and UM-33 (values/fields).

    Turn these green by importing the real parser once it exists, e.g.:
        from agent import update_object as uo
        blocks = uo.parse_update_object(data)
    """

    @unittest.skip("agent/update_object.py doesn't exist yet (UM-32)")
    def test_every_fixture_consumes_exactly_its_length(self):
        # For every fixture: parsing consumes exactly len(payload) bytes,
        # each block's update_type is in 0-5 (UM-32 acceptance criterion).
        for name in EXPECTED_BLOCK_COUNTS:
            with self.subTest(fixture=name):
                self.fail("pending agent.update_object.parse_update_object")

    @unittest.skip("agent/update_object.py doesn't exist yet (UM-32)")
    def test_self_position_matches_login_verify_world(self):
        # login_self_create.bin's block should decode to a LIVING position
        # within 0.1 yd of (10344.900390625, -6354.1201171875,
        # 32.60350036621094, 0.0) — see fixtures README.
        self.fail("pending agent.update_object.parse_update_object")

    @unittest.skip("agent/update_fields.py doesn't exist yet (UM-33)")
    def test_expected_entries_present(self):
        # login_sunstrider.bin should decode a "Cat" critter (entry 6368,
        # level 1, health 1); busy_zone.bin should decode "[DND] Shaker"
        # (entry 37543, level 60) and gameobject entries 182323-182326/
        # 182623/182624 — see fixtures README for the full table.
        self.fail("pending agent.update_fields.decode_fields")

    @unittest.skip("agent/update_object.py doesn't exist yet (UM-32)")
    def test_login_sunstrider_spline_block_raises(self):
        # Block 3 of login_sunstrider.bin has MOVEMENTFLAG_SPLINE_ENABLED
        # set; until spline parsing is implemented this should raise
        # UnhandledMovementFlags, not silently desync the offset.
        self.fail("pending agent.update_object.UnhandledMovementFlags")


if __name__ == '__main__':
    unittest.main()
