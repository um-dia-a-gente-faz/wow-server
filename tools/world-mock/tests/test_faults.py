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
from agent import session as session_mod  # noqa: E402
from agent import transport  # noqa: E402
from agent.auth import AuthRejected  # noqa: E402
from agent.__main__ import _connect_and_login, _run_think_loop, _supervise_connection  # noqa: E402

BACKOFF_S = 1.0     # handed to an injected sleep, never really slept
SESSION_S = 1.0     # how long a healthy session's think loop runs before it ends cleanly
RECOVER_S = 20.0    # upper bound for a whole fault -> reconnect -> healthy session run
LOGOUT_AFTER_DROP_S = 1.0   # logout() after a lost connection (5 s before #406; generous for CI)
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


class LivenessConstantsTest(unittest.TestCase):
    def test_ping_interval_respects_the_server_overspeed_floor(self):
        # WorldSocket::HandlePing counts pings less than 27 s apart and kicks past
        # MaxOverspeedPings (2); SocketTimeOutTimeActive (60 s) kicks a silent client.
        self.assertGreater(session_mod.PING_INTERVAL_S, 27)
        self.assertGreater(session_mod.DEAD_SOCKET_TIMEOUT_S, session_mod.PING_INTERVAL_S)
        self.assertLess(session_mod.DEAD_SOCKET_TIMEOUT_S, 60)
        self.assertLess(session_mod.KEEPALIVE_INTERVAL_S, 60)


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
        self.logout_s = []     # how long each session's logout() took (#406)
        real_logout = session_mod.WoWSession.logout

        def timed_logout(sess):
            start = time.monotonic()
            real_logout(sess)
            self.logout_s.append(time.monotonic() - start)

        self.enterContext(mock.patch.object(session_mod.WoWSession, "logout", timed_logout))

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
                self.assertLess(self.logout_s[0], LOGOUT_AFTER_DROP_S)    # #406: nobody to say goodbye to
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

    def short_liveness(self):
        """Keepalive, ping and dead-socket deadline shrunk (checked on 0.5 s recv ticks) so a test needs seconds, not minutes."""
        for name, value in (("KEEPALIVE_INTERVAL_S", 0.1), ("PING_INTERVAL_S", 0.2), ("DEAD_SOCKET_TIMEOUT_S", 1.2)):
            self.enterContext(mock.patch.object(session_mod, name, value))

    def test_stall_longer_than_the_deadline_is_a_dead_socket_and_reconnects_once(self):
        # #404: the socket stays open but the server says nothing. Stall before packet 9, the
        # reply to the quest query the agent sends once the login burst is applied.
        self.short_liveness()
        self.enterContext(mock.patch.dict(globals(), SESSION_S=3.0))   # outlast the 1.2 s deadline
        sleeps = self.supervise(FaultPlan(stall=3.0, stall_at=9))
        self.assertEqual(sleeps, [BACKOFF_S])
        self.assertEqual((self.mock.auth_connections, self.mock.world_connections), (2, 2))
        self.assertTrue(self.sessions[0].unexpected_disconnect)
        self.assertLess(self.logout_s[0], LOGOUT_AFTER_DROP_S)
        self.assertFalse(self.sessions[-1].unexpected_disconnect)
        # Sent during the stall (the mock reads them once it wakes): keepalive and a ping
        # with the TrinityCore layout, uint32 ping id + uint32 latency (HandlePing).
        self.assertTrue(wait_for(lambda: self.mock.packets_received(op.CMSG_KEEP_ALIVE)))
        self.assertTrue(wait_for(lambda: self.mock.packets_received(op.CMSG_PING)))
        self.assertEqual(len(self.mock.packets_received(op.CMSG_PING)[0]), 8)

    def test_quiet_server_that_answers_pings_is_not_a_dead_socket(self):
        self.short_liveness()
        self.start()
        sess = self.login()
        self.assertTrue(wait_for(lambda: burst_applied(sess)))
        time.sleep(2 * session_mod.DEAD_SOCKET_TIMEOUT_S)    # silent except for pong replies
        self.assertFalse(sess.unexpected_disconnect)
        self.assertTrue(sess.recv_thread_alive())
        self.assertGreaterEqual(len(self.mock.packets_received(op.CMSG_PING)), 3)
        self.assertEqual(self.mock.world_connections, 1)

    # ── ping spacing (#426) ──────────────────────────────────────────────

    OVERSPEED_S = 0.5     # stands for TrinityCore's 27 s, scaled with the constants below

    def scaled_ping_rules(self):
        for name, value in (("LOGIN_POLL_S", 0.05), ("PING_INTERVAL_S", 0.6), ("KEEPALIVE_INTERVAL_S", 0.1),
                            ("DEAD_SOCKET_TIMEOUT_S", 5.0)):
            self.enterContext(mock.patch.object(session_mod, name, value))

    def test_silent_login_and_the_session_after_it_never_ping_over_speed(self):
        # Login silent for 12 polls (the old code pinged on each, a kick on the 4th fast one),
        # then the in-world pings: every CMSG_PING of the whole session at least OVERSPEED_S apart.
        self.scaled_ping_rules()
        self.start(FaultPlan(login_silence=0.6, overspeed_s=self.OVERSPEED_S))
        sess = self.login()
        self.assertTrue(wait_for(lambda: burst_applied(sess)))
        self.assertTrue(wait_for(lambda: len(self.mock.ping_times) >= 2, 5.0), "no periodic ping")
        gaps = [b - a for a, b in zip(self.mock.ping_times, self.mock.ping_times[1:], strict=False)]
        self.assertGreaterEqual(min(gaps), self.OVERSPEED_S)
        self.assertEqual(self.mock.world_connections, 1)
        self.assertTrue(sess.recv_thread_alive())

    def test_login_that_never_completes_still_times_out(self):
        self.scaled_ping_rules()
        self.enterContext(mock.patch.object(session_mod, "LOGIN_TIMEOUT_S", 0.3))
        self.start(FaultPlan(login_silence=30.0, overspeed_s=self.OVERSPEED_S))
        sessions = []
        real_login = session_mod.WoWSession.login_character

        def login_character(sess, guid):
            sessions.append(sess)
            return real_login(sess, guid)

        self.enterContext(mock.patch.object(session_mod.WoWSession, "login_character", login_character))
        with self.assertRaisesRegex(TimeoutError, "Login timed out"):
            self.login()
        sessions[0].logout()     # closes the socket, so the mock's handler thread ends
        self.assertEqual(self.mock.ping_times, [])

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
        with mock.patch.object(transport, "MID_PACKET_TIMEOUT_S", 0.2):
            sleeps = self.supervise(FaultPlan(bad_crypt_from=6))
        self.assertEqual(sleeps, [BACKOFF_S])            # one backoff, not a busy loop
        self.assertEqual((self.mock.auth_connections, self.mock.world_connections), (2, 2))
        self.assertTrue(self.sessions[0].unexpected_disconnect)
        self.assertLess(self.logout_s[0], LOGOUT_AFTER_DROP_S)

    def test_graceful_logout_on_a_healthy_session_still_waits_for_logout_complete(self):
        self.start()
        sess = self.login()
        self.assertTrue(wait_for(lambda: burst_applied(sess)))
        real_recv, seen = session_mod.WoWSession._recv_packet, []

        def recording_recv(self):
            seen.append(real_recv(self))
            return seen[-1]

        with mock.patch.object(session_mod.WoWSession, "_recv_packet", recording_recv):
            sess.logout()
        self.assertEqual(len(self.mock.packets_received(op.CMSG_LOGOUT_REQUEST)), 1)
        self.assertEqual(seen[-1][0], op.SMSG_LOGOUT_COMPLETE)    # waited for the reply
        self.assertIsNone(sess.sock)

    # ── auth ─────────────────────────────────────────────────────────────

    def test_slow_auth_still_logs_in(self):
        self.start(FaultPlan(slow_auth=0.3))
        t0 = time.monotonic()
        sess = self.login()
        self.assertGreaterEqual(time.monotonic() - t0, 0.3)
        self.assertTrue(wait_for(lambda: burst_applied(sess)))

    def test_auth_reject_fails_fast_naming_the_code(self):
        # TrinityCore sends 3 bytes and leaves the socket open (#405): the agent must not wait for 119.
        for code, name in ((3, "banned"), (4, "unknown account"), (9, "version invalid")):
            with self.subTest(code=code):
                self.start(FaultPlan(auth_reject=code))
                t0 = time.monotonic()
                with self.assertRaises(AuthRejected) as cm:
                    _connect_and_login(self.cfg, self.log)
                self.assertLess(time.monotonic() - t0, 1.0)
                self.assertEqual(cm.exception.code, code)
                self.assertIn(name, str(cm.exception))
                self.assertIn(f"0x{code:02X}", str(cm.exception))
                self.assertEqual(self.mock.world_connections, 0)

if __name__ == "__main__":
    unittest.main()
