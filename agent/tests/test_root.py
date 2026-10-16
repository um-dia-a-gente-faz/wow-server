"""#292: the composition root (agent/root.py): build_agent() wires one agent without
connecting, Agent.run() supervises it and closes what build_agent() started, and the
dry run stays outside the supervisor and the lifecycle."""

import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from agent import __main__ as main_mod
from agent import root
from agent.audit import AuditLogger
from agent.auth import AuthRejected
from agent.config import Config
from agent.lifecycle import Lifecycle
from agent.tests.builders import make_session
from agent.tests.test_reconnect import _RecordingLog

LOG = logging.getLogger("test.root")


class _Log(_RecordingLog):
    """_RecordingLog plus critical(), which the dry run and --list-chars use for a failed login."""

    def critical(self, *a):
        self._record("critical", *a)


class _OrderLog:
    """Logs into the shared step list, so a log line takes its place among the start-up steps."""

    def __init__(self, order):
        self.order = order

    def info(self, fmt, *args):
        self.order.append("log: " + fmt)

    def warning(self, fmt, *args):
        self.order.append("log: " + fmt)


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
        self.assertEqual(seen["start_delay"], 0.0)
        agent.chat_relay.stop.assert_called_once_with()
        agent.http_server.shutdown.assert_called_once_with()

    def test_run_hands_the_configured_start_delay_to_the_supervisor(self):
        agent = _agent(start_delay=42.0)
        with mock.patch.object(root, "supervise_connection") as sup:
            agent.run(threading.Event())
        self.assertEqual(sup.call_args.kwargs["start_delay"], 42.0)

    def test_a_real_config_start_delay_reaches_the_supervisor_through_build_agent(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {"AGENT_START_DELAY_S": "42", "AGENT_AUDIT_DIR": tmp.name, "AGENT_HTTP_PORT": "0"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = Config()
        agent = root.build_agent(cfg, _Log())
        self.addCleanup(agent.close)
        with mock.patch.object(root, "supervise_connection") as sup:
            agent.run(threading.Event())
        self.assertEqual(sup.call_args.kwargs["start_delay"], 42.0)

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
                                         audit_logger=agent.audit_logger, observer=None, stop=stop, unmet=None)

    def test_run_session_hands_its_brain_and_observer_to_run_loop(self):
        brain, observer, stop = object(), object(), threading.Event()
        agent = _agent(brain=brain, observer=observer)
        with mock.patch.object(root, "run_loop", return_value=False) as run_loop:
            agent.run_session("SESSION", stop)
        self.assertIs(run_loop.call_args.kwargs["brain"], brain)
        self.assertIs(run_loop.call_args.kwargs["observer"], observer)

    def test_close_stops_the_relay_then_the_observer_and_a_second_close_repeats_both_without_raising(self):
        order, agent = [], _agent()
        agent.chat_relay.stop.side_effect = lambda: order.append("chat_relay.stop")
        agent.http_server.shutdown.side_effect = lambda: order.append("http_server.shutdown")
        agent.close()
        self.assertEqual(order, ["chat_relay.stop", "http_server.shutdown"])
        agent.close()   # close() has no guard: both stops run again and nothing raises
        self.assertEqual(order, ["chat_relay.stop", "http_server.shutdown"] * 2)

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
                               chat_relay_timeout=1.0, start_delay=0.0)

    def test_records_every_start_up_step_in_the_order_main_used_and_does_not_connect(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        order, brain, observer, server = [], mock.Mock(), root.AgentObserver("Built"), mock.Mock()
        relay, real_lifecycle = mock.Mock(), root.build_lifecycle
        brain.describe.return_value = "llm"
        brain.restore_jev_spend.side_effect = lambda *a: order.append("restore_jev_spend")

        def step(name, value):
            return lambda *a, **kw: (order.append(name), value)[1]

        def audit(*a, **kw):
            order.append("audit_logger")
            return AuditLogger(*a, **kw)

        def lifecycle(audit_logger, obs):
            order.append("lifecycle")
            return real_lifecycle(audit_logger, obs)

        with mock.patch.object(root, "build_chat_relay", side_effect=step("chat_relay", relay)), \
                mock.patch.object(root.Brain, "from_config", side_effect=step("brain", brain)), \
                mock.patch.object(root, "warn_jev_unselected", side_effect=step("jev_warning", None)), \
                mock.patch.object(root, "AuditLogger", side_effect=audit), \
                mock.patch.object(root, "start_observer", side_effect=step("start_observer", (observer, server))), \
                mock.patch.object(root, "build_lifecycle", side_effect=lifecycle), \
                mock.patch.object(root, "connect_and_login", side_effect=AssertionError("build_agent connected")):
            agent = root.build_agent(self.cfg(tmp.name), _OrderLog(order), 12.0, perception_dump=True)
        self.assertEqual(order, ["chat_relay", "brain", "jev_warning", "log: brain: %s", "audit_logger",
                                 "log: audit log: %s (retention %d days)", "restore_jev_spend",
                                 "start_observer", "lifecycle"])
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

    def test_one_unmet_intents_recorder_is_the_observers_and_writes_to_the_audit_dir(self):
        # #319: build_agent makes one UnmetIntents, the observer serves it and it audits to <agent>/unmet/
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        observer = root.AgentObserver("Built")
        with mock.patch.object(root.Brain, "from_config", return_value=None), \
                mock.patch.object(root, "start_observer", return_value=(observer, None)), \
                self.assertLogs(LOG, level="WARNING"):
            agent = root.build_agent(self.cfg(tmp.name), LOG)
        self.assertIs(observer.unmet, agent.unmet)
        self.assertIs(agent.unmet.audit_logger, agent.audit_logger)
        self.assertEqual(agent.unmet.mission, "level_to_cap")

    def test_the_brain_and_audit_log_lines_are_each_logged_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        brain = mock.Mock()
        brain.describe.return_value = "llm"
        with mock.patch.object(root.Brain, "from_config", return_value=brain), self.assertLogs(LOG, level="INFO") as cm:
            agent = root.build_agent(self.cfg(tmp.name), LOG)
        self.addCleanup(agent.close)
        messages = [r.getMessage() for r in cm.records]
        self.assertEqual(sum(m == "brain: llm" for m in messages), 1)
        self.assertEqual(sum(m.startswith("audit log: ") for m in messages), 1)

    def test_the_brain_and_the_audit_log_reach_the_observer(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg, brain = self.cfg(tmp.name), mock.Mock(model="brain-model")
        vars(cfg).update(http_port=8123, http_bind="127.0.0.1", persona="", llm_model="cfg-model", control_token="")
        with mock.patch.object(root.Brain, "from_config", return_value=brain), \
                mock.patch.object(root, "start_server", return_value=mock.Mock()):
            agent = root.build_agent(cfg, LOG)
        self.addCleanup(agent.close)
        self.assertEqual(agent.observer.model, "brain-model")
        self.assertEqual(agent.audit_logger.on_record, agent.observer.record_decision)


class WiringPartsTest(unittest.TestCase):
    """The parts build_agent() relies on: the audit log feeds the observer, the control
    token reaches it, and an enabled chat relay is started."""

    def observer_cfg(self):
        return SimpleNamespace(http_port=8123, http_bind="127.0.0.1", agent_name="Wired", persona="",
                               llm_model="m", control_token="tok")

    def start(self, tmp):
        audit = AuditLogger("Wired", base_dir=tmp)
        with mock.patch.object(root, "start_server", return_value=mock.Mock()):
            observer, _server = root.start_observer(self.observer_cfg(), audit, LOG)
        return audit, observer

    def test_the_audit_log_feeds_the_observer(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        audit, observer = self.start(tmp.name)
        self.assertEqual(audit.on_record, observer.record_decision)

    def test_the_control_token_reaches_the_observer(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _audit, observer = self.start(tmp.name)
        self.assertEqual(observer.control_token, "tok")

    def test_an_enabled_chat_relay_is_started(self):
        relay = mock.Mock(enabled=True)
        cfg = SimpleNamespace(chat_relay_url="http://relay", agent_name="Wired", chat_relay_token="",
                              chat_relay_timeout=1.0, start_delay=0.0)
        with mock.patch.object(root, "ChatRelay", return_value=relay):
            self.assertIs(root.build_chat_relay(cfg, LOG), relay)
        relay.start.assert_called_once_with()


class MainLogLinesTest(unittest.TestCase):
    """The two lines main() logs around the agent run: 'entering agent loop' and 'done.'."""

    def run_main(self):
        self.agent, self.stop = mock.Mock(), threading.Event()
        cfg = mock.MagicMock(run_duration=0.0)
        cfg.validate.return_value = []
        with mock.patch.object(sys, "argv", ["wow-agent"]), \
                mock.patch.object(main_mod, "load_config", return_value=cfg), \
                mock.patch.object(main_mod, "_setup_logging"), \
                mock.patch.object(main_mod, "build_agent", return_value=self.agent), \
                mock.patch.object(main_mod, "_install_stop_signals", return_value=self.stop), \
                self.assertLogs("agent", level="INFO") as cm:
            main_mod.main()
        return [r.getMessage() for r in cm.records]

    def test_entering_the_loop_and_done_are_each_logged_once(self):
        messages = self.run_main()
        self.assertEqual(sum(m.startswith("entering agent loop") for m in messages), 1)
        self.assertEqual(sum(m == "done." for m in messages), 1)

    def test_main_runs_the_agent_once_and_leaves_the_closing_to_run(self):
        self.run_main()
        self.assertEqual(self.agent.mock_calls, [mock.call.run(self.stop)])


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
