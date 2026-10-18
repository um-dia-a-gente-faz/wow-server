"""#320: the idle watchdog. Unit tests on agent.watchdog.Watchdog with an injected clock, and
seam tests through think_and_act (a follow that stops, one no_progress record)."""
import unittest
from unittest import mock

from agent import actions as ac
from agent import config
from agent import think
from agent.reflexes.follow import FollowReflex
from agent.tests.builders import FakeSession
from agent.tests.test_think import FakeLLMClient, RecordingAction, _world_with_self
from agent.think import think_and_act
from agent.unmet import UnmetIntents
from agent.watchdog import Watchdog


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def moving(i: int) -> dict:
    """A snapshot whose own position differs on every call."""
    return {"position": {"map": 0, "x": float(i * 5), "y": 0.0, "z": 0.0}}


STILL = {"position": {"map": 0, "x": 1.0, "y": 2.0, "z": 3.0}}


class WatchdogTest(unittest.TestCase):

    def test_idle_fires_once_per_stretch_and_again_after_a_change(self):
        clock = FakeClock()
        wd = Watchdog(window_s=300, idle_cycles=3, clock=clock)
        fired = [t for _ in range(10) for t in wd.observe(STILL, following=False)]
        self.assertEqual(fired, ["idle"])
        fired = [t for t in wd.observe(moving(1), following=False)]
        self.assertEqual(fired, [])  # a change re-arms; it is not itself a trigger
        fired = [t for _ in range(5) for t in wd.observe(moving(1), following=False)]
        self.assertEqual(fired, ["idle"])

    def test_stuck_follow_fires_after_the_window_while_following(self):
        clock = FakeClock()
        wd = Watchdog(window_s=10, idle_cycles=0, clock=clock)
        fired = []
        for _ in range(20):
            clock.t += 1.0
            fired += wd.observe(STILL, following=True)
        self.assertEqual(fired, ["stuck_follow"])

    def test_stuck_follow_never_fires_when_not_following(self):
        clock = FakeClock()
        wd = Watchdog(window_s=10, idle_cycles=0, clock=clock)
        fired = []
        for _ in range(30):
            clock.t += 1.0
            fired += wd.observe(STILL, following=False)
        self.assertEqual(fired, [])

    def test_stuck_follow_counts_from_when_the_follow_started(self):
        clock = FakeClock()
        wd = Watchdog(window_s=10, idle_cycles=0, clock=clock)
        for _ in range(30):  # parked and not following for three windows
            clock.t += 1.0
            self.assertEqual(wd.observe(STILL, following=False), [])
        fired = []
        for _ in range(10):  # the follow starts at the first of these: 0 to 9 s elapsed
            clock.t += 1.0
            fired += wd.observe(STILL, following=True)
        self.assertEqual(fired, [])
        clock.t += 1.0  # 10 s since the follow started: a full window
        self.assertEqual(wd.observe(STILL, following=True), ["stuck_follow"])

    def test_stuck_follow_window_restarts_with_a_second_follow(self):
        clock = FakeClock()
        wd = Watchdog(window_s=10, idle_cycles=0, clock=clock)
        fired = []
        for _ in range(11):
            clock.t += 1.0
            fired += wd.observe(STILL, following=True)
        self.assertEqual(fired, ["stuck_follow"])
        clock.t += 1.0
        self.assertEqual(wd.observe(moving(1), following=False), [])  # follow stopped, progress
        for _ in range(30):  # parked, not following, for three windows
            clock.t += 1.0
            self.assertEqual(wd.observe(moving(1), following=False), [])
        for _ in range(10):  # the second follow starts at the first of these: 0 to 9 s elapsed
            clock.t += 1.0
            fired += wd.observe(moving(1), following=True)
        self.assertEqual(fired, ["stuck_follow"])  # still only the first one
        clock.t += 1.0  # a full window since the restart
        self.assertEqual(wd.observe(moving(1), following=True), ["stuck_follow"])

    def test_normal_movement_never_trips_either_trigger(self):
        clock = FakeClock()
        wd = Watchdog(window_s=10, idle_cycles=5, clock=clock)
        fired = []
        for i in range(3 * 10 * 5):  # 3x the window in seconds and 3x the idle cycles
            clock.t += 1.0
            fired += wd.observe(moving(i), following=True)
        self.assertEqual(fired, [])

    def test_zero_disables_each_trigger(self):
        clock = FakeClock()
        no_window = Watchdog(window_s=0, idle_cycles=3, clock=clock)
        no_idle = Watchdog(window_s=10, idle_cycles=0, clock=clock)
        fired_w, fired_i = [], []
        for _ in range(50):
            clock.t += 1.0
            fired_w += no_window.observe(STILL, following=True)
            fired_i += no_idle.observe(STILL, following=True)
        self.assertEqual(fired_w, ["idle"])  # the idle trigger still runs
        self.assertEqual(fired_i, ["stuck_follow"])  # and the follow trigger still runs

    def test_stuck_follow_rearms_after_a_change(self):
        clock = FakeClock()
        wd = Watchdog(window_s=5, idle_cycles=0, clock=clock)
        fired = []
        for _ in range(10):
            clock.t += 1.0
            fired += wd.observe(STILL, following=True)
        clock.t += 1.0
        fired += wd.observe(moving(1), following=True)
        for _ in range(10):
            clock.t += 1.0
            fired += wd.observe(moving(1), following=True)
        self.assertEqual(fired, ["stuck_follow", "stuck_follow"])


class ThinkWatchdogSeamTest(unittest.TestCase):
    """The watchdog through think_and_act: a stuck follow is stopped with reason 'watchdog',
    and an idle stretch records one no_progress unmet intent."""

    ENV = {"AGENT_WATCHDOG_WINDOW_S": "10", "AGENT_WATCHDOG_IDLE_CYCLES": "0"}

    def setUp(self):
        self.clock = FakeClock()
        self.world = _world_with_self()
        self.sess = FakeSession(race=10, player_guid=0x1, player_position=(0, 0.0, 0.0, 0.0, 0.0))
        self.reflex = FollowReflex(clock=self.clock)
        self.reflex.start(0x99, leader_name="Leader")
        self.sess.follow_reflex = self.reflex
        self.unmet = UnmetIntents("Luaprata", "explore", clock=self.clock)
        self.llm = FakeLLMClient(action_name="test_action", params={})
        self.registry = {"test_action": RecordingAction()}

    def cycle(self, state):
        self.clock.t += 1.0
        return think_and_act(self.sess, self.world, self.llm, my_position=self.sess.player_position,
                             registry=self.registry, state=state, unmet=self.unmet)

    def test_stuck_follow_is_stopped_after_the_window_with_one_record(self):
        with mock.patch.dict("os.environ", self.ENV, clear=True):
            state = think.ThinkState(clock=self.clock)
        for _ in range(9):
            self.cycle(state)
        self.assertTrue(self.reflex.enabled)
        for _ in range(3):
            self.cycle(state)
        self.assertFalse(self.reflex.enabled)
        stops = [e for e in self.sess.events if e["kind"] == "follow_stopped"]
        self.assertEqual([e["reason"] for e in stops], ["watchdog"])
        self.assertEqual([r["kind"] for r in self.unmet.recent()], ["no_progress"])

    def test_idle_stretch_records_one_no_progress_per_stretch(self):
        env = {"AGENT_WATCHDOG_WINDOW_S": "0", "AGENT_WATCHDOG_IDLE_CYCLES": "4"}
        self.reflex.stop()
        self.llm = FakeLLMClient(action_name="test_action", params={"value": 7})
        with mock.patch.dict("os.environ", env, clear=True):
            state = think.ThinkState(clock=self.clock)
        for _ in range(12):
            self.cycle(state)
        records = self.unmet.recent()
        self.assertEqual([r["kind"] for r in records], ["no_progress"])
        self.assertEqual(records[0]["wanted"]["action"], "test_action")
        self.assertEqual(records[0]["wanted"]["params"], {})  # params=None: the cycle's args are not recorded

    def test_a_follow_started_after_a_long_park_gets_a_full_window(self):
        class StartFollow(RecordingAction):
            name = "test_follow"

            def execute(self, session, world, **params):
                session.follow_reflex.start(0x99, leader_name="Leader")
                return ac.ActionResult(ok=True, detail={"followed": True})

        self.reflex.stop()
        self.registry = {"test_action": RecordingAction(), "test_follow": StartFollow()}
        with mock.patch.dict("os.environ", self.ENV, clear=True):
            state = think.ThinkState(clock=self.clock)
        for _ in range(30):  # parked, not following, for three windows
            self.cycle(state)
        self.llm = FakeLLMClient(action_name="test_follow", params={})
        self.cycle(state)  # the brain's follow action runs in this cycle
        self.assertTrue(self.reflex.enabled)
        for _ in range(5):  # well inside the window since the follow started
            self.llm = FakeLLMClient(action_name="test_action", params={})
            self.cycle(state)
        self.assertTrue(self.reflex.enabled)

    def test_disabled_window_leaves_the_follow_running(self):
        env = dict(self.ENV, AGENT_WATCHDOG_WINDOW_S="0")
        with mock.patch.dict("os.environ", env, clear=True):
            state = think.ThinkState(clock=self.clock)
        for _ in range(50):
            self.cycle(state)
        self.assertTrue(self.reflex.enabled)

    def test_a_watchdog_failure_never_reaches_the_think_loop(self):
        with mock.patch.dict("os.environ", self.ENV, clear=True):
            state = think.ThinkState(clock=self.clock)
        with mock.patch.object(state.watchdog, "observe", side_effect=RuntimeError("boom")):
            result = self.cycle(state)
        self.assertTrue(result.ok)

    def test_defaults_are_300_seconds_and_40_cycles(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(config.get("AGENT_WATCHDOG_WINDOW_S"), 300.0)
            self.assertEqual(config.get("AGENT_WATCHDOG_IDLE_CYCLES"), 40)


if __name__ == "__main__":
    unittest.main()
