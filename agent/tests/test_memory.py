"""agent/memory.py (#276): round trip, TTL, order, corrupt and too-new files, migration, writer thread."""
import os
import sqlite3
import tempfile
import threading
import time
import unittest

from agent import memory
from agent.memory import Memory


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class MemoryTests(unittest.TestCase):
    JOIN_BOUND_S = 10
    FLUSH_BOUND_S = 60  # a slow CI run takes up to 19 s here; far below the 5 min job limit

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "memory.db")
        self.clock = _Clock()

    def _flush(self, m: Memory, stop: threading.Event | None = None) -> None:
        """m.flush() with a bound: it waits for the writer thread for as long as that thread lives (#691)."""
        t = threading.Thread(target=m.flush, daemon=True)
        t.start()
        t.join(self.FLUSH_BOUND_S)
        if t.is_alive():
            if stop is not None:
                stop.set()  # a reader hogging the lock may be what starves the writer
            self.fail(f"flush() still waiting for the writer after {self.FLUSH_BOUND_S:g}s")

    def _reap(self, stop: threading.Event, *threads: threading.Thread) -> None:
        """Stop the spinners and join every thread, bounded; fail by name if one stays (#691). Safe to call twice."""
        stop.set()
        stuck = []
        for t in threads:
            t.join(self.JOIN_BOUND_S)
            if t.is_alive():
                stuck.append(t.name)
        if stuck:
            self.fail(f"threads still alive after {self.JOIN_BOUND_S:g}s: {stuck}")

    def _start_readers(self, m: Memory, n: int) -> tuple[threading.Event, list, list]:
        """n threads named 'reader' that hammer m until stopped or until m starts closing. Reaped at cleanup,
        whatever the test does, so a failing assertion cannot leave them spinning for the rest of the process.
        Returns (stop, errors, readers)."""
        stop = threading.Event()
        errors: list = []

        def reader():
            try:
                # A closed store answers without a lock, so a reader that kept calling would only spin on the GIL
                # and starve the writer and the closer: stop when close() begins.
                while not stop.is_set() and not m._closed:
                    m.recall("action", "k5")
                    m.recent("action", 10)
            except Exception as e:   # the reader must never raise either
                errors.append(e)

        readers = [threading.Thread(target=reader, name="reader", daemon=True) for _ in range(n)]
        self.addCleanup(self._reap, stop, *readers)
        for r in readers:
            r.start()
        return stop, errors, readers

    def _open(self, path=None) -> Memory:
        m = Memory(self.path if path is None else path, now=self.clock)
        self.addCleanup(m.close)
        return m

    def test_round_trip_survives_reopen(self):
        m = self._open()
        m.remember("known_target", "Springpaw Cub", {"x": 1.5, "guid": 42})
        self._flush(m)
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
        self._flush(m)
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
        self._flush(m)
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
        self._flush(m)
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
        self._flush(m)
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
        self._flush(m0)
        m0.close()
        again = self._open(empty)
        self.assertEqual(again.recall("goal", "x"), "kept")

    def test_missing_directory_is_created(self):
        nested = os.path.join(self._tmp.name, "agent", "memory.db")
        m = self._open(nested)
        m.remember("goal", "x", 3)
        self._flush(m)
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
        self._flush(m)
        self.assertEqual(m.recall("known_target", "a"), 1)

    def test_a_flush_that_never_returns_fails_the_test_by_name(self):
        m = self._open()
        m._io.acquire()                        # the writer cannot take its next write, so flush() waits
        self.addCleanup(lambda: m._io.locked() and m._io.release())
        m.remember("goal", "x", 1)
        self.FLUSH_BOUND_S = 0.3
        with self.assertRaisesRegex(self.failureException, "flush.. still waiting"):
            self._flush(m)

    def test_close_stops_the_writer_thread_and_public_methods_stay_quiet(self):
        m = self._open()
        m.remember("goal", "x", 1)
        m.close()
        self.assertFalse(m._thread.is_alive())
        self._flush(m)                              # returns, does not hang
        m.remember("goal", "y", 2)             # dropped, no raise
        self.assertIsNone(m.recall("goal", "y"))
        self.assertEqual(m.recent("goal", 5), [])

    def test_unserialisable_value_is_dropped_not_raised(self):
        m = self._open()
        m.remember("goal", "bad", object())
        m.remember("goal", "ok", "fine")
        self._flush(m)
        self.assertIsNone(m.recall("goal", "bad"))
        self.assertEqual(m.recall("goal", "ok"), "fine")

    def test_in_memory_store_when_no_path(self):
        m = Memory(None, now=self.clock)
        self.addCleanup(m.close)
        m.remember("goal", "x", "y")
        self._flush(m)
        self.assertEqual(m.recall("goal", "x"), "y")

    def test_in_memory_store_keeps_every_write_under_concurrent_reads(self):
        # The shared-cache in-memory DB used to drop rows here ("database table is locked").
        n = 10000
        for _trial in range(3):
            m = Memory(None, now=self.clock)
            self.addCleanup(m.close)
            stop, errors, readers = self._start_readers(m, 1)
            for i in range(n):
                m.remember("action", f"k{i}", i)
            self._flush(m, stop)                   # reads overlap the writes: flush before stopping the reader
            self._reap(stop, *readers)
            self.assertEqual(errors, [])
            self.assertEqual(len(m.recent("action", n + 10)), n)
            self.assertEqual(m.recall("action", f"k{n - 1}"), n - 1)

    def test_close_while_readers_run_on_the_in_memory_store_neither_crashes_nor_hangs(self):
        # The writer used to close the shared in-memory connection without the lock the readers hold.
        m = Memory(None, now=self.clock)
        for i in range(2000):
            m.remember("action", f"k{i}", i)
        self._flush(m)
        stop, errors, readers = self._start_readers(m, 3)
        closer = threading.Thread(target=m.close, name="closer", daemon=True)
        self.addCleanup(self._reap, stop, closer)
        closer.start()
        closer.join(5)
        self.assertFalse(closer.is_alive(), "close() hung while readers ran")
        self._reap(stop, *readers)
        self.assertEqual(errors, [])

    def test_a_read_that_waited_for_the_lock_while_close_began_returns_nothing_quietly(self):
        m = Memory(None, now=self.clock)
        m.remember("goal", "x", 1)
        self._flush(m)
        got = []
        m._read_lock.acquire()                 # the reader passes the closed check, then waits here
        t = threading.Thread(target=lambda: got.append(m.recall("goal", "x")), name="late-reader", daemon=True)
        self.addCleanup(self._reap, threading.Event(), t)
        t.start()
        time.sleep(0.2)
        m._closed = True                       # what close() does first
        m._reader.close()                      # ... and what it does under the lock
        with self.assertNoLogs("agent.memory", "WARNING"):
            m._read_lock.release()
            t.join(self.JOIN_BOUND_S)
        self.assertEqual(got, [None])

    def test_writes_queued_before_close_reach_the_file_without_flush(self):
        m = self._open()
        for i in range(500):
            m.remember("action", f"k{i}", i)
        m.close()                              # no flush(): close must drain the queue itself
        again = self._open()
        self.assertEqual(len(again.recent("action", 1000)), 500)
        self.assertEqual(again.recall("action", "k499"), 499)

    def test_writer_thread_is_a_daemon(self):
        m = self._open()
        self.assertTrue(m._thread.daemon)


if __name__ == "__main__":
    unittest.main()
