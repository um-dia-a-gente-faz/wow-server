"""Ties SERVER_VERSION (#398) to docker-compose.yml and tdb/README.md.

The worldserver image, the TDB release and its filename are recorded in three places. This
suite fails when they disagree: the `image:` of the trinitycore-wowserver service must equal the
`image` key, and `tdb` and `tdb_file` must appear in tdb/README.md as whole tokens, not as
substrings. The record must have exactly the four known keys, each non-empty, and a
TrinityCore commit that looks like a hash. The compose file is read as text (no yaml
dependency), so this stays stdlib-only.
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
TDB_RELEASE = re.compile(r"TDB\d+\.\d+")  # e.g. TDB335.25101; a bare "TDB" is not a release
COMMIT = re.compile(r"[0-9a-f]{7,40}")


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

    def test_exact_keys_each_non_empty(self):
        self.assertEqual(set(self.values), KNOWN_KEYS)
        for key in KNOWN_KEYS:
            self.assertTrue(self.values[key], f"{key} is empty")

    def test_trinitycore_commit_is_a_hash(self):
        self.assertIsNotNone(COMMIT.fullmatch(self.values["trinitycore_commit"]))

    def test_tdb_is_a_release_token(self):
        self.assertIsNotNone(TDB_RELEASE.fullmatch(self.values["tdb"]))

    def test_image_matches_compose_worldserver(self):
        match = SERVICE_IMAGE.search(COMPOSE.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "trinitycore-wowserver image: not found in docker-compose.yml")
        self.assertEqual(self.values["image"], match.group(1))

    def test_tdb_and_filename_are_whole_tokens_in_tdb_readme(self):
        readme = TDB_README.read_text(encoding="utf-8")
        tdb = self.values["tdb"]
        tdb_file = self.values["tdb_file"]
        self.assertRegex(readme, re.escape(tdb) + r"\b")
        self.assertRegex(readme, r"\b" + re.escape(tdb_file) + r"(?![\w.])")


if __name__ == "__main__":
    unittest.main()
