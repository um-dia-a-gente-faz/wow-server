"""ADR 0018 slice 7 (#739): every released schema version of the activity store is a fixture that
opens with the real ActivityStore, migrates to SCHEMA_VERSION and reads back the same rows.

fixtures/activity_db/v<N>.sql is the database as version N created it, frozen as text: its
tables are a copy of SCHEMA, never an import of it. v0 is the file from before user_version was
set. Fixtures are append-only. Adding migration N+1 means: append it to MIGRATIONS in
activity_schema.py, add v<N+1>.sql, pin its sha256 in PINS and its rows in ROWS / META.
"""
import hashlib
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

# #670: point CALIBRATION_FILE at a temp file before any wowmap import
import _calibration_env  # noqa: F401
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "wowmap"))
import activity  # noqa: E402
import activity_schema  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "activity_db"
VERSIONS = (0, *range(1, activity_schema.SCHEMA_VERSION + 1))

# sha256 of each shipped fixture, CRLF normalised. Pinned by hand: a changed fixture must fail.
PINS = {
    0: "c08ae5ef6b947dd8b34dd650ab7fcb5f79d78419f620cfb8a84cf021d5ba0b75",
    1: "3145fdd61ac202718ba004e76c486442f71eb5ad064e125670921c0d99baebb9",
}
# What recent(character, 50) returns at read time, newest first, for the fixtures' rows.
# 'Rubens' and 'rubens' are one character to the store (the column is NOCASE), so both sets are here.
ROWS = {
    "Rubens": [
        {"t": 104.0, "kind": "money", "text": "Money +26c", "detail": {"delta": 26, "total": 1026},
         "source": "db", "inferred": False},
        {"t": 102.0, "kind": "chat", "text": 'Said "hi"', "detail": {"kind": "say", "channel": None},
         "source": "chat", "inferred": False},
        {"t": 101.0, "kind": "sell", "text": "Sold Linen Cloth ×2",
         "detail": {"item": 2589, "count": 2, "sell_price": 13, "money": 26}, "source": "db", "inferred": True},
        {"t": 100.0, "kind": "level", "text": "Reached level 11", "detail": {"from": 10, "to": 11},
         "source": "db", "inferred": False},
    ],
    "Thrall": [
        {"t": 103.5, "kind": "move", "text": "Move to x=1.5 y=2.0 z=0.0",
         "detail": {"action": "move_to", "args": {"x": 1.5, "y": 2.0, "z": 0.0}, "ok": True, "error": None,
                    "cycle": 4},
         "source": "audit", "inferred": False},
    ],
}
META = {"chat_last_id": "1700000000123-0"}


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _user_version(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


class FixtureGuardTests(unittest.TestCase):
    """The harness: a new migration cannot land without its fixture, pin and rows."""

    def test_every_version_from_0_to_schema_version_has_a_fixture(self):
        missing = [n for n in VERSIONS if not (FIXTURES / f"v{n}.sql").is_file()]
        self.assertEqual(missing, [], "\n".join(
            f"no fixtures/activity_db/v{n}.sql: write the database as version {n} creates it, with rows that "
            f"exercise what migration {n} added, then pin it and list its rows. A shipped fixture is never edited."
            for n in missing))

    def test_every_version_is_pinned_and_has_its_rows(self):
        problems = []
        for n in VERSIONS:
            if n not in PINS:
                path = FIXTURES / f"v{n}.sql"
                pin = f"PINS[{n}] = {_digest(path)!r}" if path.is_file() else "the fixture first"
                problems.append(f"v{n}.sql has no sha256 pin: add {pin}")
        self.assertEqual(problems, [])


class ActivityMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _from_fixture(self, n: int) -> str:
        path = os.path.join(self._tmp.name, f"v{n}.sqlite3")
        conn = sqlite3.connect(path)
        try:
            conn.executescript((FIXTURES / f"v{n}.sql").read_text(encoding="utf-8"))
        finally:
            conn.close()
        return path

    def _open(self, path: str) -> activity.ActivityStore:
        store = activity.ActivityStore(path)
        self.addCleanup(store._conn.close)
        return store

    def test_shipped_fixtures_are_frozen(self):
        for n, pinned in sorted(PINS.items()):
            digest = _digest(FIXTURES / f"v{n}.sql")
            self.assertEqual(digest, pinned,
                             f"v{n}.sql differs from its pin. Fixtures are append-only: a shipped fixture is "
                             f"never edited, restore it from git. Only a NEW fixture is pinned: "
                             f"PINS[{n}] = {digest!r}")

    def test_every_fixture_opens_migrates_and_reads_back(self):
        for n in VERSIONS:
            with self.subTest(fixture=f"v{n}.sql"):
                path = self._from_fixture(n)
                store = self._open(path)
                for character, rows in ROWS.items():
                    for name in (character, character.lower(), character.upper()):
                        self.assertEqual(store.recent(name, 50), rows)
                        self.assertEqual(store.count(name), len(rows))
                for key, value in META.items():
                    self.assertEqual(store.get_meta(key), value)
                self.assertEqual(store.recent("Nobody", 50), [])
                self.assertEqual(store.add([activity.event("Rubens", 200.0, "money", "new write", "db",
                                                           f"new:{n}")]), 1)
                self.assertEqual(store.recent("Rubens", 1)[0]["text"], "new write")
                store._conn.close()
                self.assertEqual(_user_version(path), activity_schema.SCHEMA_VERSION)
