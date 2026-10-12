"""#292: the composition root (agent/root.py): build_agent() wires one agent without
connecting, Agent.run() supervises it and closes what build_agent() started, and the
dry run stays outside the supervisor and the lifecycle."""

import json
import logging
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from agent import root
from agent.audit import AuditLogger
from agent.auth import AuthRejected
from agent.lifecycle import Lifecycle
from agent.tests.builders import make_session
from agent.tests.test_reconnect import _RecordingLog

LOG = logging.getLogger("test.root")


class _Log(_RecordingLog):
    """_RecordingLog plus critical(), which the dry run and --list-chars use for a failed login."""

    def critical(self, *a):
        self._record("critical", *a)


def _agent(**overrides):
    fields = dict(cfg=SimpleNamespace(think_interval=0.01), log=_Log(), chat_relay=mock.Mock(),
                  brain=None, audit_logger=mock.Mock(), observer=None, http_server=mock.Mock(),
                  lifecycle=Lifecycle(), duration=1.5, perception_dump=True)
    fields.update(overrides)
    return root.Agent(**fields)


class AgentRunTest(unittest.TestCase):
    def test_run_supervises_with_its_own_parts_and_then_closes(self):
        agent, stop, seen = _agent(), threading.Event(), {}

        def supervise(**kw):
            seen.update(kw)
            self.assertFalse(agent.chat_relay.stop.called, "closed before the supervisor returned")

        with mock.patch.object(root, "supervise_connection", side_effect=supervise):
            agent.run(stop, sleep="S", jitter="J")
        self.assertIs(seen["stop"], stop)
        self.assertIs(seen["lifecycle"], agent.lifecycle)
        self.assertIs(seen["log"], agent.log)
        self.assertEqual((seen["sleep"], seen["jitter"]), ("S", "J"))
        self.assertEqual(seen["build_session"], agent.build_session)
        agent.chat_relay.stop.assert_called_once_with()
        agent.http_server.shutdown.assert_called_once_with()

    def test_run_closes_and_lets_the_exception_through(self):
        for crash in (AuthRejected("logon proof", 0x03), ValueError("think loop")):
            with self.subTest(crash=type(crash).__name__):
                agent = _agent(http_server=None)   # no observability server: close() must cope
                with mock.patch.object(root, "supervise_connection", side_effect=crash), \
                        self.assertRaises(type(crash)) as cm:
                    agent.run()
                self.assertIs(cm.exception, crash)
                agent.chat_relay.stop.assert_called_once_with()

    def test_sessions_are_built_and_run_through_login_and_run_loop(self):
        agent, stop = _agent(), threading.Event()
        with mock.patch.object(root, "connect_and_login", return_value="SESSION") as login, \
                mock.patch.object(root, "run_loop", return_value=True) as run_loop:
            self.assertEqual(agent.build_session(), "SESSION")
            self.assertIs(agent.run_session("SESSION", stop), True)
        login.assert_called_once_with(agent.cfg, agent.log, agent.chat_relay)
        run_loop.assert_called_once_with("SESSION", agent.cfg, 1.5, perception_dump=True, brain=None,
                                         audit_logger=agent.audit_logger, observer=None, stop=stop)

    def test_run_end_to_end_writes_the_lifecycle_and_stops_on_the_event(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        audit = AuditLogger("Rooted", base_dir=tmp.name)
        observer = root.AgentObserver("Rooted")
        agent = _agent(audit_logger=audit, observer=observer, lifecycle=root.build_lifecycle(audit, observer))
        stop, session = threading.Event(), mock.Mock()

        def run_loop(sess, cfg, duration, **kw):
            self.assertIs(kw["stop"], stop)
            stop.set()             # SIGTERM while the session runs
            return False

        with mock.patch.object(root, "connect_and_login", return_value=session), \
                mock.patch.object(root, "run_loop", side_effect=run_loop):
            agent.run(stop)
        session.logout.assert_called_once_with()
        (name,) = os.listdir(os.path.join(tmp.name, "Rooted", "lifecycle"))
        with open(os.path.join(tmp.name, "Rooted", "lifecycle", name)) as f:
            walk = [(d["from"], d["to"], d["reason"]) for d in map(json.loads, f)]
        self.assertEqual(walk, [("connecting", "online", "connected"), ("online", "stopping", "stop"),
                                ("stopping", "stopped", "stop")])
        self.assertEqual(observer.state()["lifecycle"]["state"], "stopped")


class BuildAgentTest(unittest.TestCase):
    def cfg(self, tmp):
        return SimpleNamespace(agent_name="Built", audit_dir=tmp, audit_retention_days=3, agent_brain="llm",
                               jev_enabled=False, http_port=0, chat_relay_url="", chat_relay_token="",
                               chat_relay_timeout=1.0)

    def test_wires_every_part_in_the_order_main_used_and_does_not_connect(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        order, brain, observer, server = [], mock.Mock(), root.AgentObserver("Built"), mock.Mock()
        relay = mock.Mock()

        def step(name, value):
            return lambda *a, **kw: (order.append(name), value)[1]

        with mock.patch.object(root, "build_chat_relay", side_effect=step("chat_relay", relay)), \
                mock.patch.object(root.Brain, "from_config", side_effect=step("brain", brain)), \
                mock.patch.object(root, "warn_jev_unselected", side_effect=step("jev_warning", None)), \
                mock.patch.object(root, "start_observer", side_effect=step("observer", (observer, server))), \
                mock.patch.object(root, "connect_and_login", side_effect=AssertionError("build_agent connected")):
            agent = root.build_agent(self.cfg(tmp.name), LOG, 12.0, perception_dump=True)
        self.assertEqual(order, ["chat_relay", "brain", "jev_warning", "observer"])
        self.assertEqual((agent.chat_relay, agent.brain, agent.observer, agent.http_server),
                         (relay, brain, observer, server))
        self.assertEqual((agent.duration, agent.perception_dump), (12.0, True))
        self.assertEqual(agent.audit_logger.agent_dir, os.path.join(tmp.name, "Built"))
        self.assertEqual(agent.audit_logger.retention_days, 3)
        brain.restore_jev_spend.assert_called_once_with(tmp.name, "Built")
        self.assertIsInstance(agent.lifecycle, Lifecycle)
        self.assertIs(observer.lifecycle, agent.lifecycle)
        self.assertEqual(agent.lifecycle.history(), ())

    def test_without_a_brain_or_an_http_port_the_agent_is_still_built(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(root.Brain, "from_config", return_value=None), self.assertLogs(LOG, level="WARNING"):
            agent = root.build_agent(self.cfg(tmp.name), LOG)
        self.addCleanup(agent.close)
        self.assertIsNone(agent.brain)
        self.assertEqual((agent.observer, agent.http_server), (None, None))
        self.assertEqual((agent.duration, agent.perception_dump), (None, False))
        self.assertFalse(agent.chat_relay.enabled)


class DryRunTest(unittest.TestCase):
    """--dry-run keeps its own connect, sleep and logout: no supervisor and no lifecycle (ADR 0015, decision 5)."""

    def run_dry(self, login):
        relay, slept = mock.Mock(), []
        with mock.patch.object(root, "build_chat_relay", return_value=relay), \
                mock.patch.object(root, "connect_and_login", side_effect=login), \
                mock.patch.object(root.time, "sleep", side_effect=slept.append), \
                mock.patch.object(root, "supervise_connection", side_effect=AssertionError("supervised")), \
                mock.patch.object(root, "Lifecycle", side_effect=AssertionError("a lifecycle was built")), \
                mock.patch.object(root, "AuditLogger", side_effect=AssertionError("an audit log was built")):
            result = root.dry_run(SimpleNamespace(), _Log(), 30.0)
        return result, relay, slept

    def test_logs_in_sleeps_logs_out_and_stops_the_relay(self):
        session = mock.Mock()
        session.world_state.get_objects.return_value = {}
        session.dropped_packets = 0
        result, relay, slept = self.run_dry(lambda cfg, log, chat_relay: session)
        self.assertIs(result, True)
        self.assertEqual(slept, [30.0])
        session.logout.assert_called_once_with()
        relay.stop.assert_called_once_with()

    def test_a_failed_login_returns_false_without_sleeping(self):
        def refused(cfg, log, chat_relay):
            raise AuthRejected("logon proof", 0x05)

        result, relay, slept = self.run_dry(refused)
        self.assertIs(result, False)
        self.assertEqual(slept, [])
        relay.stop.assert_not_called()   # as before #292: the process exits right after


class ListCharactersTest(unittest.TestCase):
    def test_logs_out_and_reports_success_or_failure(self):
        session = mock.Mock()
        with mock.patch.object(root, "authenticate_and_login", return_value=(session, [])):
            self.assertIs(root.list_characters(SimpleNamespace(), _Log()), True)
        session.logout.assert_called_once_with()
        with mock.patch.object(root, "authenticate_and_login", side_effect=AuthRejected("logon proof", 0x05)):
            self.assertIs(root.list_characters(SimpleNamespace(), _Log()), False)


class OneStartStopPathTest(unittest.TestCase):
    """run_loop() is the one place that starts and stops a session's workers: when it
    returns or raises, no reflex thread is left and the observer is detached."""

    def run_loop_and_count(self, **kw):
        from agent import runloop
        before = set(threading.enumerate())
        observer = mock.Mock()
        observer.action_lock = threading.Lock()
        sess = make_session()
        try:
            runloop.run_loop(sess, SimpleNamespace(think_interval=0.01, persona=""), 0.05, observer=observer, **kw)
        finally:
            self.leftover = [t for t in threading.enumerate() if t not in before and t.is_alive()]
        return observer

    def test_reflex_threads_are_gone_and_the_observer_is_detached_on_return(self):
        observer = self.run_loop_and_count()
        self.assertEqual(self.leftover, [])
        observer.attach.assert_called_once()
        observer.detach.assert_called_once_with()

    def test_the_same_holds_when_the_think_loop_crashes(self):
        with mock.patch("agent.runloop.think_and_act", side_effect=ValueError("bug")), self.assertRaises(ValueError):
            self.run_loop_and_count(brain=mock.Mock())
        self.assertEqual(self.leftover, [])


if __name__ == "__main__":
    unittest.main()
