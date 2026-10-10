"""Unit tests for the reconnect supervisor in agent.__main__ (UM-43).
build_session()/run_session() are both fakes; time.sleep is replaced with a
fake that just records durations instead of actually waiting, so backoff
timing (5s -> 5min cap) is asserted without the test taking minutes."""

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
                                        sleep=sleep,
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
                                        sleep=sleep,
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
                                       initial_backoff=5.0, max_backoff=300.0)
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
                                        sleep=sleep, initial_backoff=5.0, max_backoff=300.0)

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
                                        sleep=sleep, initial_backoff=5.0, max_backoff=300.0)

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
