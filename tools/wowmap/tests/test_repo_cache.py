"""#281: read-only DB sessions and the single-flight TTL cache on repo.players.online()."""
import threading
import time
import unittest
from unittest import mock

# #670: point CALIBRATION_FILE at a temp file before any wowmap import
import _calibration_env  # noqa: F401
import pagesrc  # noqa: E402,F401  (puts wowmap/ on sys.path; no new sys.path edit, see ADR 0019)
import state  # noqa: E402
from repo import cache  # noqa: E402
from repo import players as players_repo  # noqa: E402

THREADS = 10


class CountingDb:
    """Stands in for state.db(): counts queries and holds each one long enough for the
    other threads to pile up behind it."""

    def __init__(self, rows=(("Aaa", 10),), fail=False):
        self.rows, self.fail = list(rows), fail
        self.queries = 0
        self._lock = threading.Lock()

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        with self._lock:
            self.queries += 1
        time.sleep(0.2)
        if self.fail:
            raise RuntimeError("db down")

    def fetchall(self):
        return self.rows


def run_threads(fn, n=THREADS):
    """Call fn from n threads at once; return (results, errors) in arrival order."""
    start, results, errors, lock = threading.Barrier(n), [], [], threading.Lock()

    def worker():
        start.wait()
        try:
            value = fn()
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(e)
        else:
            with lock:
                results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class TtlCacheTests(unittest.TestCase):
    def test_concurrent_callers_share_one_query_then_refresh_after_ttl(self):
        clock = FakeClock()
        calls = []

        @cache.ttl_cache(1.0, clock=clock)
        def fetch(key):
            calls.append(key)
            time.sleep(0.2)
            return ["row", len(calls)]

        results, errors = run_threads(lambda: fetch("k"))
        self.assertEqual((errors, len(calls)), ([], 1))
        self.assertTrue(all(r == ["row", 1] for r in results))

        clock.now += 0.5
        self.assertEqual(fetch("k"), ["row", 1])
        self.assertEqual(len(calls), 1)

        clock.now += 0.6  # 1.1 s after the fill
        self.assertEqual(fetch("k"), ["row", 2])
        self.assertEqual(len(calls), 2)

    def test_keys_are_cached_separately(self):
        calls = []

        @cache.ttl_cache(60.0)
        def fetch(key):
            calls.append(key)
            return key

        self.assertEqual((fetch("a"), fetch("b"), fetch("a")), ("a", "b", "a"))
        self.assertEqual(calls, ["a", "b"])

    def test_exception_reaches_every_waiter_and_is_not_cached(self):
        attempts = []

        @cache.ttl_cache(60.0)
        def fetch():
            attempts.append(1)
            if len(attempts) == 1:
                time.sleep(0.2)
                raise RuntimeError("boom")
            return "ok"

        results, errors = run_threads(fetch, n=5)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(results, [])
        self.assertEqual([str(e) for e in errors], ["boom"] * 5)

        self.assertEqual(fetch(), "ok")
        self.assertEqual(len(attempts), 2)

    def test_cache_clear_forces_a_fresh_call(self):
        calls = []

        @cache.ttl_cache(60.0)
        def fetch():
            calls.append(1)
            return len(calls)

        self.assertEqual((fetch(), fetch()), (1, 1))
        fetch.cache_clear()
        self.assertEqual(fetch(), 2)


class PlayersOnlineTests(unittest.TestCase):
    def setUp(self):
        players_repo.online.cache_clear()
        self.addCleanup(players_repo.online.cache_clear)

    def test_ten_parallel_polls_make_one_query(self):
        fake = CountingDb()
        with mock.patch.object(state, "db", fake):
            results, errors = run_threads(players_repo.online)
        self.assertEqual((errors, fake.queries), ([], 1))
        self.assertTrue(all(r == [("Aaa", 10)] for r in results))

    def test_failed_query_reaches_every_waiter_and_next_poll_retries(self):
        fake = CountingDb(fail=True)
        with mock.patch.object(state, "db", fake):
            results, errors = run_threads(players_repo.online)
        self.assertEqual((results, fake.queries), ([], 1))
        self.assertEqual(len(errors), THREADS)

        fake.fail = False
        with mock.patch.object(state, "db", fake):
            self.assertEqual(players_repo.online(), [("Aaa", 10)])
        self.assertEqual(fake.queries, 2)


class ReadOnlySessionTests(unittest.TestCase):
    def test_every_connection_is_read_only_with_timeouts(self):
        with mock.patch("pymysql.connect") as connect:
            state.db()
        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["init_command"], "SET SESSION TRANSACTION READ ONLY")
        self.assertEqual((kwargs["connect_timeout"], kwargs["read_timeout"], kwargs["write_timeout"]),
                         (3, 5, 5))

    def test_mysql_settings_win_over_the_default_timeouts(self):
        with mock.patch.object(state, "MYSQL", {**state.MYSQL, "connect_timeout": 9}), \
                mock.patch("pymysql.connect") as connect:
            state.db()
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 9)
        self.assertEqual(connect.call_args.kwargs["init_command"], "SET SESSION TRANSACTION READ ONLY")


if __name__ == "__main__":
    unittest.main()
