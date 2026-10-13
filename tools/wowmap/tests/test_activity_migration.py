"""ADR 0018 slice 7 (#739): every released schema version of the activity store is a fixture that
opens with the real ActivityStore, migrates to SCHEMA_VERSION and reads back the same rows.

fixtures/activity_db/v<N>.sql is the database as version N created it, frozen as text: its
tables are a copy of SCHEMA, never an import of it. v0 is the file from before user_version was
set. Fixtures are append-only. Adding migration N+1 means: append it to MIGRATIONS in
activity_schema.py, add v<N+1>.sql (the v<N> script with the step and user_version N+1), pin its
sha256 in PINS and list its rows in ROWS and META under N+1.

Before a store opens a fixture, the built database must pass two checks: PRAGMA user_version is
N, and the rows across all tables are the rows the test lists. For N == SCHEMA_VERSION the tables
and sqlite_master names must also equal those of a database the real store creates fresh
(table_info, not the sql text, so ALTER TABLE and an inline column compare equal).
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
    0: "6025d359154d48f92556b4ff262068744ea246079df04a3c97ac4999f97ebb03",
    1: "e94057e74c6a9ef29836ad3009e4bf0cf759fd50527282c6520088cdc15d652a",
}
# A row both fixtures hold: a uid that is in the fixture, re-added by the test (add() must refuse it).
DUP_UID = "db:1:104.000:money:"
# What recent(character, 50) returns at read time, newest first, for the fixtures' rows.
# 'Rubens' and 'rubens' are one character to the store (the column is NOCASE), so both sets are here.
_ROWS_V1 = {
    "Rubens": [
        {"t": 105.0, "kind": "item", "text": "Got Linen Cloth ×2", "detail": {"item": 2589, "count": 2},
         "source": "db", "inferred": False},
        {"t": 105.0, "kind": "item", "text": "Got Refreshing Spring Water ×5",
         "detail": {"item": 159, "count": 5}, "source": "db", "inferred": False},
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
_META_V1 = {"chat_last_id": "1700000000123-0"}
ROWS = {0: _ROWS_V1, 1: _ROWS_V1}
META = {0: _META_V1, 1: _META_V1}


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _user_version(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _listed(n: int) -> int:
    return sum(len(rows) for rows in ROWS[n].values()) + len(META[n])


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
                        f"fixture in ROWS / META (and at least one row)")
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
            for name, table in (("ROWS", ROWS), ("META", META)):
                if n not in table:
                    problems.append(f"v{n}.sql has no entry in {name}: list its rows")
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
        self.assertEqual(_fixture_problems(path, n), [])
        if n == activity_schema.SCHEMA_VERSION:
            self.assertEqual(_shape(path), self._fresh_shape(),
                             f"v{n}.sql: tables or sqlite_master names differ from a database the store creates fresh")
        return path

    def _fresh_shape(self):
        path = os.path.join(self._tmp.name, "fresh.sqlite3")
        store = activity.ActivityStore(path)
        store._conn.close()
        return _shape(path)

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
                for character, rows in ROWS[n].items():
                    for name in (character, character.lower(), character.upper()):
                        self.assertEqual(store.recent(name, 50), rows)
                        self.assertEqual(store.count(name), len(rows))
                for key, value in META[n].items():
                    self.assertEqual(store.get_meta(key), value)
                self.assertEqual(store.recent("Nobody", 50), [])
                # A uid the fixture holds is refused: nothing is added, the count is unchanged.
                before = store.count("Rubens")
                self.assertEqual(store.add([activity.event("Rubens", 104.0, "money", "again", "db", DUP_UID)]), 0)
                self.assertEqual(store.count("Rubens"), before)
                self.assertEqual(store.add([activity.event("Rubens", 200.0, "money", "new write", "db",
                                                           f"new:{n}")]), 1)
                self.assertEqual(store.recent("Rubens", 1)[0]["text"], "new write")
                store._conn.close()
                self.assertEqual(_user_version(path), activity_schema.SCHEMA_VERSION)
