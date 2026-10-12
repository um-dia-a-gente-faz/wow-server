"""#292: reconnect and death scenarios through the composition root, against the world-mock.

Each row builds a whole agent with agent.root.build_agent() from a real Config, runs
Agent.run() over real localhost sockets against a world-mock with a FaultPlan, and checks
three things: the lifecycle lines left in the audit directory (ADR 0015), what the mock saw
on the wire, and the backoffs the supervisor asked for (the sleep is injected, never slept)."""

import glob
import json
import logging
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import death_scenario as ds  # noqa: E402
import server  # noqa: E402
from server import FaultPlan  # noqa: E402

from agent import opcodes as op  # noqa: E402
from agent import root  # noqa: E402
from agent import session as session_mod  # noqa: E402
from agent.auth import AuthRejected  # noqa: E402
from agent.config import load_config  # noqa: E402

BACKOFF_S = 1.0      # handed to the injected sleep
SESSION_S = 1.0      # AGENT_RUN_DURATION_S: how long a healthy session runs before it ends cleanly
RECOVER_S = 20.0     # upper bound for one whole scenario
RUN_DEADLINE_S = 30.0    # Agent.run() must return this long after it started: the session ended, or stop was set
MAX_BACKOFFS = 5

CONNECTED = ("connecting", "online", "connected")
RETRY = ("backoff", "connecting", "backoff_elapsed")
ENDED = [("online", "stopping", "run_ended"), ("stopping", "stopped", "run_ended")]
DROPPED = [CONNECTED, ("online", "backoff", "disconnected"), RETRY, CONNECTED, *ENDED]

# The packets the first session reads when the mock hangs up halfway through packet 6: the
# login packets 1-5, and no packet 6 (a truncated frame never completes a read).
TRUNCATED_FIRST_SESSION_READS = [op.SMSG_AUTH_RESPONSE, op.SMSG_TIME_SYNC_REQ, op.SMSG_TUTORIAL_FLAGS,
                                 op.SMSG_CHAR_ENUM, op.SMSG_LOGIN_VERIFY_WORLD]


# DeathMock world packets: 1-5 login, 6-8 the login burst, 9 the spirit healer, 10 the corpse,
# 11 the VALUES update with health 0, 12 the reply to the first quest query. Dropping on 11 would
# race: the mock hangs up with the agent's packets unread, the RST discards what the agent has
# not read yet, and the death update can be lost. So the plan drops on 12, and the test asks
# for the quest log (which sends the query) only after it has seen the character dead.
DEATH_DROP_AFTER = 12


class Crash(Exception):
    """Stands for a bug in the think loop."""


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class LifecycleScenarioTest(unittest.TestCase):
    def setUp(self):
        self.threads_before = threading.active_count()
        self.logs = self.enterContext(self.assertLogs(level="DEBUG"))
        self.audit = self.enterContext(tempfile.TemporaryDirectory())
        self.addCleanup(self.check_no_leaks)

    def check_no_leaks(self):
        self.assertNotIn(server.DEFAULT_PASSWORD, "\n".join(self.logs.output))
        self.assertTrue(wait_for(lambda: threading.active_count() <= self.threads_before),
                        f"thread leak: {threading.enumerate()}")

    def build(self, mock_server, duration=SESSION_S):
        """A whole agent from a real Config pointed at the mock: no brain, no HTTP port, no chat relay."""
        self.mock = mock_server
        self.addCleanup(self.mock.close)
        env = {"WOW_HOST": "127.0.0.1", "WOW_AUTH_PORT": str(self.mock.auth_port),
               "WOW_ACCOUNT": server.DEFAULT_ACCOUNT, "WOW_PASSWORD": server.DEFAULT_PASSWORD,
               "WOW_CHARACTER": "Mock", "AGENT_NAME": "Scenario", "AGENT_CHANNELS": "none",
               "AGENT_AUDIT_DIR": self.audit, "AGENT_HTTP_PORT": "0", "AGENT_THINK_INTERVAL_S": "0.02",
               "LLM_BASE_URL": ""}
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        cfg = load_config()
        self.assertEqual(cfg.validate(), [])
        agent = root.build_agent(cfg, logging.getLogger("test.scenario"), duration)
        self.assertIsNone(agent.brain)
        return agent

    def run_agent(self, agent, name, stop=None):
        """Agent.run() in a daemon thread, with an injected sleep that records the backoffs and
        bounds the scenario. A run still going after RUN_DEADLINE_S fails by name, and its stop
        event is set so the thread can end. An exception raised by run() is re-raised here as
        the same object."""
        sleeps = self.sleeps = []
        self.reads = {}           # session -> opcodes of the packets it read whole, in order
        stop = stop or threading.Event()
        outcome, returned = [], []
        t0 = time.monotonic()
        real_recv = session_mod.WoWSession._recv_packet

        def recording_recv(sess):
            opcode, payload = real_recv(sess)
            self.reads.setdefault(sess, []).append(opcode)
            return opcode, payload

        def sleep(backoff):
            sleeps.append(backoff)
            if len(sleeps) > MAX_BACKOFFS or time.monotonic() - t0 > RECOVER_S:
                raise AssertionError(f"supervisor did not recover: backoffs {sleeps}")

        def target():
            try:
                agent.run(stop, sleep=sleep, jitter=lambda b: b, initial_backoff=BACKOFF_S)
                returned.append(True)
            except Exception as e:  # noqa: BLE001 - carried back to the test thread, re-raised as is
                outcome.append(e)

        with mock.patch.object(session_mod.WoWSession, "_recv_packet", recording_recv):
            runner = threading.Thread(target=target, daemon=True, name=f"run-{name}")
            runner.start()
            runner.join(RUN_DEADLINE_S)
            if runner.is_alive():
                stop.set()
                self.fail(f"{name}: Agent.run did not return within {RUN_DEADLINE_S:.0f} s")
        if outcome:
            raise outcome[0]
        self.assertTrue(returned, f"{name}: Agent.run ended without returning")
        self.assertLess(time.monotonic() - t0, RECOVER_S)

    def walk(self):
        (path,) = glob.glob(os.path.join(self.audit, "Scenario", "lifecycle", "*.jsonl"))
        with open(path) as f:
            return [(d["from"], d["to"], d["reason"]) for d in map(json.loads, f)]

    def logout_requests(self):
        return len(self.mock.packets_received(op.CMSG_LOGOUT_REQUEST))

    # ── reconnect ────────────────────────────────────────────────────────

    def test_reconnect_scenarios(self):
        # name, fault plans (one per connection), lifecycle lines, backoffs, (auth, world) connections
        table = [
            ("healthy session ends with its duration", [], [CONNECTED, *ENDED], [], (1, 1)),
            ("dropped session reconnects once", [FaultPlan(drop_after=8)], DROPPED, [BACKOFF_S], (2, 2)),
            ("truncated packet reconnects once", [FaultPlan(drop_after=6, truncate=True)], DROPPED, [BACKOFF_S], (2, 2)),
            ("transient auth refusals back off and recover",
             [FaultPlan(auth_reject=6), FaultPlan(auth_reject=6)],
             [("connecting", "backoff", "auth_rejected"), RETRY, ("connecting", "backoff", "auth_rejected"), RETRY,
              CONNECTED, *ENDED], [BACKOFF_S, 2 * BACKOFF_S], (3, 1)),
        ]
        for name, faults, lines, sleeps, connections in table:
            with self.subTest(scenario=name):
                self.audit = self.enterContext(tempfile.TemporaryDirectory())
                agent = self.build(server.WorldMock(faults=faults))
                self.run_agent(agent, name)
                self.assertEqual(self.walk(), lines)
                self.assertEqual(self.sleeps, sleeps)
                self.assertEqual((self.mock.auth_connections, self.mock.world_connections), connections)
                self.assertEqual(self.logout_requests(), 1,
                                 "the mock saw one CMSG_LOGOUT_REQUEST in total, so a dropped session sent none "
                                 "(#406; test_faults.py pins that by timing)")
                self.assertEqual(agent.lifecycle.snapshot()["state"], "stopped")
                if any(f.truncate for f in faults):
                    # A plain drop_after=6 delivers packet 6 whole. The truncated one never completes a
                    # read, so the first session's reads stop at packet 5 (the hang-up is inside packet 6).
                    first_session = next(iter(self.reads.values()))
                    self.assertEqual(first_session, TRUNCATED_FIRST_SESSION_READS)

    def test_permanent_auth_refusal_fails_after_one_attempt(self):
        agent = self.build(server.WorldMock(faults=[FaultPlan(auth_reject=3)]))
        with self.assertRaises(AuthRejected) as cm:
            self.run_agent(agent, "permanent auth refusal")
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(self.walk(), [("connecting", "failed", "auth_rejected_permanent")])
        self.assertEqual(self.sleeps, [])
        self.assertEqual((self.mock.auth_connections, self.mock.world_connections), (1, 0))

    def test_stop_during_a_session_logs_out_once_and_does_not_reconnect(self):
        agent = self.build(server.WorldMock(), duration=None)      # forever: only the stop ends it
        stop = threading.Event()
        threading.Thread(target=lambda: (wait_for(lambda: agent.lifecycle.state.value == "online"),
                                         time.sleep(0.2), stop.set()), daemon=True).start()
        self.run_agent(agent, "stop during a session", stop)
        self.assertEqual(self.walk(), [CONNECTED, ("online", "stopping", "stop"), ("stopping", "stopped", "stop")])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.mock.world_connections, 1)
        self.assertEqual(self.logout_requests(), 1)

    def test_a_crash_in_the_session_still_logs_out_and_ends_stopped_crashed(self):
        agent = self.build(server.WorldMock())
        crash = Crash("bug in the think loop")
        with mock.patch.object(root, "run_loop", side_effect=crash), self.assertRaises(Crash) as cm:
            self.run_agent(agent, "crash in the session")
        self.assertIs(cm.exception, crash)
        self.assertEqual(self.walk(), [CONNECTED, ("online", "stopping", "crashed"),
                                       ("stopping", "stopped", "crashed")])
        self.assertEqual(self.logout_requests(), 1)
        self.assertEqual(self.mock.world_connections, 1)

    # ── death ────────────────────────────────────────────────────────────

    def test_death_is_game_state_and_not_a_lifecycle_state(self):
        """Dead on login, dropped, reconnected: the lifecycle is the one of any drop, and the
        character is dead again on the second session (ADR 0015, out of scope: player state)."""
        agent = self.build(ds.DeathMock(faults=[FaultPlan(drop_after=DEATH_DROP_AFTER)]))
        dead, real_run_loop = [], root.run_loop

        def run_loop(sess, *args, **kw):
            dead.append(wait_for(lambda: sess.world_state.get_my_object() is not None
                                 and sess.world_state.get_my_object().is_dead()))
            sess.world_state.build_quest_log()     # queues CMSG_QUEST_QUERY: the mock's packet 12
            return real_run_loop(sess, *args, **kw)

        with mock.patch.object(root, "run_loop", side_effect=run_loop):
            self.run_agent(agent, "death")
        self.assertEqual(dead, [True, True])
        self.assertEqual(self.walk(), DROPPED)
        self.assertEqual(self.sleeps, [BACKOFF_S])
        self.assertEqual((self.mock.auth_connections, self.mock.world_connections), (2, 2))
        self.assertNotIn("dead", {s for line in self.walk() for s in line})


if __name__ == "__main__":
    unittest.main()
