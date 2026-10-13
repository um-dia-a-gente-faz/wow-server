"""ADR 0018 slice 7 (#739): every released schema version of the memory store is a fixture that
opens with the real Memory, migrates to SCHEMA_VERSION and reads back the same rows.

fixtures/memory_db/v<N>.sql is the database as version N created it, frozen as text: its
CREATE TABLE is a copy, never an import of MIGRATIONS. Fixtures are append-only. Adding
migration N+1 means: append it to MIGRATIONS in agent/memory.py, add v<N+1>.sql (the v<N> script
with the step and user_version N+1), pin its sha256 in PINS and list its rows in EXPECTED,
EXPIRING and EXPIRED.

Before a store opens a fixture, the built database must pass two checks: PRAGMA user_version is
N, and the rows across all tables are the rows the test lists. For N == SCHEMA_VERSION the tables
and sqlite_master names must also equal those of a database the real store creates fresh
(table_info, not the sql text, so ALTER TABLE and an inline column compare equal).
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
NOW = 1_800_000_000.0          # the clock the rows are read at (Stale Gnoll expired before it)
FAR_FUTURE = 4102444800.0      # 2100-01-01T00:00:00Z
STALE = 1700000000.0

# sha256 of each shipped fixture, CRLF normalised. Pinned by hand: a changed fixture must fail.
PINS = {
    1: "96817d2d7e0464036c5ef974ed3c841fc883af9b4b62e2e6dc3637599578c988",
}
# What each fixture reads as at NOW: kind -> [(key, value)], newest first, as recent() returns it.
# A JSON null reads as None, the same as a missing row.
EXPECTED = {
    1: {
        "known_target": [("Springpaw Cub", {"x": 1.5, "guid": 42})],
        "goal": [("quest", "reach level 10")],
        "action": [("last_cycle", 17)],
        "failed_target": [("Razormane Scout", ["move_to", "timeout"]), ("Tauren Guard", None)],
    },
}
# Rows with a finite expires_at: (kind, key, expires_at, value). Read one second before and at expiry.
EXPIRING = {
    1: [("failed_target", "Razormane Scout", FAR_FUTURE, ["move_to", "timeout"]),
        ("failed_target", "Stale Gnoll", STALE, "old")],
}
# Rows already expired at NOW: recall and recent must not return them.
EXPIRED = {
    1: [("failed_target", "Stale Gnoll")],
}


class _Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _user_version(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _listed(n: int) -> int:
    return sum(len(rows) for rows in EXPECTED[n].values()) + len(EXPIRED[n])


def _never_expiring(n: int) -> list:
    expiring = {(kind, key) for kind, key, _, _ in EXPIRING[n]}
    return [(kind, key, value) for kind, rows in EXPECTED[n].items()
            for key, value in rows if (kind, key) not in expiring]


def _fixture_problems(path: str, n: int) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        rows = sum(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables)
    finally:
        conn.close()
    problems = []
    if version != n:
        problems.append(f"PRAGMA user_version is {version}: v{n}.sql must end with PRAGMA user_version = {n}")
    if rows == 0 or rows != _listed(n):
        problems.append(f"v{n}.sql holds {rows} rows, the test lists {_listed(n)}: list every row of the "
                        f"fixture in EXPECTED / EXPIRED (and at least one row)")
    return problems


def _shape(path: str):
    conn = sqlite3.connect(path)
    try:
        master = conn.execute("SELECT type, name FROM sqlite_master ORDER BY type, name").fetchall()
        info = {name: conn.execute(f'PRAGMA table_info("{name}")').fetchall()
                for kind, name in master if kind == "table"}
    finally:
        conn.close()
    return master, info


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
            for name, table in (("EXPECTED", EXPECTED), ("EXPIRING", EXPIRING), ("EXPIRED", EXPIRED)):
                if n not in table:
                    problems.append(f"v{n}.sql has no entry in {name}: list its rows")
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
        self.assertEqual(_fixture_problems(path, n), [])
        if n == memory.SCHEMA_VERSION:
            self.assertEqual(_shape(path), self._fresh_shape(),
                             f"v{n}.sql: tables or sqlite_master names differ from a database the store creates fresh")
        return path

    def _fresh_shape(self):
        path = os.path.join(self._tmp.name, "fresh.db")
        Memory(path, now=_Clock(NOW)).close()
        return _shape(path)

    def _open(self, path: str, clock: _Clock) -> Memory:
        m = Memory(path, now=clock)
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
                clock = _Clock(NOW)
                m = self._open(path, clock)
                for kind, rows in EXPECTED[n].items():
                    self.assertEqual(m.recent(kind, 10), rows, kind)
                    for key, value in rows:
                        self.assertEqual(m.recall(kind, key), value, key)
                for kind, key in EXPIRED[n]:
                    self.assertIsNone(m.recall(kind, key), key)
                    self.assertNotIn(key, [k for k, _ in m.recent(kind, 10)])
                # The TTL rows: read one second before expires_at, gone at expires_at.
                for kind, key, expires, value in EXPIRING[n]:
                    clock.t = expires - 1
                    self.assertEqual(m.recall(kind, key), value, f"{key}, one second before expires_at")
                    clock.t = expires
                    self.assertIsNone(m.recall(kind, key), f"{key}, at expires_at")
                # Rows without expires_at still read after every fixture expiry.
                clock.t = max(expires for _, _, expires, _ in EXPIRING[n]) + 1
                for kind, key, value in _never_expiring(n):
                    self.assertEqual(m.recall(kind, key), value, f"{key} (no expires_at), after every expiry")
                # A write after migration, one of them over a key the fixture holds.
                clock.t = NOW
                m.remember("goal", "after_migration", {"fixture": n})
                m.remember("known_target", "Springpaw Cub", {"overwritten": n})
                m.flush()
                self.assertEqual(m.recall("goal", "after_migration"), {"fixture": n})
                self.assertEqual(m.recall("known_target", "Springpaw Cub"), {"overwritten": n})
                self.assertEqual(m.recent("known_target", 10), [("Springpaw Cub", {"overwritten": n})])
                m.close()
                self.assertFalse(m._thread.is_alive(), "the writer thread outlived close()")
                self.assertEqual(_user_version(path), memory.SCHEMA_VERSION)
