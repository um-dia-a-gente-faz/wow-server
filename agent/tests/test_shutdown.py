"""#401: SIGTERM/SIGINT set a stop event; the supervisor, the think loop and logout()
unwind through the existing finally: session.logout() path instead of dying mid-session."""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from agent import __main__ as m
from agent import runloop, supervisor
from agent import session as session_mod
from agent.auth import AuthRejected
from agent.tests.builders import make_session
from agent.tests.restore import join_new_threads
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

        supervisor.supervise_connection(build, run, log=_NullLog(), stop=stop)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].logouts, 1)

    def test_stop_during_backoff_wakes_the_wait_and_ends(self):
        stop, builds = threading.Event(), []

        def build():
            builds.append(1)
            raise RuntimeError("refused")

        threading.Timer(0.1, stop.set).start()
        t0 = time.monotonic()
        supervisor.supervise_connection(build, lambda s: False, log=_NullLog(), stop=stop,
                                initial_backoff=30.0)
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(len(builds), 1)


class MainWiresStopTest(unittest.TestCase):
    """main() must hand the signal event to Agent.run(): without it a SIGTERM during a
    reconnect backoff (up to 300 s) is ignored and docker kills the container."""

    def run_main(self, run, argv=("wow-agent",)):
        self.stop, self.calls = threading.Event(), []
        cfg = mock.MagicMock(run_duration=0.0)
        cfg.validate.return_value = []
        agent = mock.Mock()
        agent.run.side_effect = run

        def build(cfg, log, duration, perception_dump=False):
            self.calls.append(("build", duration, perception_dump))
            return agent

        def install():
            self.calls.append(("signals",))
            return self.stop

        with mock.patch.object(sys, "argv", list(argv)), \
                mock.patch.object(m, "load_config", return_value=cfg), \
                mock.patch.object(m, "_setup_logging"), \
                mock.patch.object(m, "build_agent", side_effect=build), \
                mock.patch.object(m, "_install_stop_signals", side_effect=install):
            m.main()
        return agent

    def test_main_passes_the_signal_event_to_the_agent_after_the_wiring(self):
        agent = self.run_main(lambda stop: self.calls.append(("run", stop)))
        self.assertEqual(self.calls, [("build", None, False), ("signals",), ("run", self.stop)])
        agent.run.assert_called_once_with(self.stop)

    def test_duration_and_perception_dump_reach_build_agent(self):
        self.run_main(lambda stop: None, argv=("wow-agent", "--duration", "7", "--perception-dump"))
        self.assertEqual(self.calls[0], ("build", 7.0, True))

    def test_a_permanent_refusal_exits_one_without_a_traceback(self):
        def refused(stop):
            raise AuthRejected("logon proof", 0x03)

        with self.assertRaises(SystemExit) as cm:
            self.run_main(refused)
        self.assertEqual(cm.exception.code, 1)

    def test_dry_run_and_list_chars_never_build_an_agent(self):
        for flag, name in (("--dry-run", "dry_run"), ("--list-chars", "list_characters")):
            for ok in (True, False):
                with self.subTest(flag=flag, ok=ok), mock.patch.object(m, name, return_value=ok) as mode:
                    if ok:
                        self.run_main(None, argv=("wow-agent", flag))
                    else:
                        with self.assertRaises(SystemExit) as cm:
                            self.run_main(None, argv=("wow-agent", flag))
                        self.assertEqual(cm.exception.code, 1)
                    mode.assert_called_once()
                    self.assertEqual(self.calls, [])


class ThinkLoopStopTest(unittest.TestCase):
    def test_stop_interrupts_the_deadline_sleep_and_is_a_clean_stop(self):
        stop = threading.Event()
        cfg = SimpleNamespace(think_interval=30.0)
        threading.Timer(0.1, stop.set).start()
        t0 = time.monotonic()
        result = runloop.run_think_loop(FakeSession(), cfg, None, time.monotonic(), stop=stop)
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


_SECOND_SIGNAL_CHILD = """
import sys, threading, time
from agent import __main__ as m
stop = m._install_stop_signals()
print("ready", flush=True)
while True:       # a hung shutdown: only the second signal can end this
    time.sleep(0.05)
"""


class SecondSignalTest(unittest.TestCase):
    def test_second_signal_kills_the_process(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(sig=sig):
                child = subprocess.Popen([sys.executable, "-c", _SECOND_SIGNAL_CHILD],
                                         stdout=subprocess.PIPE, text=True,
                                         cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
                self.addCleanup(child.kill)
                self.addCleanup(child.stdout.close)
                self.assertEqual(child.stdout.readline().strip(), "ready")
                child.send_signal(sig)
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.wait(timeout=0.5)  # the first signal only sets the event
                child.send_signal(sig)
                self.assertEqual(child.wait(timeout=5.0), -sig)  # killed by the second, default action

    def test_handler_resets_to_default_after_first_signal(self):
        old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in old.items()])
        stop = threading.Event()
        m._install_stop_signals(stop)
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(stop.wait(2.0))
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_DFL)


class LogoutBoundTest(unittest.TestCase):
    def setUp(self):
        join_new_threads(self)  # logout's cut Timer and the test's own logout thread end just after the test

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

    def _logout_in_thread(self, sess, within):
        t = threading.Thread(target=sess.logout, daemon=True)
        t.start()
        t.join(within)
        return not t.is_alive()

    def test_logout_wait_is_bounded_by_the_socket_timeout_alone(self):
        # The cut timer is a second line of defence; with it out of the way a silent server
        # must still end the wait through settimeout(LOGOUT_WAIT_S).
        sess = make_session()
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        sess.sock = ours
        sess._in_world = True
        sess._recv_thread = None
        with mock.patch.object(session_mod, "LOGOUT_WAIT_S", 0.3), \
                mock.patch.object(session_mod.threading, "Timer"):
            done = self._logout_in_thread(sess, within=3.0)
            if not done:
                theirs.close()  # unblock the stuck thread before failing
        self.assertTrue(done, "logout blocked on a silent server: no socket timeout")

    def test_logout_join_of_a_stuck_recv_thread_is_bounded(self):
        sess = make_session()
        release = threading.Event()
        self.addCleanup(release.set)
        sess._recv_thread = threading.Thread(target=release.wait, daemon=True)
        sess._recv_thread.start()
        with mock.patch.object(session_mod, "LOGOUT_WAIT_S", 0.2):
            done = self._logout_in_thread(sess, within=3.0)
            release.set()
        self.assertTrue(done, "logout waited for a recv thread that never exits")
        self.assertTrue(sess.unexpected_disconnect)
        self.assertIsNone(sess.sock)


if __name__ == "__main__":
    unittest.main()
