"""agent/worldknowledge.py (ADR 0016, slice 1): lookups, the four unavailable cases, threads, read-only."""
import os
import sqlite3
import tempfile
import threading
import time
import unittest

from agent import worldknowledge as wk

SPELL = (133, "Fireball", "Rank 1", 1, 0, 30, 0, 1500, 0.0, 35.0, 35.0)


def make_bundle(path, version=wk.SCHEMA_VERSION):
    conn = sqlite3.connect(path)
    conn.executescript(wk.SCHEMA)
    conn.execute("INSERT INTO meta VALUES ('client_build', '12340')")
    conn.execute("INSERT INTO tables VALUES ('spell', 'client')")
    conn.execute("INSERT INTO spell VALUES (?,?,?,?,?,?,?,?,?,?,?)", SPELL)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


class _Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class SlowConn:
    """Wraps the real connection (or none, then answers SPELL itself); execute() sleeps so
    overlapping calls show up. Without a real connection SQLite is never touched from two threads."""

    def __init__(self, real, on_execute=None, delay=0.005):
        self.real = real
        self.on_execute = on_execute
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self._count = threading.Lock()

    def execute(self, sql, params=()):
        with self._count:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.on_execute:
                self.on_execute()
            time.sleep(self.delay)
            return _Cursor(SPELL) if self.real is None else self.real.execute(sql, params)
        finally:
            with self._count:
                self.in_flight -= 1

    def close(self):
        if self.real is not None:
            self.real.close()


class RaisingConn(SlowConn):
    def execute(self, sql, params=()):
        raise sqlite3.OperationalError("disk I/O error")


class WorldKnowledgeTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        self.path = os.path.join(d.name, "world.sqlite3")

    def opened(self):
        r = wk.WorldKnowledge(self.path)
        self.addCleanup(r.close)
        return r

    def test_known_and_unknown_spell(self):
        make_bundle(self.path)
        r = self.opened()
        self.assertTrue(r.available)
        self.assertEqual(r.spell(133), dict(zip(wk.SPELL_COLUMNS, SPELL, strict=True)))
        self.assertIsNone(r.spell(999999))
        self.assertEqual(r.meta(), {"client_build": "12340"})

    def test_unavailable_cases_warn_once_and_answer_nothing(self):
        make_bundle(os.path.join(self.dir, "v2.sqlite3"), version=2)
        with open(os.path.join(self.dir, "junk"), "wb") as f:
            f.write(b"this is not a sqlite file" * 50)
        cases = {"none": None, "missing": os.path.join(self.dir, "nope.sqlite3"),
                 "not sqlite": os.path.join(self.dir, "junk"), "other version": os.path.join(self.dir, "v2.sqlite3")}
        for name, path in cases.items():
            with self.subTest(name), self.assertLogs("agent.worldknowledge", "WARNING") as logs:
                r = wk.WorldKnowledge(path)
                self.assertFalse(r.available)
                self.assertIsNone(r.spell(133))
                self.assertEqual(r.meta(), {})
                r.close()
            self.assertEqual(len(logs.records), 1)

    def test_missing_path_is_not_created(self):
        with self.assertLogs("agent.worldknowledge", "WARNING"):
            wk.WorldKnowledge(self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_lookup_from_another_thread(self):
        make_bundle(self.path)
        r = self.opened()
        out = []
        t = threading.Thread(target=lambda: out.append(r.spell(133)))
        t.start()
        t.join()
        self.assertEqual(out[0]["name"], "Fireball")

    def test_connection_is_read_only_and_bytes_unchanged(self):
        make_bundle(self.path)
        with open(self.path, "rb") as f:
            before = f.read()
        r = self.opened()
        r.spell(133)
        with self.assertRaises(sqlite3.OperationalError):
            r._conn.execute("DELETE FROM spell")
        r.close()
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_closed_reader_answers_nothing(self):
        make_bundle(self.path)
        r = self.opened()
        r.close()
        self.assertFalse(r.available)
        self.assertIsNone(r.spell(133))

    def test_spell_table_dropped_or_column_missing_is_unavailable(self):
        shapes = {"table dropped": "DROP TABLE spell",
                  "column missing": "ALTER TABLE spell DROP COLUMN cast_time_ms"}
        for name, sql in shapes.items():
            path = os.path.join(self.dir, name.replace(" ", "_") + ".sqlite3")
            make_bundle(path)
            conn = sqlite3.connect(path)
            conn.execute(sql)
            conn.commit()
            conn.close()
            with open(path, "rb") as f:
                before = f.read()
            with self.subTest(name), self.assertLogs("agent.worldknowledge", "WARNING") as logs:
                r = wk.WorldKnowledge(path)
                self.assertFalse(r.available)
                self.assertIsNone(r.spell(133))
                self.assertEqual(r.meta(), {})
                r.close()
            self.assertEqual(len(logs.records), 1)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), before)

    def test_lookups_are_serialised(self):
        make_bundle(self.path)
        r = self.opened()
        fake = SlowConn(None)
        r._conn = fake
        threads = [threading.Thread(target=r.spell, args=(133,)) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(fake.max_in_flight, 1)

    def test_lookups_racing_close_raise_nothing(self):
        make_bundle(self.path)
        r = self.opened()
        errors = []

        def look():
            try:
                for _ in range(200):
                    r.spell(133)
            except Exception as e:  # noqa: BLE001 - the point of the test: nothing may escape
                errors.append(e)

        threads = [threading.Thread(target=look) for _ in range(16)]
        for t in threads:
            t.start()
        r.close()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_close_waits_for_a_lookup_in_progress(self):
        make_bundle(self.path)
        r = wk.WorldKnowledge(self.path)
        entered = threading.Event()
        busy_at_close = []

        class Probe(SlowConn):
            def close(self):
                busy_at_close.append(self.in_flight)
                self.real.close()

        r._conn = Probe(r._conn, on_execute=entered.set, delay=0.2)
        t = threading.Thread(target=r.spell, args=(133,))
        t.start()
        self.assertTrue(entered.wait(5))
        r.close()
        t.join()
        self.assertEqual(busy_at_close, [0])

    def test_sqlite_error_during_lookup_answers_none(self):
        make_bundle(self.path)
        r = self.opened()
        r._conn = RaisingConn(r._conn)
        self.assertIsNone(r.spell(133))

    def test_overflowing_spell_id_answers_none(self):
        make_bundle(self.path)
        r = self.opened()
        self.assertIsNone(r.spell(2 ** 70))


if __name__ == "__main__":
    unittest.main()
