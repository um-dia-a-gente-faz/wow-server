"""End-to-end fault injection (#339): the agent's real client code (auth, WoWSession,
the recv thread, the think loop and the reconnect supervisor) against a world-mock
that misbehaves, over real localhost sockets. One test per FaultPlan fault."""

import logging
import pathlib
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402
from server import FaultPlan  # noqa: E402

from agent import actions as ac  # noqa: E402
from agent import opcodes as op  # noqa: E402
from agent import transport  # noqa: E402
from agent.__main__ import _connect_and_login, _run_think_loop, _supervise_connection  # noqa: E402

BACKOFF_S = 1.0     # handed to an injected sleep, never really slept
SESSION_S = 1.0     # how long a healthy session's think loop runs before it ends cleanly
RECOVER_S = 20.0    # upper bound for a whole fault -> reconnect -> healthy session run
MAX_BACKOFFS = 5    # more backoffs than any plan here needs (the most is 3)


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def burst_applied(sess) -> bool:
    world = sess.world_state
    return world.get_object(server.QUESTGIVER_GUID) is not None and bool(world.build_quest_log())


class FaultPlanParseTest(unittest.TestCase):
    def test_env_spec(self):
        self.assertEqual(FaultPlan.parse(""), [FaultPlan()])
        self.assertEqual(
            FaultPlan.parse("drop_after=8,reset;;stall=1.5,stall_at=6;corrupt_opcode=0xA9"),
            [FaultPlan(drop_after=8, reset=True), FaultPlan(), FaultPlan(stall=1.5, stall_at=6),
             FaultPlan(corrupt_opcode=op.SMSG_UPDATE_OBJECT)])
        with self.assertRaises(KeyError):
            FaultPlan.parse("no_such_fault=1")


class FaultInjectionTest(unittest.TestCase):
    def setUp(self):
        # Once per test, not per start(): a subTest's mocks stay open until the test ends.
        self.threads_before = threading.active_count()
        # Captures (and silences) every log record; tearDown checks none leaks the password.
        self.logs = self.enterContext(self.assertLogs(level="DEBUG"))
        self.log = logging.getLogger("test")
        self.addCleanup(self.check_no_leaks)    # registered first, so it runs after logout/close

    def check_no_leaks(self):
        out = "\n".join(self.logs.output)
        self.assertNotIn(server.DEFAULT_PASSWORD, out)
        self.assertNotIn("world-mock handler crashed", out)
        self.assertTrue(wait_for(lambda: threading.active_count() <= self.threads_before),
                        f"thread leak: {threading.enumerate()}")

    def start(self, *faults):
        self.mock = server.WorldMock(faults=faults)
        self.cfg = SimpleNamespace(
            wow_host="127.0.0.1", wow_auth_port=self.mock.auth_port, account=server.DEFAULT_ACCOUNT,
            password=server.DEFAULT_PASSWORD, verbose_packets=False, dump_packets_dir="",
            character=None, char_guid=0, channels="none", think_interval=0.02)
        self.addCleanup(self.mock.close)

    def login(self):
        sess = _connect_and_login(self.cfg, self.log)
        self.addCleanup(sess.logout)
        return sess

    def supervise(self, *faults):
        """Runs the real reconnect supervisor over the real login and think loop until a
        session ends cleanly. Returns the backoffs it asked to sleep."""
        self.start(*faults)
        sleeps = []
        self.sessions = []

        def build():
            self.sessions.append(_connect_and_login(self.cfg, self.log))
            return self.sessions[-1]

        t0 = time.monotonic()

        def sleep(backoff):
            # Bounds the supervisor from the inside: without it a login that always
            # fails would loop until the CI job timeout.
            sleeps.append(backoff)
            if len(sleeps) > MAX_BACKOFFS or time.monotonic() - t0 > RECOVER_S:
                raise AssertionError(f"supervisor did not recover: backoffs {sleeps}")

        _supervise_connection(build, lambda s: _run_think_loop(s, self.cfg, SESSION_S, time.monotonic()),
                              self.log, sleep=sleep, initial_backoff=BACKOFF_S)
        self.assertLess(time.monotonic() - t0, RECOVER_S)
        self.assertTrue(burst_applied(self.sessions[-1]), "the session after the fault is not healthy")
        return sleeps

    # ── drop / reset / truncate ──────────────────────────────────────────

    def test_drop_reset_and_truncate_reconnect_once(self):
        for plan in (FaultPlan(drop_after=8), FaultPlan(drop_after=8, reset=True),
                     FaultPlan(drop_after=6, truncate=True)):
            with self.subTest(plan=plan):
                self.assertEqual(self.supervise(plan), [BACKOFF_S])
                self.assertEqual((self.mock.auth_connections, self.mock.world_connections), (2, 2))
                # A RST also discards what the client has not read yet, so it can land
                # during login (a failed attempt) instead of in the session: either way
                # exactly one reconnect.
                if not plan.reset:
                    self.assertTrue(self.sessions[0].unexpected_disconnect)
                self.assertFalse(self.sessions[-1].unexpected_disconnect)

    def test_backoff_grows_on_failed_logins_and_resets_after_a_good_one(self):
        sleeps = self.supervise(FaultPlan(auth_reject=3), FaultPlan(auth_reject=3),
                                FaultPlan(drop_after=8))
        # two rejected logins back off 1x, 2x; the drop after a good login is back at 1x
        self.assertEqual(sleeps, [BACKOFF_S, 2 * BACKOFF_S, BACKOFF_S])
        self.assertEqual((self.mock.auth_connections, self.mock.world_connections), (4, 2))

    # ── stall ────────────────────────────────────────────────────────────

    def test_stall_does_not_wedge_the_think_loop_or_kill_the_session(self):
        self.start(FaultPlan(stall=1.0, stall_at=6))     # silence right before the login burst
        sess = self.login()
        t0 = time.monotonic()
        self.assertFalse(_run_think_loop(sess, self.cfg, 0.3, t0))
        self.assertLess(time.monotonic() - t0, 0.8)
        self.assertFalse(burst_applied(sess), "the mock did not stall")
        self.assertTrue(sess.recv_thread_alive())
        self.assertFalse(sess.unexpected_disconnect)
        self.assertTrue(wait_for(lambda: burst_applied(sess)), "no recovery after the stall")
        # Not covered: the 15 s keepalive, and a stall that never ends (nothing detects it, #404).

    # ── split writes ─────────────────────────────────────────────────────

    def test_split_write_is_reassembled(self):
        self.start(FaultPlan(split_write=3))             # smaller than a 4-byte header
        sess = self.login()
        self.assertTrue(wait_for(lambda: burst_applied(sess)))
        self.assertTrue(wait_for(lambda: self.mock.packets_received(op.CMSG_QUEST_QUERY)))
        self.assertTrue(wait_for(lambda: sess.world_state.build_quest_log()[0].get("title")))
        self.assertEqual(sess.dropped_packets, 0)
        self.assertFalse(sess.unexpected_disconnect)

    # ── corrupt payload ──────────────────────────────────────────────────

    def test_corrupt_payload_is_dropped_and_counted_not_fatal(self):
        self.start(FaultPlan(corrupt_opcode=op.SMSG_QUEST_QUERY_RESPONSE))
        sess = self.login()
        self.assertTrue(wait_for(lambda: burst_applied(sess)))    # reading the quest log queues the query
        self.assertTrue(wait_for(lambda: sess.dropped_packets >= 1), "handler error not counted")
        self.assertTrue(sess.recv_thread_alive())
        self.assertFalse(sess.unexpected_disconnect)
        # the stream is still in sync: the next exchange works
        res = ac.REGISTRY["complete_quest"].run(sess, sess.world_state, npc_guid=server.QUESTGIVER_GUID,
                                                quest_id=server.QUEST_ID)
        self.assertTrue(res.ok, res.error)
        self.assertTrue(wait_for(
            lambda: (sess.world_state.get_ui_state() or {}).get("kind") == "quest_offer_reward"))

    # ── bad header crypt ─────────────────────────────────────────────────

    def test_bad_header_crypt_fails_cleanly_and_reconnects_once(self):
        # A desynced RC4 stream turns headers into noise; the agent ends up waiting for a
        # payload that never comes and gives up after MID_PACKET_TIMEOUT_S (30 s live).
        # Most of this test's ~7 s is logout() waiting 5 s on the dead stream (#406).
        with mock.patch.object(transport, "MID_PACKET_TIMEOUT_S", 0.2):
            sleeps = self.supervise(FaultPlan(bad_crypt_from=6))
        self.assertEqual(sleeps, [BACKOFF_S])            # one backoff, not a busy loop
        self.assertEqual((self.mock.auth_connections, self.mock.world_connections), (2, 2))
        self.assertTrue(self.sessions[0].unexpected_disconnect)

    # ── auth ─────────────────────────────────────────────────────────────

    def test_slow_auth_still_logs_in(self):
        self.start(FaultPlan(slow_auth=0.3))
        t0 = time.monotonic()
        sess = self.login()
        self.assertGreaterEqual(time.monotonic() - t0, 0.3)
        self.assertTrue(wait_for(lambda: burst_applied(sess)))

    def test_auth_reject_raises_without_reaching_the_world_server(self):
        self.start(FaultPlan(auth_reject=3))
        # ConnectionError today: the agent waits for a full 119-byte challenge (#405).
        with self.assertRaises((RuntimeError, ConnectionError)):
            _connect_and_login(self.cfg, self.log)
        self.assertEqual(self.mock.world_connections, 0)


if __name__ == "__main__":
    unittest.main()
