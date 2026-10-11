"""#401: SIGTERM/SIGINT set a stop event; the supervisor, the think loop and logout()
unwind through the existing finally: session.logout() path instead of dying mid-session."""

import os
import signal
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from agent import __main__ as m
from agent import session as session_mod
from agent.tests.test_reconnect import _NullLog


class FakeSession:
    def __init__(self):
        self.logouts = 0
        self.unexpected_disconnect = False
        self.world_state = SimpleNamespace(get_objects=dict)

    def logout(self):
        self.logouts += 1


class SupervisorStopTest(unittest.TestCase):
    def test_stop_mid_session_logs_out_and_does_not_reconnect(self):
        stop, sessions = threading.Event(), []

        def build():
            sessions.append(FakeSession())
            return sessions[-1]

        def run(sess):
            stop.set()
            return True  # even a "dropped" verdict must not trigger a reconnect once stopped

        m._supervise_connection(build, run, log=_NullLog(), stop=stop)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].logouts, 1)

    def test_stop_during_backoff_wakes_the_wait_and_ends(self):
        stop, builds = threading.Event(), []

        def build():
            builds.append(1)
            raise RuntimeError("refused")

        threading.Timer(0.1, stop.set).start()
        t0 = time.monotonic()
        m._supervise_connection(build, lambda s: False, log=_NullLog(), stop=stop,
                                initial_backoff=30.0)
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(len(builds), 1)


class ThinkLoopStopTest(unittest.TestCase):
    def test_stop_interrupts_the_deadline_sleep_and_is_a_clean_stop(self):
        stop = threading.Event()
        cfg = SimpleNamespace(think_interval=30.0)
        threading.Timer(0.1, stop.set).start()
        t0 = time.monotonic()
        result = m._run_think_loop(FakeSession(), cfg, None, time.monotonic(), stop=stop)
        self.assertIs(result, False)  # False = clean stop, supervisor must not reconnect
        self.assertLess(time.monotonic() - t0, 5.0)


class SignalHandlerTest(unittest.TestCase):
    def test_sigterm_and_sigint_set_the_event(self):
        old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in old.items()])
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(sig=sig):
                stop = threading.Event()
                m._install_stop_signals(stop)
                os.kill(os.getpid(), sig)
                self.assertTrue(stop.wait(2.0))


class LogoutBoundTest(unittest.TestCase):
    def test_logout_gives_up_waiting_for_logout_complete(self):
        sess = session_mod.WoWSession.__new__(session_mod.WoWSession)
        sess.sock = mock.Mock()
        sess._recv_thread = None
        sess._in_world = True
        sess.unexpected_disconnect = False
        sess._send_packet = lambda *a, **k: None
        sess._recv_packet = lambda: (0x1, b"")  # chatty server, never SMSG_LOGOUT_COMPLETE
        with mock.patch.object(session_mod, "LOGOUT_WAIT_S", 0.2):
            t0 = time.monotonic()
            sess.logout()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertIsNone(sess.sock)


if __name__ == "__main__":
    unittest.main()
