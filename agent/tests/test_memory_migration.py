"""ADR 0018 slice 7 (#739): every released schema version of the memory store is a fixture that
opens with the real Memory, migrates to SCHEMA_VERSION and reads back the same rows.

fixtures/memory_db/v<N>.sql is the database as version N created it, frozen as text: its
CREATE TABLE is a copy, never an import of MIGRATIONS. Fixtures are append-only. Adding
migration N+1 means: append it to MIGRATIONS in agent/memory.py, add v<N+1>.sql, pin its
sha256 in PINS and its rows in EXPECTED / EXPIRED.
"""
import hashlib
import os
import pathlib
import sqlite3
import tempfile
import unittest

from agent import memory
from agent.memory import Memory

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "memory_db"
NOW = 1_800_000_000.0   # the clock the rows are read at (Stale Gnoll expired before it)

# sha256 of each shipped fixture, CRLF normalised. Pinned by hand: a changed fixture must fail.
PINS = {
    1: "f373c5e72c07b529fe8ddb09d4811a63fd6cc33f29de4112f29d3b723879ccea",
}
# What each fixture reads as at NOW: kind -> [(key, value)], newest first, as recent() returns it.
# A JSON null reads as None, the same as a missing row.
EXPECTED = {
    1: {
        "known_target": [("Springpaw Cub", {"x": 1.5, "guid": 42})],
        "goal": [("quest", "reach level 10")],
        "action": [("last_cycle", 17)],
        "failed_target": [("Tauren Guard", None), ("Razormane Scout", ["move_to", "timeout"])],
    },
}
# Rows that are expired at NOW: recall and recent must not return them.
EXPIRED = {
    1: [("failed_target", "Stale Gnoll")],
}


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

    def test_every_version_from_1_to_schema_version_has_a_fixture(self):
        missing = [n for n in range(1, memory.SCHEMA_VERSION + 1) if not (FIXTURES / f"v{n}.sql").is_file()]
        self.assertEqual(missing, [], "\n".join(
            f"no fixtures/memory_db/v{n}.sql: write the database as version {n} creates it, with rows that "
            f"exercise what migration {n} added, then pin it and list its rows. A shipped fixture is never edited."
            for n in missing))

    def test_every_version_is_pinned_and_has_its_rows(self):
        problems = []
        for n in range(1, memory.SCHEMA_VERSION + 1):
            if n not in PINS:
                path = FIXTURES / f"v{n}.sql"
                pin = f"PINS[{n}] = {_digest(path)!r}" if path.is_file() else "the fixture first"
                problems.append(f"v{n}.sql has no sha256 pin: add {pin}")
            if n not in EXPECTED:
                problems.append(f"v{n}.sql has no rows: add EXPECTED[{n}] and EXPIRED[{n}]")
        self.assertEqual(problems, [])


class MemoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _from_fixture(self, n: int) -> str:
        path = os.path.join(self._tmp.name, f"v{n}.db")
        conn = sqlite3.connect(path)
        try:
            conn.executescript((FIXTURES / f"v{n}.sql").read_text(encoding="utf-8"))
        finally:
            conn.close()
        return path

    def _open(self, path: str) -> Memory:
        m = Memory(path, now=lambda: NOW)
        self.addCleanup(m.close)
        return m

    def test_shipped_fixtures_are_frozen(self):
        for n, pinned in sorted(PINS.items()):
            digest = _digest(FIXTURES / f"v{n}.sql")
            self.assertEqual(digest, pinned,
                             f"v{n}.sql differs from its pin. Fixtures are append-only: a shipped fixture is "
                             f"never edited, restore it from git. Only a NEW fixture is pinned: "
                             f"PINS[{n}] = {digest!r}")

    def test_every_fixture_opens_migrates_and_reads_back(self):
        for n in range(1, memory.SCHEMA_VERSION + 1):
            with self.subTest(fixture=f"v{n}.sql"):
                path = self._from_fixture(n)
                m = self._open(path)
                for kind, rows in EXPECTED[n].items():
                    self.assertEqual(m.recent(kind, 10), rows)
                    for key, value in rows:
                        self.assertEqual(m.recall(kind, key), value)
                for kind, key in EXPIRED[n]:
                    self.assertIsNone(m.recall(kind, key))
                    self.assertNotIn(key, [k for k, _ in m.recent(kind, 10)])
                m.remember("goal", "after_migration", {"fixture": n})
                m.flush()
                self.assertEqual(m.recall("goal", "after_migration"), {"fixture": n})
                m.close()
                self.assertFalse(m._thread.is_alive(), "the writer thread outlived close()")
                self.assertEqual(_user_version(path), memory.SCHEMA_VERSION)
