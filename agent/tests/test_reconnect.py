"""Unit tests for the reconnect supervisor in agent.__main__ (UM-43).
build_session()/run_session() are both fakes; time.sleep is replaced with a
fake that just records durations instead of actually waiting, so backoff
timing (5s -> 5min cap) is asserted without the test taking minutes."""

import functools
import random
import threading
import time
import unittest

from agent import __main__ as main_mod
from agent.auth import AuthRejected


class FakeSleep:
    """Records requested sleep durations instead of blocking."""

    def __init__(self, limit=None):
        self.calls = []
        self.limit = limit

    def __call__(self, seconds):
        self.calls.append(seconds)
        if self.limit is not None and len(self.calls) > self.limit:
            raise AssertionError(f"supervisor kept retrying: sleeps {self.calls}")


class SupervisorConnectFailureTest(unittest.TestCase):
    def test_retries_with_exponential_backoff_and_recovers(self):
        sleep = FakeSleep()
        attempts = []

        def build_session():
            attempts.append(1)
            if len(attempts) < 4:
                raise RuntimeError("connection refused")
            return object()

        def run_session(session):
            return False  # clean stop once we finally connect

        main_mod._supervise_connection(build_session, run_session, log=_NullLog(),
                                        sleep=sleep, jitter=lambda b: b,
                                        initial_backoff=5.0, max_backoff=300.0)

        self.assertEqual(len(attempts), 4)
        # 3 failed attempts before the 4th succeeds: 5s, 10s, 20s
        self.assertEqual(sleep.calls, [5.0, 10.0, 20.0])

    def test_backoff_caps_at_max(self):
        sleep = FakeSleep()
        attempts = []

        def build_session():
            attempts.append(1)
            if len(attempts) < 6:
                raise RuntimeError("connection refused")
            return object()

        main_mod._supervise_connection(build_session, lambda s: False, log=_NullLog(),
                                        sleep=sleep, jitter=lambda b: b,
                                        initial_backoff=5.0, max_backoff=15.0)

        # 5, 10, 15(capped), 15, 15
        self.assertEqual(sleep.calls, [5.0, 10.0, 15.0, 15.0, 15.0])


class SupervisorAuthRejectTest(unittest.TestCase):
    """#430: a permanent AuthResult stops the supervisor after one line; a transient one backs off."""

    def run_with_rejects(self, code, rejects):
        """Login is refused with `code` for the first `rejects` attempts, then succeeds."""
        attempts, sleep, log = [], FakeSleep(), _RecordingLog()

        def build_session():
            attempts.append(1)
            if len(attempts) <= rejects:
                raise AuthRejected("logon proof", code)
            return _FakeSession()

        main_mod._supervise_connection(build_session, lambda s: False, log=log, sleep=sleep,
                                       jitter=lambda b: b, initial_backoff=5.0, max_backoff=300.0)
        return attempts, sleep, log

    def test_permanent_code_raises_after_one_error_line_and_no_traceback(self):
        for code in (0x03, 0x04, 0x05, 0x09, 0x0A, 0x0D):
            with self.subTest(code=code):
                attempts, log = [], _RecordingLog()
                # a bounded sleep: a supervisor that retries a permanent reject fails here, not by hanging
                with self.assertRaises(AuthRejected) as cm:
                    main_mod._supervise_connection(self.always_refused(code, attempts),
                                                   lambda s: False, log=log,
                                                   sleep=FakeSleep(limit=3), initial_backoff=5.0,
                                                   max_backoff=300.0)
                self.assertEqual(cm.exception.code, code)
                self.assertEqual(len(attempts), 1)
                self.assertEqual([level for level, _ in log.lines], ["error"])
                self.assertIn(f"0x{code:02X}", log.lines[0][1])

    @staticmethod
    def always_refused(code, attempts):
        def build_session():
            attempts.append(1)
            raise AuthRejected("logon proof", code)
        return build_session

    def test_transient_code_backs_off_with_one_warning_and_no_traceback(self):
        for code in (0x06, 0x08, 0x0C, 0x10, 0x19):
            with self.subTest(code=code):
                attempts, sleep, log = self.run_with_rejects(code, rejects=1)
                self.assertEqual(len(attempts), 2)
                self.assertEqual(sleep.calls, [5.0])
                self.assertEqual([level for level, _ in log.lines], ["warning"])


class _FakeSession:
    def logout(self):
        pass


class _RecordingLog:
    """Records (level, rendered message) for the auth-reject tests; no traceback is kept."""

    def __init__(self):
        self.lines = []

    def _record(self, level, fmt, *args):
        self.lines.append((level, fmt % args))

    def info(self, *a):
        pass

    def warning(self, *a):
        self._record("warning", *a)

    def error(self, *a):
        self._record("error", *a)

    def exception(self, *a):
        self._record("exception", *a)


class SupervisorDisconnectTest(unittest.TestCase):
    def test_retries_after_unexpected_disconnect_then_stops_cleanly(self):
        sleep = FakeSleep()
        sessions_built = []
        logged_out = []

        class FakeSession:
            def __init__(self, n):
                self.n = n

            def logout(self):
                logged_out.append(self.n)

        def build_session():
            n = len(sessions_built)
            sess = FakeSession(n)
            sessions_built.append(sess)
            return sess

        run_results = [True, True, False]  # two unexpected disconnects, then a clean stop

        def run_session(session):
            return run_results[session.n]

        main_mod._supervise_connection(build_session, run_session, log=_NullLog(),
                                        sleep=sleep, jitter=lambda b: b,
                                        initial_backoff=5.0, max_backoff=300.0)

        self.assertEqual(len(sessions_built), 3)
        self.assertEqual(logged_out, [0, 1, 2])  # every attempt is logged out, even after disconnect
        # Each disconnect follows a *successful* connect, so backoff resets
        # to the initial value both times rather than compounding.
        self.assertEqual(sleep.calls, [5.0, 5.0])

    def test_backoff_resets_after_a_successful_reconnect(self):
        sleep = FakeSleep()
        # attempt 1: connect fails twice (5s, 10s), then succeeds and disconnects (backoff resets to 5s)
        # attempt 2 (after reconnect): disconnects again -> should back off from 5s again, not 20s
        build_calls = []

        def build_session():
            build_calls.append(1)
            if len(build_calls) <= 2:
                raise RuntimeError("still down")
            return object()

        run_calls = []

        def run_session(session):
            run_calls.append(1)
            return len(run_calls) == 1  # first successful run disconnects, second stops cleanly

        main_mod._supervise_connection(build_session, run_session, log=_NullLog(),
                                        sleep=sleep, jitter=lambda b: b,
                                        initial_backoff=5.0, max_backoff=300.0)

        # 5s, 10s (connect retries) then 5s again (post-disconnect retry — reset, not 20s)
        self.assertEqual(sleep.calls, [5.0, 10.0, 5.0])

    def test_run_session_exception_during_logout_does_not_crash_supervisor(self):
        sleep = FakeSleep()

        class FakeSession:
            def logout(self):
                raise RuntimeError("socket already closed")

        calls = []

        def run_session(session):
            calls.append(1)
            return len(calls) == 1

        main_mod._supervise_connection(lambda: FakeSession(), run_session, log=_NullLog(),
                                        sleep=sleep, initial_backoff=1.0, max_backoff=10.0)
        self.assertEqual(len(calls), 2)


class SupervisorJitterTest(unittest.TestCase):
    """#402: each wait is drawn from [backoff / 2, backoff]; the exponential backoff itself is unchanged."""

    ATTEMPTS = 12
    EXPECTED = [min(5.0 * 2 ** i, 300.0) for i in range(ATTEMPTS)]  # 5, 10, 20, ... 300 (capped)

    def retry_waits(self, **kwargs):
        """The waits of a supervisor whose first ATTEMPTS connects fail, then succeed and stop."""
        sleep, builds = FakeSleep(), []

        def build_session():
            builds.append(1)
            if len(builds) <= self.ATTEMPTS:
                raise RuntimeError("connection refused")
            return object()

        main_mod._supervise_connection(build_session, lambda s: False, log=_NullLog(), sleep=sleep,
                                       initial_backoff=5.0, max_backoff=300.0, **kwargs)
        return sleep.calls

    @staticmethod
    def seeded(seed):
        return {"jitter": functools.partial(main_mod._equal_jitter, rng=random.Random(seed))}

    def test_seeded_supervisors_spread_their_first_retry(self):
        firsts = [self.retry_waits(**self.seeded(seed))[0] for seed in range(25)]
        self.assertGreater(len(set(firsts)), 1)
        self.assertGreaterEqual(len(set(firsts)), 20)

    def test_every_wait_is_within_its_backoff_bounds_and_the_cap(self):
        for seed in range(25):
            waits = self.retry_waits(**self.seeded(seed))
            self.assertEqual(len(waits), self.ATTEMPTS)
            for wait, backoff in zip(waits, self.EXPECTED, strict=True):
                with self.subTest(seed=seed, backoff=backoff):
                    self.assertGreaterEqual(wait, backoff / 2)
                    self.assertLessEqual(wait, backoff)
                    self.assertLessEqual(wait, 300.0)

    def test_default_supervisor_jitters_within_bounds(self):
        waits = self.retry_waits()  # no jitter argument: the default is jitter on
        for wait, backoff in zip(waits, self.EXPECTED, strict=True):
            self.assertGreaterEqual(wait, backoff / 2)
            self.assertLessEqual(wait, backoff)
        self.assertNotEqual(waits, self.EXPECTED)

    def test_log_line_prints_the_jittered_delay_actually_waited(self):
        log, sleep, builds = _RecordingLog(), FakeSleep(limit=2), []

        def build_session():
            builds.append(1)
            if len(builds) == 1:
                raise AuthRejected("logon proof", 0x06)  # transient: a warning line with the wait
            return object()

        main_mod._supervise_connection(build_session, lambda s: False, log=log, sleep=sleep,
                                       jitter=lambda b: 3.0, initial_backoff=5.0, max_backoff=300.0)
        self.assertEqual(sleep.calls, [3.0])
        self.assertIn("retrying in 3s", log.lines[0][1])

    def test_stop_during_a_jittered_wait_returns_without_reconnecting(self):
        stop, builds = threading.Event(), []

        def build_session():
            builds.append(1)
            raise RuntimeError("refused")

        threading.Timer(0.1, stop.set).start()
        t0 = time.monotonic()
        main_mod._supervise_connection(build_session, lambda s: False, log=_NullLog(), stop=stop,
                                       initial_backoff=30.0)  # jittered wait: 15 s to 30 s
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(len(builds), 1)


class SupervisorReconnectCountTest(unittest.TestCase):
    """#402: each reconnect wait is counted once under its reason; a permanent reject is not a reconnect."""

    def setUp(self):
        from agent import metrics
        self.m = metrics
        metrics.RECONNECTS.update(dict.fromkeys(metrics.RECONNECT_REASONS, 0))
        self.addCleanup(metrics.RECONNECTS.update, dict.fromkeys(metrics.RECONNECT_REASONS, 0))

    def test_each_reason_is_counted_once_for_its_wait(self):
        builds, runs = [], []

        def build_session():
            builds.append(1)
            if len(builds) == 1:
                raise AuthRejected("logon proof", 0x06)  # transient: auth_rejected
            if len(builds) == 2:
                raise RuntimeError("connection refused")  # connect_failed
            return object()

        def run_session(session):
            runs.append(1)
            return len(runs) == 1  # first session drops (disconnected), second stops cleanly

        main_mod._supervise_connection(build_session, run_session, log=_NullLog(), sleep=FakeSleep(),
                                       initial_backoff=5.0, max_backoff=300.0)
        self.assertEqual(self.m.RECONNECTS, {"connect_failed": 1, "auth_rejected": 1, "disconnected": 1})

    def test_a_permanent_reject_is_not_counted(self):
        def build_session():
            raise AuthRejected("logon proof", 0x03)  # permanent: raised, never waits

        with self.assertRaises(AuthRejected):
            main_mod._supervise_connection(build_session, lambda s: False, log=_RecordingLog(),
                                           sleep=FakeSleep(limit=0))
        self.assertEqual(sum(self.m.RECONNECTS.values()), 0)


class _NullLog:
    """A logger stand-in that swallows everything — these tests only care
    about control flow/timing, not log output."""

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass

    def log(self, *a, **k):
        pass

    def critical(self, *a, **k):
        pass


if __name__ == "__main__":
    unittest.main()
