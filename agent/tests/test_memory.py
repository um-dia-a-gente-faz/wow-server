"""agent/memory.py (#276): round trip, TTL, order, corrupt and too-new files, migration, writer thread."""
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

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

    def _flush(self, m: Memory, stop: threading.Event | None = None, release=lambda: None) -> None:
        """m.flush() bounded by FLUSH_BOUND_S; the bounded method itself must confirm (#691, #708)."""
        if not self._bounded(release, m.flush, self.FLUSH_BOUND_S, bound=self.FLUSH_BOUND_S + self.JOIN_BOUND_S, stop=stop):
            if stop is not None:
                stop.set()  # a reader hogging the lock may be what starves the writer
            self.fail(f"flush() not confirmed after {self.FLUSH_BOUND_S:g}s")

    def _bounded(self, release, fn, *args, bound=None, stop=None, **kw):
        """fn(*args) in a daemon thread, joined within `bound` (default JOIN_BOUND_S, never less than the timeout
        handed to the call). When it is still running, set `stop`, call `release` (undo the stall), join again
        and fail by name: a call that must time out cannot hang the suite (#708)."""
        bound = self.JOIN_BOUND_S if bound is None else bound
        out: list = []

        def run():
            try:
                out.append(fn(*args, **kw))
            except Exception as e:
                out.append(e)

        t = threading.Thread(target=run, name="bounded-call", daemon=True)
        t.start()
        t.join(bound)
        if t.is_alive():
            if stop is not None:
                stop.set()
            release()
            t.join(self.JOIN_BOUND_S)
            self.fail(f"{getattr(fn, '__name__', fn)}() did not return within {bound:g}s")
        if isinstance(out[0], Exception):
            raise out[0]
        return out[0]

    @staticmethod
    def _release_io(m: Memory):
        return lambda: m._io.locked() and m._io.release()

    @staticmethod
    def _release_read(m: Memory):
        return lambda: m._read_lock.locked() and m._read_lock.release()

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

    def _start_closer(self, close, stop: threading.Event) -> threading.Thread:
        """Run close in a thread named 'closer', reaped at cleanup like the readers."""
        closer = threading.Thread(target=close, name="closer", daemon=True)
        self.addCleanup(self._reap, stop, closer)
        closer.start()
        return closer

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

    def test_flush_waits_for_a_busy_writer_longer_than_the_join_bound(self):
        # _flush must wait up to FLUSH_BOUND_S for a writer that is merely slow (CI took up to 19 s), however
        # short the join bound for stalled calls is.
        ran = unittest.TestResult()

        class Slow(MemoryTests):
            JOIN_BOUND_S = 0.2
            FLUSH_BOUND_S = 2

            def runTest(inner):
                m = inner._open()
                m._io.acquire()
                release = inner._release_io(m)
                inner.addCleanup(release)
                m.remember("goal", "x", 1)
                timer = threading.Timer(0.6, release)   # the writer is busy for 0.6 s, three join bounds
                timer.start()
                inner.addCleanup(timer.join)
                inner._flush(m, release=release)
                inner.assertEqual(m.recall("goal", "x"), 1)

        Slow().run(ran)
        self.assertEqual((ran.failures, ran.errors), ([], []))
        self.assertEqual(ran.testsRun, 1)

    def test_a_flush_that_never_returns_fails_the_test_by_name(self):
        m = self._open()
        m._io.acquire()                        # the writer cannot take its next write, so flush() waits
        self.addCleanup(lambda: m._io.locked() and m._io.release())
        m.remember("goal", "x", 1)
        self.FLUSH_BOUND_S = 0.3
        with self.assertLogs("agent.memory", "WARNING"), \
                self.assertRaisesRegex(self.failureException, r"flush.. not confirmed after 0.3s"):
            self._flush(m, release=self._release_io(m))

    def test_close_stops_the_writer_thread_and_public_methods_stay_quiet(self):
        m = self._open()
        m.remember("goal", "x", 1)
        m.close()
        self.assertFalse(m._thread.is_alive())
        self.assertFalse(m.flush())                 # returns at once, does not hang
        m.remember("goal", "y", 2)             # dropped, no raise
        self.assertIsNone(m.recall("goal", "y"))
        self.assertEqual(m.recent("goal", 5), [])

    def test_unserialisable_value_is_dropped_not_raised(self):
        m = self._open()
        with self.assertLogs("agent.memory", "WARNING") as logs:
            m.remember("goal", "bad", object())
        self.assertIn("is not JSON, dropped", logs.output[0])
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
        # Only a read already inside the connection when close() begins can meet that close, so one close
        # seldom shows it: repeat (a trial takes about 15 ms).
        for _trial in range(25):
            m = Memory(None, now=self.clock)
            for i in range(2000):
                m.remember("action", f"k{i}", i)
            self._flush(m)
            stop, errors, readers = self._start_readers(m, 3)
            closer = self._start_closer(m.close, stop)
            closer.join(5)
            self.assertFalse(closer.is_alive(), "close() hung while readers ran")
            self._reap(stop, *readers)
            self.assertEqual(errors, [])

    def test_a_thread_that_cannot_be_stopped_fails_the_test_by_name(self):
        release = threading.Event()
        self.addCleanup(release.set)
        stuck = threading.Thread(target=release.wait, name="stuck", daemon=True)
        stuck.start()
        self.JOIN_BOUND_S = 0.2
        with self.assertRaisesRegex(self.failureException, r"threads still alive after 0.2s: \['stuck'\]"):
            self._reap(threading.Event(), stuck)
        release.set()
        stuck.join(10)
        self.assertFalse(stuck.is_alive())

    def test_a_closer_that_hangs_is_named_at_cleanup(self):
        release = threading.Event()
        self.addCleanup(release.set)
        ran = unittest.TestResult()

        class Failing(MemoryTests):
            JOIN_BOUND_S = 0.2

            def runTest(inner):
                inner._start_closer(release.wait, threading.Event())

        Failing().run(ran)
        self.assertEqual(len(ran.failures), 1)
        self.assertIn("['closer']", ran.failures[0][1])

    def test_readers_are_stopped_at_cleanup_when_the_test_fails_first(self):
        m = Memory(None, now=self.clock)
        self.addCleanup(m.close)
        ran = unittest.TestResult()

        class Failing(MemoryTests):
            def runTest(inner):
                self.readers = inner._start_readers(m, 2)[2]
                inner.fail("before stop.set()")

        Failing().run(ran)
        self.assertEqual(len(ran.failures), 1)
        self.assertEqual([r.is_alive() for r in self.readers], [False, False])

    def test_a_read_that_waited_for_the_lock_while_close_began_returns_nothing_quietly(self):
        m = Memory(None, now=self.clock)
        m.remember("goal", "x", 1)
        self._flush(m)
        got = []
        m._read_lock.acquire()                 # the reader passes the closed check, then waits here
        t = threading.Thread(target=lambda: got.append(m.recall("goal", "x")), name="late-reader", daemon=True)
        self.addCleanup(self._reap, threading.Event(), t, m._thread)
        self.addCleanup(m._ops.put, None)      # close() is bypassed below, so stop the writer thread by hand
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

    def _stalled(self, m: Memory) -> None:
        """Hold the writer inside its first write: it has a queued row and cannot take _io. Undone at cleanup."""
        m._io.acquire()
        self.addCleanup(self._unstall, m)
        m.remember("goal", "stalled", 1)

    def _unstall(self, m: Memory) -> None:
        if m._io.locked():
            m._io.release()
        m._ops.put(None)   # stop the writer even if the test never closed the store
        self._reap(threading.Event(), m._thread)

    def _file_rows(self) -> list:
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute("SELECT key FROM memory WHERE kind = 'goal'").fetchall()
        finally:
            conn.close()

    def test_flush_with_a_stalled_writer_returns_false_in_time_and_warns_once(self):
        m = self._open()
        self._stalled(m)
        with self.assertLogs("agent.memory", "WARNING") as logs:
            t0 = time.monotonic()
            self.assertFalse(self._bounded(self._release_io(m), m.flush, timeout=0.3))
            self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("flush timed out after 0.3s; writes still queued", logs.output[0])
        with self.assertNoLogs("agent.memory", "WARNING"):
            self.assertFalse(self._bounded(self._release_io(m), m.flush, timeout=0.1))

    def test_a_timed_out_flush_leaves_the_write_queued_and_a_later_flush_confirms(self):
        m = self._open()
        self._stalled(m)
        with self.assertLogs("agent.memory", "WARNING"):
            self.assertFalse(self._bounded(self._release_io(m), m.flush, timeout=0.1))
        m._io.release()
        self.assertTrue(m.flush())
        self.assertEqual(m.recall("goal", "stalled"), 1)

    def test_flush_is_true_on_a_healthy_store_and_false_on_a_closed_one(self):
        m = self._open()
        m.remember("goal", "x", 1)
        self.assertTrue(m.flush())
        m.close()
        self.assertFalse(m.flush())

    def test_flush_returns_false_when_the_writer_died(self):
        m = self._open()
        m._ops.put(None)
        m._thread.join(self.JOIN_BOUND_S)
        self.assertFalse(self._bounded(lambda: None, m.flush, timeout=30))

    def _check_close_timeout(self, m: Memory) -> None:
        self._stalled(m)
        with self.assertLogs("agent.memory", "WARNING") as logs:
            t0 = time.monotonic()
            self.assertFalse(self._bounded(self._release_io(m), m.close, timeout=0.3))
            self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("close timed out after 0.3s; queued writes may be lost", logs.output[0])
        # closed in effect, and a second close answers at once with the same value
        t0 = time.monotonic()
        self.assertFalse(self._bounded(self._release_io(m), m.close, timeout=30))
        self.assertLess(time.monotonic() - t0, 1)
        self.assertFalse(self._bounded(self._release_io(m), m.flush))
        self.assertIsNone(self._bounded(self._release_io(m), m.recall, "goal", "stalled"))
        with self.assertNoLogs("agent.memory", "WARNING"):   # the writer finishes its queue without failing
            m._io.release()
            m._thread.join(self.JOIN_BOUND_S)
        self.assertFalse(m._thread.is_alive())

    def test_close_with_a_stalled_writer_in_memory_returns_false_and_the_writer_finishes_cleanly(self):
        self._check_close_timeout(Memory(None, now=self.clock))

    def test_close_with_a_stalled_writer_on_a_file_returns_false_and_the_row_reaches_the_file(self):
        m = self._open()
        self._check_close_timeout(m)
        self.assertEqual(self._file_rows(), [("stalled",)])

    def test_a_timed_out_close_leaves_the_in_memory_connection_to_the_draining_writer(self):
        # The writer is parked between two writes, outside _io, so the lock is free: only close() itself decides
        # whether it closes the connection under the writer ("Cannot operate on a closed database").
        m = Memory(None, now=self.clock)
        gate = threading.Event()
        self.addCleanup(self._unstall, m)
        self.addCleanup(gate.set)
        ops = m._ops

        class Gated:
            put = staticmethod(ops.put)

            @staticmethod
            def get():
                gate.wait()
                return ops.get()

        m._ops = Gated
        for i in range(3):   # the first wakes the writer from the real queue; the rest wait behind the gate
            m.remember("action", f"k{i}", i)
        with self.assertLogs("agent.memory", "WARNING") as logs:
            self.assertFalse(self._bounded(gate.set, m.close, timeout=0.05))
            self.assertTrue(m._thread.is_alive())
            gate.set()
            m._thread.join(self.JOIN_BOUND_S)
        self.assertEqual(len(logs.records), 1, logs.output)
        self.assertIn("close timed out", logs.output[0])
        self.assertFalse(m._thread.is_alive())

    def test_close_returns_false_when_the_reader_lock_cannot_be_taken(self):
        m = self._open()
        m._read_lock.acquire()
        self.addCleanup(m._reader.close)       # close() skips it when the lock is not obtained
        self.addCleanup(self._release_read(m))
        with self.assertLogs("agent.memory", "WARNING"):
            t0 = time.monotonic()
            self.assertFalse(self._bounded(self._release_read(m), m.close, timeout=0.3))
            self.assertLess(time.monotonic() - t0, 5)

    def test_a_negative_timeout_neither_raises_nor_waits(self):
        for t in (-2, -1):
            with self.subTest(timeout=t):
                m = Memory(os.path.join(self._tmp.name, f"neg{-t}.db"), now=self.clock)
                m._read_lock.acquire()
                self.addCleanup(m._reader.close)
                self.addCleanup(self._release_read(m))
                with self.assertLogs("agent.memory", "WARNING"):
                    self.assertFalse(self._bounded(self._release_read(m), m.close, timeout=t))
                m2 = Memory(None, now=self.clock)
                self.addCleanup(m2.close)
                self._stalled(m2)
                with self.assertLogs("agent.memory", "WARNING"):
                    self.assertFalse(self._bounded(self._release_io(m2), m2.flush, timeout=t))

    def _capture_thread_errors(self) -> list:
        """Replace threading.excepthook for this test, so a dying writer prints no traceback (#741)."""
        seen: list = []
        old = threading.excepthook
        threading.excepthook = seen.append
        self.addCleanup(setattr, threading, "excepthook", old)
        return seen

    def test_close_returns_false_and_says_the_writer_is_not_running_when_it_died_before_the_sentinel(self):
        m = self._open()
        seen = self._capture_thread_errors()
        m._ops.put(1)   # not a write: the loop itself fails to unpack it, which is not a bad write
        m._thread.join(self.JOIN_BOUND_S)
        self.assertFalse(m._thread.is_alive())
        with self.assertLogs("agent.memory", "WARNING") as logs:
            t0 = time.monotonic()
            self.assertIs(self._bounded(lambda: None, m.close), False)
            self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.output[0].split(":", 2)[2],
                         "agent memory: writer is not running; queued writes were not written")
        self.assertEqual([e.exc_type for e in seen], [TypeError])   # and nothing reached stderr

    def _survives(self, kind, key, value):
        m = Memory(self.path, now=self.clock)
        self.addCleanup(m.close)
        seen = self._capture_thread_errors()
        with self.assertLogs("agent.memory", "WARNING") as logs:
            m.remember(kind, key, value, 1)
            m.remember(kind, key, value, 2)   # a second bad write: still one warning in all
            m.remember("goal", "good", "kept")
            self._flush(m)
            self.assertEqual(m.recall("goal", "good"), "kept")
        self.assertTrue(m._thread.is_alive())
        self.assertEqual(seen, [])
        return m, logs

    def test_a_lone_surrogate_in_the_kind_or_the_key_drops_that_write_with_one_warning(self):
        for where, args in (("kind", ("\ud800", "k")), ("key", ("a", "\ud800"))):
            with self.subTest(where=where):
                self.setUp()
                m, logs = self._survives(*args, 1)
                self.assertEqual(len(logs.records), 1, logs.output)
                self.assertIn("write failed", logs.output[0])
                self.assertEqual(m.recent(args[0], 5), [])
                m.remember("goal", "again", 2)   # and it goes on
                self._flush(m)
                self.assertEqual(m.recall("goal", "again"), 2)

    def test_a_lone_surrogate_in_the_value_is_stored_escaped_and_read_back(self):
        # json.dumps escapes it to ASCII, so it never reaches sqlite as a surrogate: no warning
        m = Memory(self.path, now=self.clock)
        self.addCleanup(m.close)
        with self.assertNoLogs("agent.memory", "WARNING"):
            m.remember("goal", "v", "\ud800")
            self._flush(m)
            self.assertEqual(m.recall("goal", "v"), "\ud800")
        self.assertTrue(m._thread.is_alive())

    def test_reads_with_a_lone_surrogate_argument_return_nothing_and_release_the_lock(self):
        for path in (self.path, None):
            with self.subTest(file=path is not None):
                m = Memory(path, now=self.clock)
                self.addCleanup(m.close)
                with self.assertLogs("agent.memory", "WARNING") as logs:
                    self.assertIsNone(self._bounded(self._release_io(m), m.recall, "\ud800", "k"))
                    self.assertEqual(self._bounded(self._release_io(m), m.recent, "\ud800", 3), [])
                self.assertEqual(len(logs.records), 1)
                self.assertIn("read failed", logs.output[0])
                self.assertFalse(m._read_lock.locked())
                m.remember("goal", "x", 1)   # in memory the writer shares that lock
                self._flush(m)
                self.assertEqual(m.recall("goal", "x"), 1)

    def test_a_reader_behind_a_stalled_writer_in_memory_gives_up_with_one_warning(self):
        patch = mock.patch.object(memory, "_READ_TIMEOUT_S", 0.2)
        patch.start()
        self.addCleanup(patch.stop)
        m = Memory(None, now=self.clock)
        self.addCleanup(m.close)
        self._stalled(m)
        with self.assertLogs("agent.memory", "WARNING") as logs:
            t0 = time.monotonic()
            self.assertIsNone(self._bounded(self._release_io(m), m.recall, "goal", "stalled"))
            self.assertEqual(self._bounded(self._release_io(m), m.recent, "goal", 3), [])
            self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("read timed out", logs.output[0])
        self.assertTrue(m._io.locked())   # the stall is still the test's: the reader released nothing it never took

    def test_an_infinite_timeout_neither_raises_nor_overflows(self):
        for path in (None, self.path):
            with self.subTest(file=path is not None):
                m = Memory(path, now=self.clock)
                m.remember("goal", "x", 1)
                self.assertIs(self._bounded(lambda: None, m.flush, float("inf")), True)
                self.assertIs(self._bounded(lambda: None, m.close, float("inf")), True)
                self.assertIs(self._bounded(lambda: None, m.close, float("inf")), True)

    def test_a_nan_timeout_counts_as_zero(self):
        m = Memory(None, now=self.clock)
        self.addCleanup(m.close)
        self._stalled(m)
        nan = float("nan")
        with self.assertLogs("agent.memory", "WARNING") as logs:
            t0 = time.monotonic()
            self.assertIs(self._bounded(self._release_io(m), m.flush, nan), False)
            self.assertIs(self._bounded(self._release_io(m), m.close, nan), False)
            self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("flush timed out after 0s", logs.output[0])
        self.assertIn("close timed out after 0s", logs.output[1])

    def test_a_second_close_during_the_first_returns_the_first_result_and_closes_each_connection_once(self):
        for path in (None, self.path):
            with self.subTest(file=path is not None):
                self._second_close_case(path)

    def _second_close_case(self, path):
        m = Memory(path, now=self.clock)
        closes = []

        class Spy:
            def __init__(self, conn, name):
                self.c, self.name = conn, name

            def __getattr__(self, attr):
                return getattr(self.c, attr)

            def close(self):
                closes.append(self.name)
                self.c.close()

        shared = m._reader is m._writer
        m._writer = Spy(m._writer, "writer")
        m._reader = m._writer if shared else Spy(m._reader, "reader")
        self._stalled(m)
        got = {}
        first = threading.Thread(target=lambda: got.update(first=m.close(timeout=30)), daemon=True)
        second = threading.Thread(target=lambda: got.update(second=m.close(timeout=30)), daemon=True)
        self.addCleanup(self._reap, threading.Event(), first, second)
        first.start()
        limit = time.monotonic() + self.JOIN_BOUND_S
        while not m._closed and time.monotonic() < limit:   # first is inside close(), joining the writer
            time.sleep(0.01)
        self.assertTrue(m._closed, "close() never began")
        second.start()
        second.join(0.3)
        self.assertTrue(second.is_alive(), "the second close() answered before the first finished")
        m._io.release()
        first.join(self.JOIN_BOUND_S)
        second.join(self.JOIN_BOUND_S)
        self.assertEqual(got, {"first": True, "second": True})
        self.assertEqual(sorted(closes), ["reader", "writer"] if not shared else ["writer"])


    def test_close_is_true_on_a_healthy_store_and_a_second_close_repeats_it(self):
        for path in (None, self.path):
            m = Memory(path, now=self.clock)
            m.remember("goal", "x", 1)
            with self.assertNoLogs("agent.memory", "WARNING"):
                self.assertIs(m.close(), True)
                self.assertIs(m.close(), True)
            self.assertFalse(m._thread.is_alive())

    def test_writer_thread_is_a_daemon(self):
        m = self._open()
        self.assertTrue(m._thread.daemon)


if __name__ == "__main__":
    unittest.main()
