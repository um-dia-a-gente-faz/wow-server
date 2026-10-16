"""ADR 0018 slice 7 (#739): every released schema version of the activity store is a fixture that
opens with the real ActivityStore, migrates to SCHEMA_VERSION and reads back the same rows.

fixtures/activity_db/v<N>.sql is the database as version N created it, frozen as text: its
tables are a copy of SCHEMA, never an import of it. v0 is the file from before user_version was
set. Fixtures are append-only. Adding migration N+1 means: append it to MIGRATIONS in
activity_schema.py, add v<N+1>.sql (the v<N> script with the step and user_version N+1), pin its
sha256 in PINS and list its rows in ROWS and META under N+1.

Before a store opens a fixture, the built database must pass three checks: PRAGMA user_version is
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
    0: "a5d9db65b9d2eee07a1dd6dc5583a7f40c9f6f0c550e071f68d665eac415d02e",
    1: "e3a3be70b578c367b1618959dd3df29c96cea4c2b3f20b95f3b17ecea53c843a",
    2: "bae655c2b9eb420f7c707c5775c560beede591d5576241eb32343d76b51d4e1d",
}
# A row both fixtures hold: a uid that is in the fixture, re-added by the test (add() must refuse it).
DUP_UID = "db:1:101.000:money:"
# What recent(character, 50) returns at read time, newest first, for the fixtures' rows.
# 'Rubens' and 'rubens' are one character to the store (the column is NOCASE), so both sets are here.
_ROWS_V1 = {
    "Rubens": [
        {"t": 105.0, "kind": "item", "text": "Got Linen Cloth ×2", "detail": {"item": 2589, "count": 2},
         "source": "db", "inferred": False},
        {"t": 105.0, "kind": "item", "text": "Got Refreshing Spring Water ×5",
         "detail": {"item": 159, "count": 5}, "source": "db", "inferred": False},
        {"t": 102.0, "kind": "chat", "text": 'Said "hi"', "detail": {"kind": "say", "channel": None},
         "source": "chat", "inferred": False},
        {"t": 101.0, "kind": "money", "text": "Money +26c", "detail": {"delta": 26, "total": 1026},
         "source": "db", "inferred": False},
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
_META_V1 = {"chat_last_id": "2490369:5120"}
# v2 adds the fact_key column (ADR 0020 slice 3, #806) and one agent turn-in, which carries its key.
_ROWS_V2 = {
    "Rubens": [*_ROWS_V1["Rubens"][:2],
               {"t": 104.0, "kind": "quest", "text": "Turned in quest: Quest 8325", "detail": {"quest_id": 8325},
                "source": "agent", "inferred": False},
               *_ROWS_V1["Rubens"][2:]],
    "Thrall": _ROWS_V1["Thrall"],
}
ROWS = {0: _ROWS_V1, 1: _ROWS_V1, 2: _ROWS_V2}
META = {0: _META_V1, 1: _META_V1, 2: _META_V1}
# fact_key of every event row, by uid: what the column holds after the step-2 backfill (v0, v1) or as
# the store wrote it (v2). A row with no fact (another kind, or a hand-in) holds NULL.
_FACT_KEYS_V1 = {
    "db:1:100.000:level:": "11",
    "db:1:101.000:sell:-2589": None,
    "db:1:101.000:money:": None,
    "chat:2490369:5120": None,
    "audit:Thrall:4:103.5": None,
    "db:1:105.000:item:+159": None,
    "db:1:105.000:item:+2589": None,
}
_FACT_KEYS_V2 = {**_FACT_KEYS_V1, "agent:Rubens:quest:104.000:8325": "8325"}


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


def _fact_keys(path: str) -> dict:
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT uid, fact_key FROM events"))
    finally:
        conn.close()


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
                self.assertEqual(store.add([activity.event("Rubens", 101.0, "money", "again", "db", DUP_UID)]), 0)
                self.assertEqual(store.count("Rubens"), before)
                self.assertEqual(store.add([activity.event("Rubens", 200.0, "money", "new write", "db",
                                                           f"new:{n}")]), 1)
                self.assertEqual(store.recent("Rubens", 1)[0]["text"], "new write")
                store._conn.close()
                self.assertEqual(_user_version(path), activity_schema.SCHEMA_VERSION)

    def test_fact_key_is_backfilled_from_the_detail_of_old_rows(self):
        for n, expected in ((0, _FACT_KEYS_V1), (1, _FACT_KEYS_V1), (2, _FACT_KEYS_V2)):
            with self.subTest(fixture=f"v{n}.sql"):
                path = self._from_fixture(n)
                self._open(path)
                self.assertEqual(_fact_keys(path), expected)

    def test_an_old_agent_row_gets_its_fact_key_when_migrated(self):
        # A v1 store holding agent rows (written before the column existed): the step backfills `to`
        # for a level and `quest_id` for a quest.
        path = self._from_fixture(1)
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO events (uid, character, t, kind, text, detail_json, source, inferred)"
                     " VALUES ('agent:Rubens:level:200.000:12', 'Rubens', 200.0, 'level', 'Reached level 12',"
                     " '{\"to\": 12}', 'agent', 0)")
        conn.execute("INSERT INTO events (uid, character, t, kind, text, detail_json, source, inferred)"
                     " VALUES ('agent:Rubens:quest:201.000:8325', 'Rubens', 201.0, 'quest',"
                     " 'Turned in quest: Quest 8325', '{\"quest_id\": 8325}', 'agent', 0)")
        conn.commit()
        conn.close()
        self._open(path)
        keys = _fact_keys(path)
        self.assertEqual(keys["agent:Rubens:level:200.000:12"], "12")
        self.assertEqual(keys["agent:Rubens:quest:201.000:8325"], "8325")

    def test_a_backfilled_db_level_row_is_deleted_by_an_agent_level_within_the_window(self):
        for n in (0, 1):
            with self.subTest(fixture=f"v{n}.sql"):
                path = self._from_fixture(n)
                store = self._open(path)
                agent = activity.agent_events({"events": [{"kind": "level", "t": 100.5, "detail": {"to": 11}}]},
                                              "Rubens")
                self.assertEqual(store.add(agent), 1)
                self.assertEqual([(r["source"], r["text"]) for r in store.recent("Rubens", 50)
                                  if r["kind"] == "level"], [("agent", "Reached level 11")])
                self.assertNotIn("db:1:100.000:level:", _fact_keys(path))
