"""#292: the reconnect supervisor drives the connection lifecycle of ADR 0015.

One table row per way through supervise_connection(): what build_session() and
run_session() do, when the stop arrives, and the exact transitions expected. The fakes
never sleep and never open a socket."""

import json
import os
import tempfile
import threading
import time
import unittest

from agent import metrics, root
from agent.audit import AuditLogger
from agent.auth import AuthRejected
from agent.http_api import AgentObserver
from agent.lifecycle import REASONS, Lifecycle, State
from agent.supervisor import supervise_connection
from agent.tests.restore import reset_counters
from agent.tests.test_reconnect import _NoThrottle, _RecordingLog
from unittest import mock

OK, DROP, END = "ok", "drop", "end"   # build: a session; run: dropped (True) / ended cleanly (False)


class Boom(Exception):
    """A crash that is not a connect failure."""


class Unwind(BaseException):
    """Leaves build_session() past the supervisor's `except Exception` (as KeyboardInterrupt would)."""


class Session:
    def __init__(self, world):
        self.world = world

    def logout(self):
        self.world.states_at_logout.append(self.world.lifecycle.state)
        if self.world.stop_on_logout:
            self.world.stop.set()


class World:
    """Scripts one supervisor run. `builds` and `runs` are consumed one per call; an
    exception instance is raised, STOP sets the stop event and returns the value after it."""

    def __init__(self, builds, runs=(), stop_on_sleep=None, sleep_raises=None, stop_on_logout=False,
                 lifecycle=None, start_delay=0.0):
        self.builds, self.runs = list(builds), list(runs)
        self.stop = threading.Event()
        self.stop_on_sleep, self.sleep_raises, self.stop_on_logout = stop_on_sleep, sleep_raises, stop_on_logout
        self.sleeps, self.states_at_logout, self.start_delay = [], [], start_delay
        self.lifecycle = lifecycle if lifecycle is not None else Lifecycle(clock=lambda: 0.0)

    def build_session(self):
        step = self.builds.pop(0)
        if isinstance(step, BaseException):
            raise step
        return Session(self)

    def run_session(self, session):
        step = self.runs.pop(0)
        if isinstance(step, BaseException):
            raise step
        if step == "stop+drop":
            self.stop.set()
            return True
        return step == DROP

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) > 6:
            raise AssertionError(f"supervisor kept retrying: sleeps {self.sleeps}")
        if self.sleep_raises is not None:
            raise self.sleep_raises
        if self.stop_on_sleep == len(self.sleeps):
            self.stop.set()

    def supervise(self):
        supervise_connection(self.build_session, self.run_session, log=_RecordingLog(), sleep=self.sleep,
                             stop=self.stop, jitter=lambda b: b, initial_backoff=5.0, max_backoff=300.0,
                             lifecycle=self.lifecycle, start_delay=self.start_delay)

    def walk(self):
        return [(t.source.value, t.target.value, t.reason) for t in self.lifecycle.history()]


CONNECTED = ("connecting", "online", "connected")
RETRY = ("backoff", "connecting", "backoff_elapsed")
ENDED = [("online", "stopping", "run_ended"), ("stopping", "stopped", "run_ended")]
STOPPED_ONLINE = [("online", "stopping", "stop"), ("stopping", "stopped", "stop")]
STOPPED_WAITING = [("backoff", "stopping", "stop"), ("stopping", "stopped", "stop")]

# name, World kwargs, exception expected to leave the supervisor (or None), transitions, sleeps
SCENARIOS = [
    ("clean run", dict(builds=[OK], runs=[END]), None, [CONNECTED, *ENDED], []),
    ("connect fails twice then runs", dict(builds=[RuntimeError("refused"), OSError("reset"), OK], runs=[END]), None,
     [("connecting", "backoff", "connect_failed"), RETRY, ("connecting", "backoff", "connect_failed"), RETRY,
      CONNECTED, *ENDED], [5.0, 10.0]),
    ("transient auth refusal", dict(builds=[AuthRejected("logon proof", 0x06), OK], runs=[END]), None,
     [("connecting", "backoff", "auth_rejected"), RETRY, CONNECTED, *ENDED], [5.0]),
    ("permanent auth refusal", dict(builds=[AuthRejected("logon proof", 0x03)]), AuthRejected,
     [("connecting", "failed", "auth_rejected_permanent")], []),
    ("drop then clean run", dict(builds=[OK, OK], runs=[DROP, END]), None,
     [CONNECTED, ("online", "backoff", "disconnected"), RETRY, CONNECTED, *ENDED], [5.0]),
    ("stop mid session", dict(builds=[OK], runs=["stop+drop"]), None, [CONNECTED, *STOPPED_ONLINE], []),
    ("stop during the connect retry wait", dict(builds=[RuntimeError("refused")], stop_on_sleep=1), None,
     [("connecting", "backoff", "connect_failed"), *STOPPED_WAITING], [5.0]),
    ("stop during the post-disconnect wait", dict(builds=[OK], runs=[DROP], stop_on_sleep=1), None,
     [CONNECTED, ("online", "backoff", "disconnected"), *STOPPED_WAITING], [5.0]),
    ("stop during the logout of a dropped session", dict(builds=[OK], runs=[DROP], stop_on_logout=True), None,
     [CONNECTED, *STOPPED_ONLINE], []),
    ("run_session crashes", dict(builds=[OK], runs=[Boom("bug in the think loop")]), Boom,
     [CONNECTED, ("online", "stopping", "crashed"), ("stopping", "stopped", "crashed")], []),
    ("the wait raises", dict(builds=[RuntimeError("refused")], sleep_raises=Boom("harness is done")), Boom,
     [("connecting", "backoff", "connect_failed"), ("backoff", "stopping", "crashed"),
      ("stopping", "stopped", "crashed")], [5.0]),
    ("build_session is unwound", dict(builds=[Unwind()]), Unwind,
     [("connecting", "stopping", "crashed"), ("stopping", "stopped", "crashed")], []),
]


class SupervisorLifecycleTest(unittest.TestCase):
    def test_every_way_through_the_supervisor(self):
        for name, kwargs, raises, transitions, sleeps in SCENARIOS:
            with self.subTest(scenario=name):
                world = World(**kwargs)
                if raises is None:
                    world.supervise()
                else:
                    with self.assertRaises(raises):
                        world.supervise()
                self.assertEqual(world.walk(), transitions)
                self.assertEqual(world.sleeps, sleeps)
                self.assertTrue(world.lifecycle.terminal)
                self.assertEqual((world.builds, world.runs), ([], []), "the script was not consumed")

    def test_the_original_exception_leaves_unchanged(self):
        for crash in (Boom("think loop"), AuthRejected("logon proof", 0x03)):
            with self.subTest(crash=type(crash).__name__):
                world = World(builds=[crash]) if isinstance(crash, AuthRejected) else World(builds=[OK], runs=[crash])
                with self.assertRaises(type(crash)) as cm:
                    world.supervise()
                self.assertIs(cm.exception, crash)

    def test_stopping_is_recorded_before_the_logout_except_for_a_drop(self):
        expected = {"clean run": [State.STOPPING], "stop mid session": [State.STOPPING],
                    "run_session crashes": [State.STOPPING],
                    "drop then clean run": [State.ONLINE, State.STOPPING]}
        for name, kwargs, raises, _transitions, _sleeps in SCENARIOS:
            if name not in expected:
                continue
            with self.subTest(scenario=name):
                world = World(**kwargs)
                if raises is None:
                    world.supervise()
                else:
                    with self.assertRaises(raises):
                        world.supervise()
                self.assertEqual(world.states_at_logout, expected[name])

    def test_the_table_uses_every_reason_and_only_reasons(self):
        used = {t[2] for _name, _kw, _raises, transitions, _sleeps in SCENARIOS for t in transitions}
        self.assertEqual(used, set(REASONS))

    def test_the_table_makes_every_allowed_transition(self):
        from agent.lifecycle import TRANSITIONS
        made = {(t[0], t[1]) for _name, _kw, _raises, transitions, _sleeps in SCENARIOS for t in transitions}
        self.assertEqual(made, {(s.value, t.value) for s, ts in TRANSITIONS.items() for t in ts})

    def test_without_a_lifecycle_argument_the_supervisor_makes_its_own(self):
        world = World(builds=[OK], runs=[END])
        supervise_connection(world.build_session, world.run_session, log=_RecordingLog(), sleep=world.sleep)
        self.assertEqual(world.lifecycle.history(), ())   # the one passed to World was not used


class StartDelayTest(unittest.TestCase):
    """#539: AGENT_START_DELAY_S is one wait before the first attempt, not before a reconnect."""

    def test_the_delay_is_taken_once_before_the_first_attempt(self):
        world = World(builds=[OK, OK], runs=[DROP, END], start_delay=30.0)
        order = world.sleeps
        build = world.build_session
        world.build_session = lambda: (order.append("build"), build())[1]
        world.supervise()
        self.assertEqual(order[:2], [30.0, "build"])
        self.assertEqual([s for s in order if s != "build"], [30.0, 5.0])  # the reconnect wait is the backoff
        self.assertEqual(world.walk()[0], CONNECTED)  # still CONNECTING until the first attempt succeeds

    def test_no_delay_by_default(self):
        world = World(builds=[OK], runs=[END])
        world.supervise()
        self.assertEqual(world.sleeps, [])

    def test_stop_during_the_delay_ends_before_any_attempt(self):
        world = World(builds=[OK], stop_on_sleep=1, start_delay=30.0)
        world.supervise()
        self.assertEqual(world.sleeps, [30.0])
        self.assertEqual(world.builds, [OK], "connected after a stop")
        self.assertEqual(world.walk(), [("connecting", "stopping", "stop"), ("stopping", "stopped", "stop")])

    def test_the_logged_delay_is_the_value_as_given(self):
        import logging
        world = World(builds=[OK], runs=[END], start_delay=3.5)
        with self.assertLogs("start-delay-fmt", "INFO") as logs:
            supervise_connection(world.build_session, world.run_session, log=logging.getLogger("start-delay-fmt"),
                                 sleep=world.sleep, stop=world.stop, start_delay=3.5)
        self.assertIn("waiting 3.5s before", logs.output[0])

    def test_a_default_wait_returns_when_stop_is_set(self):
        import logging
        stop = threading.Event()
        threading.Timer(0.05, stop.set).start()
        t0 = time.monotonic()
        with self.assertLogs("start-delay-test", "INFO") as logs:
            supervise_connection(lambda: self.fail("connected"), lambda s: False,
                                 log=logging.getLogger("start-delay-test"), stop=stop, start_delay=60.0)
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(logs.output, ["INFO:start-delay-test:waiting 60s before the first connection attempt "
                                       "(AGENT_START_DELAY_S)"])


class AuthPortClosedTest(unittest.TestCase):
    """#539 point 2: a closed auth port is `connect_failed`, apart from the server's own refusal
    (`auth_rejected`); no SRP byte is sent, and the retry backs off."""

    def test_refused_connect_is_connect_failed_and_sends_no_srp_packet(self):
        import socket
        from agent import auth
        with socket.socket() as s:  # a free loopback port with nothing listening
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        world = World(builds=[OK], runs=[END])
        log = _RecordingLog()
        calls = []

        def build_session():
            if calls:
                return World.build_session(world)
            calls.append(1)
            auth.auth_logon("127.0.0.1", port, "A", "B")

        with mock.patch.object(auth, "_handshake") as handshake:
            supervise_connection(build_session, world.run_session, log=log, sleep=world.sleep,
                                 stop=world.stop, jitter=lambda b: b, initial_backoff=5.0,
                                 lifecycle=world.lifecycle)
        handshake.assert_not_called()
        self.assertEqual(world.walk()[:2], [("connecting", "backoff", "connect_failed"), RETRY])
        self.assertEqual(world.sleeps, [5.0])
        self.assertIn("connect failed (ConnectionRefusedError:", log.lines[0][1])


class LifecycleNeverChangesTheSupervisorTest(unittest.TestCase):
    """The lifecycle is bookkeeping: a failing callback or a refused transition is counted, nothing else."""

    def setUp(self):
        reset_counters(self)
        patcher = mock.patch.object(metrics, "_swallowed_throttle", _NoThrottle())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_callback_that_raises_changes_nothing(self):
        def full_disk(transition):
            raise OSError(28, "No space left on device")

        world = World(builds=[OK, OK], runs=[DROP, END], lifecycle=Lifecycle(on_transition=full_disk))
        world.supervise()
        self.assertEqual(world.sleeps, [5.0])
        self.assertEqual(world.walk()[-1], ("stopping", "stopped", "run_ended"))   # the machine still moved
        self.assertEqual(metrics.SWALLOWED["supervisor.lifecycle"], 6)             # one per transition

    def test_a_callback_that_raises_changes_no_scenario(self):
        def full_disk(transition):
            raise OSError(28, "No space left on device")

        for name, kwargs, raises, transitions, sleeps in SCENARIOS:
            with self.subTest(scenario=name):
                world = World(**kwargs, lifecycle=Lifecycle(on_transition=full_disk))
                if raises is None:
                    world.supervise()
                else:
                    scripted = [s for s in (*kwargs["builds"], *kwargs.get("runs", ()), kwargs.get("sleep_raises"))
                                if isinstance(s, raises)]
                    with self.assertRaises(raises) as cm:
                        world.supervise()
                    self.assertIs(cm.exception, scripted[-1])
                self.assertEqual(world.walk(), transitions)
                self.assertEqual(world.sleeps, sleeps)

    def test_a_callback_that_raises_does_not_mask_a_crash(self):
        def full_disk(transition):
            raise OSError(28, "No space left on device")

        crash = Boom("think loop")
        world = World(builds=[OK], runs=[crash], lifecycle=Lifecycle(on_transition=full_disk))
        with self.assertRaises(Boom) as cm:
            world.supervise()
        self.assertIs(cm.exception, crash)

    def test_a_used_lifecycle_is_refused_and_the_supervisor_still_runs(self):
        used = Lifecycle()
        used.to(State.STOPPING, "stop")
        used.to(State.STOPPED, "stop")
        world = World(builds=[OK, OK], runs=[DROP, END], lifecycle=used)
        world.supervise()
        self.assertEqual(world.sleeps, [5.0])
        self.assertEqual(len(used.history()), 2)                                   # nothing was added
        self.assertEqual(metrics.SWALLOWED["supervisor.lifecycle"], 5)   # ONLINE, BACKOFF, CONNECTING, ONLINE, STOPPING


class MainLifecycleWiringTest(unittest.TestCase):
    """root.build_lifecycle: wall clock, one audit line per transition in order, the snapshot in GET /state."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.audit = AuditLogger("Wired", base_dir=tmp.name)
        self.observer = AgentObserver("Wired")
        self.directory = os.path.join(tmp.name, "Wired", "lifecycle")

    def lines(self):
        (name,) = os.listdir(self.directory)
        with open(os.path.join(self.directory, name)) as f:
            return [json.loads(line) for line in f]

    def test_each_transition_is_one_audit_line_in_order_and_state_shows_the_snapshot(self):
        before = time.time()
        lifecycle = root.build_lifecycle(self.audit, self.observer)
        self.assertIs(self.observer.lifecycle, lifecycle)
        world = World(builds=[RuntimeError("refused"), OK, OK], runs=[DROP, END], lifecycle=lifecycle)
        world.supervise()
        lines = self.lines()
        self.assertEqual([(d["from"], d["to"], d["reason"]) for d in lines], world.walk())
        self.assertEqual([d["ts"] for d in lines], [t.at for t in lifecycle.history()])
        self.assertTrue(all(before <= d["ts"] <= time.time() for d in lines), "at is not the wall clock")
        self.assertEqual({d["agent"] for d in lines}, {"Wired"})
        state = self.observer.state()
        self.assertIs(state["connected"], False)
        self.assertEqual(state["lifecycle"]["state"], "stopped")
        self.assertEqual(state["lifecycle"]["reason"], "run_ended")
        self.assertEqual(len(state["lifecycle"]["transitions"]), len(lines))

    def test_no_decision_record_is_written_and_on_record_is_not_called(self):
        seen = []
        self.audit.on_record = seen.append
        world = World(builds=[OK], runs=[END], lifecycle=root.build_lifecycle(self.audit, None))
        world.supervise()
        self.assertEqual(seen, [])
        self.assertEqual(os.listdir(os.path.dirname(self.directory)), ["lifecycle"])

    def test_a_crash_is_on_disk_before_the_exception_leaves(self):
        world = World(builds=[OK], runs=[Boom("think loop")], lifecycle=root.build_lifecycle(self.audit, None))
        with self.assertRaises(Boom):
            world.supervise()
        self.assertEqual([(d["to"], d["reason"]) for d in self.lines()][-2:],
                         [("stopping", "crashed"), ("stopped", "crashed")])

    def test_the_lifecycle_file_is_named_from_the_line_ts_not_the_clock(self):
        three_days_ago = time.time() - 3 * 86400
        self.audit.record_lifecycle("connecting", "online", "connected", ts=three_days_ago)
        (name,) = os.listdir(self.directory)
        self.assertEqual(name, self.audit._date_str(three_days_ago) + ".jsonl")
        self.assertNotEqual(name, self.audit._date_str(time.time()) + ".jsonl")

    def test_the_key_order_of_a_lifecycle_line(self):
        self.audit.record_lifecycle("connecting", "online", "connected", ts=1.0)
        (name,) = os.listdir(self.directory)
        with open(os.path.join(self.directory, name)) as f:
            self.assertEqual(list(json.loads(f.readline())),
                             ["schema", "ts", "agent", "from", "to", "reason"])

    def test_the_production_machine_uses_the_wall_clock(self):
        lifecycle = root.build_lifecycle(self.audit, None)
        lifecycle.to(State.ONLINE, "connected")
        (name,) = os.listdir(self.directory)
        self.assertNotIn("1970", name)   # time.monotonic() would give lifecycle/1970-01-01.jsonl
        self.assertGreater(lifecycle.history()[0].at, 1.6e9)

    def test_an_unwritable_lifecycle_directory_does_not_stop_a_reconnect(self):
        reset_counters(self)
        os.makedirs(self.directory)
        os.chmod(self.directory, 0o500)
        self.addCleanup(os.chmod, self.directory, 0o700)
        if os.access(self.directory, os.W_OK):
            self.skipTest("running as a user that ignores directory permissions")
        world = World(builds=[OK, OK], runs=[DROP, END], lifecycle=root.build_lifecycle(self.audit, None))
        world.supervise()
        self.assertEqual(world.sleeps, [5.0])
        self.assertEqual(world.walk()[-1], ("stopping", "stopped", "run_ended"))
        self.assertEqual(metrics.SWALLOWED["supervisor.lifecycle"], 6)


class LogoutRaisesTest(unittest.TestCase):
    def test_a_logout_that_raises_after_stop_ends_stopped_crashed(self):
        world = World(builds=[OK], runs=["stop+drop"])

        class Unwound(Session):
            def logout(self):
                raise Unwind()

        world.build_session = lambda: Unwound(world)
        with self.assertRaises(Unwind):
            world.supervise()
        self.assertEqual(world.walk(), [CONNECTED, ("online", "stopping", "stop"), ("stopping", "stopped", "crashed")])


if __name__ == "__main__":
    unittest.main()
