"""agent/worldknowledge.py (ADR 0016, slice 1): lookups, the four unavailable cases, threads, read-only."""
import os
import sqlite3
import tempfile
import threading
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


if __name__ == "__main__":
    unittest.main()
