"""Ties SERVER_VERSION (#398) to docker-compose.yml and tdb/README.md.

The worldserver image, the TDB release and its filename are recorded in three places. This
suite fails when they disagree: the `image:` of the trinitycore-wowserver service must equal the
`image` key, and both `tdb` and `tdb_file` must appear verbatim in tdb/README.md. The compose
file is read as text (no yaml dependency), so this stays stdlib-only.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
VERSION = ROOT / "SERVER_VERSION"
COMPOSE = ROOT / "docker-compose.yml"
TDB_README = ROOT / "tdb" / "README.md"

KNOWN_KEYS = {"image", "tdb", "tdb_file", "trinitycore_commit"}
SERVICE_IMAGE = re.compile(
    r"^  trinitycore-wowserver:\n(?:(?:    .*)?\n)*?    image:[ \t]*(\S+)", re.M
)


def parse_version(text):
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError(f"not key=value: {raw!r}")
        values[key.strip()] = value.strip()
    return values


class ServerVersionTest(unittest.TestCase):
    def setUp(self):
        self.values = parse_version(VERSION.read_text(encoding="utf-8"))

    def test_only_known_keys(self):
        self.assertTrue(self.values, "SERVER_VERSION has no keys")
        self.assertLessEqual(set(self.values), KNOWN_KEYS)

    def test_image_matches_compose_worldserver(self):
        match = SERVICE_IMAGE.search(COMPOSE.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "trinitycore-wowserver image: not found in docker-compose.yml")
        self.assertEqual(self.values["image"], match.group(1))

    def test_tdb_and_filename_appear_in_tdb_readme(self):
        readme = TDB_README.read_text(encoding="utf-8")
        self.assertIn(self.values["tdb"], readme)
        self.assertIn(self.values["tdb_file"], readme)


if __name__ == "__main__":
    unittest.main()
