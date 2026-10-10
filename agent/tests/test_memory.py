"""agent/memory.py (#276): round trip, TTL, order, corrupt and too-new files, migration, writer thread."""
import os
import sqlite3
import tempfile
import threading
import unittest

from agent import memory
from agent.memory import Memory


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "memory.db")
        self.clock = _Clock()

    def _open(self, path=None) -> Memory:
        m = Memory(self.path if path is None else path, now=self.clock)
        self.addCleanup(m.close)
        return m

    def test_round_trip_survives_reopen(self):
        m = self._open()
        m.remember("known_target", "Springpaw Cub", {"x": 1.5, "guid": 42})
        m.flush()
        self.assertEqual(m.recall("known_target", "Springpaw Cub"), {"x": 1.5, "guid": 42})
        self.assertIsNone(m.recall("known_target", "missing"))
        self.assertIsNone(m.recall("goal", "Springpaw Cub"))
        m.close()
        again = self._open()
        self.assertEqual(again.recall("known_target", "Springpaw Cub"), {"x": 1.5, "guid": 42})

    def test_ttl_expires_on_recall_and_recent(self):
        m = self._open()
        m.remember("failed_target", "a", "stale", ttl=10)
        m.remember("failed_target", "b", "forever")
        m.flush()
        self.assertEqual(m.recall("failed_target", "a"), "stale")
        self.clock.t += 10          # expires_at <= now counts as expired
        self.assertIsNone(m.recall("failed_target", "a"))
        self.assertEqual(m.recent("failed_target", 5), [("b", "forever")])
        self.assertEqual(m.recall("failed_target", "b"), "forever")

    def test_recent_is_newest_first_and_limited(self):
        m = self._open()
        for key in ("a", "b", "c"):
            m.remember("action", key, key.upper())
        m.remember("action", "a", "A2")      # rewriting a key makes it the newest
        m.remember("other", "z", 0)
        m.flush()
        self.assertEqual(m.recent("action", 2), [("a", "A2"), ("c", "C")])
        self.assertEqual(m.recent("action", 10), [("a", "A2"), ("c", "C"), ("b", "B")])
        self.assertEqual(m.recent("action", 0), [])

    def test_corrupt_file_falls_back_to_empty_and_is_left_alone(self):
        garbage = b"this is not a sqlite database" * 50
        with open(self.path, "wb") as f:
            f.write(garbage)
        with self.assertLogs("agent.memory", "WARNING"):
            m = self._open()                   # must not raise, and says so once
        self.assertIsNone(m.recall("goal", "x"))
        self.assertEqual(m.recent("goal", 3), [])
        m.remember("goal", "x", 1)             # still usable, in memory
        m.flush()
        self.assertEqual(m.recall("goal", "x"), 1)
        m.close()
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), garbage)   # the bad file is not overwritten

    def test_newer_schema_version_falls_back_to_empty(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version = %d" % (memory.SCHEMA_VERSION + 1))
        conn.commit()
        conn.close()
        with self.assertLogs("agent.memory", "WARNING"):
            m = self._open()
        self.assertIsNone(m.recall("goal", "x"))
        m.remember("goal", "x", 2)
        m.flush()
        self.assertEqual(m.recall("goal", "x"), 2)

    def test_migrates_fresh_and_version_zero_files(self):
        m = self._open()
        m.close()
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], memory.SCHEMA_VERSION)
        conn.close()
        # a file at version 0 with nothing in it is migrated on open, and reopening is a no-op
        empty = os.path.join(self._tmp.name, "v0.db")
        sqlite3.connect(empty).close()
        m0 = Memory(empty, now=self.clock)
        m0.remember("goal", "x", "kept")
        m0.flush()
        m0.close()
        again = self._open(empty)
        self.assertEqual(again.recall("goal", "x"), "kept")

    def test_missing_directory_is_created(self):
        nested = os.path.join(self._tmp.name, "agent", "memory.db")
        m = self._open(nested)
        m.remember("goal", "x", 3)
        m.flush()
        self.assertTrue(os.path.exists(nested))

    def test_remember_returns_while_the_writer_is_blocked(self):
        m = self._open()
        m._io.acquire()                        # stall the writer thread on its next write
        self.addCleanup(lambda: m._io.locked() and m._io.release())   # if the test fails before releasing
        t = threading.Thread(target=m.remember, args=("known_target", "a", 1), daemon=True)
        t.start()
        t.join(2)
        self.assertFalse(t.is_alive(), "remember waited on the writer")
        m._io.release()
        m.flush()
        self.assertEqual(m.recall("known_target", "a"), 1)

    def test_close_stops_the_writer_thread_and_public_methods_stay_quiet(self):
        m = self._open()
        m.remember("goal", "x", 1)
        m.close()
        self.assertFalse(m._thread.is_alive())
        m.flush()                              # returns, does not hang
        m.remember("goal", "y", 2)             # dropped, no raise
        self.assertIsNone(m.recall("goal", "y"))
        self.assertEqual(m.recent("goal", 5), [])

    def test_unserialisable_value_is_dropped_not_raised(self):
        m = self._open()
        m.remember("goal", "bad", object())
        m.remember("goal", "ok", "fine")
        m.flush()
        self.assertIsNone(m.recall("goal", "bad"))
        self.assertEqual(m.recall("goal", "ok"), "fine")

    def test_in_memory_store_when_no_path(self):
        m = Memory(None, now=self.clock)
        self.addCleanup(m.close)
        m.remember("goal", "x", "y")
        m.flush()
        self.assertEqual(m.recall("goal", "x"), "y")


if __name__ == "__main__":
    unittest.main()
