"""#316: mission transitions end to end: the brain observes each snapshot, a transition is one
`mission_changed` event on the session, the observer and /perception see the zone, and the
owner's pin stops the automatic moves."""
import logging
import unittest
from unittest import mock

from agent import brain as br
from agent import config
from agent import root as main_mod
from agent.brain import BrainError, Decision
from agent.http_api import AgentObserver
from agent.perception import WorldState
from agent.state import GameState
from agent.think import Cycle, decide as think_decide

CAP = 10
ZONE_A, ZONE_B, ZONE_C = 12, 40, 1


def snap(level, zone):
    """A snapshot the candidate generator can read: alive, nothing around, a known position in `zone`."""
    return {"me": {"level": level, "health": "90/90"}, "nearby_units": [],
            "position": {"map": 0, "x": 100.0, "y": 50.0, "z": 0.0, "zone": zone}}


class FirstJev:
    """Scripted Jev: takes the first candidate."""
    model = "scripted"

    def choose_action(self, snapshot, options, persona="", history=None):
        return options[0]["action"], options[0]["params"]


def changes(brain, snapshots):
    """Run each snapshot through brain.decide; return [(step, mission_change)] for the transitions."""
    out = []
    for i, s in enumerate(snapshots):
        d = brain.decide(s)
        if d.mission_change is not None:
            out.append((i, d.mission_change))
    return out


class BrainTransitionTest(unittest.TestCase):
    def test_level_to_cap_switches_to_explore_once_at_the_cap(self):
        b = br.Brain(jev=FirstJev(), mission="level_to_cap", explore_new_zones=2, level_cap=CAP)
        found = changes(b, [snap(CAP - 1, ZONE_A), snap(CAP, ZONE_A), snap(CAP, ZONE_B), snap(CAP + 1, ZONE_B)])
        self.assertEqual(found, [(1, ("level_to_cap", "explore", "level_cap"))])
        self.assertEqual(b.mission, "explore")

    def test_explore_below_the_cap_returns_after_n_new_zones_once(self):
        b = br.Brain(jev=FirstJev(), mission="explore", explore_new_zones=2, level_cap=CAP)
        snaps = [snap(5, ZONE_A), snap(5, ZONE_A), snap(5, ZONE_B), snap(5, ZONE_C), snap(5, ZONE_A)]
        self.assertEqual(changes(b, snaps), [(2, ("explore", "level_to_cap", "explored"))])
        self.assertEqual(b.mission, "level_to_cap")

    def test_a_snapshot_without_level_or_zone_moves_nothing(self):
        b = br.Brain(jev=FirstJev(), mission="explore", explore_new_zones=1, level_cap=CAP)
        self.assertEqual(changes(b, [{"nearby_units": [], "position": None}, {"me": {"level": 5}}]), [])
        self.assertEqual(b.mission, "explore")

    def test_an_owner_pin_stops_the_automatic_transition(self):
        b = br.Brain(jev=FirstJev(), mission="level_to_cap", explore_new_zones=1, level_cap=CAP)
        b.mission_state.set_by_owner("level_to_cap")
        self.assertEqual(changes(b, [snap(CAP, ZONE_A), snap(CAP, ZONE_B)]), [])
        b.mission_state.unpin()
        self.assertEqual(changes(b, [snap(CAP, ZONE_C)]), [(0, ("level_to_cap", "explore", "level_cap"))])

    def test_the_brain_takes_the_settings_from_config(self):
        env = {"AGENT_BRAIN": "jev", "JEV_BASE_URL": "http://jev.invalid",
               "AGENT_LEVEL_CAP": "60", "AGENT_EXPLORE_NEW_ZONES": "3"}
        with mock.patch.dict("os.environ", env, clear=True):
            b = br.Brain.from_config(config.load_config())
        self.assertEqual((b.mission_state.level_cap, b.mission_state.explore_new_zones), (60, 3))


class _Brain:
    """A brain whose decide() returns a fixed Decision or raises a fixed error."""

    def __init__(self, result):
        self.result = result

    def decide(self, snapshot, **kw):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ThinkEventTest(unittest.TestCase):
    """think.decide records the transition on the session, once, also when the brain fails."""

    def cycle(self, brain):
        session = GameState()
        c = Cycle(session, None, brain, "", None, None, None, 1, None, None)
        return c, session

    def mission_events(self, session):
        return [(e["old"], e["new"], e["reason"]) for e in session.events if e["kind"] == "mission_changed"]

    def test_a_transition_is_one_mission_changed_event(self):
        d = Decision(action="idle", mission_change=("level_to_cap", "explore", "level_cap"))
        c, session = self.cycle(_Brain(d))
        think_decide(c)
        self.assertEqual(self.mission_events(session), [("level_to_cap", "explore", "level_cap")])

    def test_no_transition_no_event(self):
        c, session = self.cycle(_Brain(Decision(action="idle")))
        think_decide(c)
        self.assertEqual(self.mission_events(session), [])

    def test_a_failed_brain_cycle_still_records_the_transition(self):
        d = Decision(brain="llm", mission_change=("explore", "level_to_cap", "explored"))
        c, session = self.cycle(_Brain(BrainError("no brain", d)))
        think_decide(c)
        self.assertEqual(self.mission_events(session), [("explore", "level_to_cap", "explored")])
        self.assertIsNotNone(c.error)


class ZoneInSnapshotTest(unittest.TestCase):
    def test_the_self_position_carries_the_zone(self):
        out = WorldState().snapshot(my_position=(1, 10.0, 20.0, 30.0, 0.0), zone=ZONE_B)
        self.assertEqual(out["position"], {"map": 1, "x": 10.0, "y": 20.0, "z": 30.0, "zone": ZONE_B})

    def test_no_position_no_zone(self):
        self.assertIsNone(WorldState().snapshot(zone=ZONE_B)["position"])

    def test_no_zone_yet_no_zone_key(self):
        out = WorldState().snapshot(my_position=(1, 10.0, 20.0, 30.0, 0.0))
        self.assertNotIn("zone", out["position"])


class ObserverTest(unittest.TestCase):
    def test_the_observer_reads_the_brains_mission(self):
        b = br.Brain(jev=FirstJev(), mission="explore", explore_new_zones=1, level_cap=CAP)
        observer = AgentObserver("Lua", mission="explore")
        observer.mission_source = lambda: b.mission_state.current
        b.decide(snap(5, ZONE_A))
        self.assertEqual(observer.mission, "level_to_cap")
        self.assertEqual(observer.healthz()["mission"], "level_to_cap")

    def test_without_a_source_the_observer_keeps_its_own_mission(self):
        observer = AgentObserver("Lua", mission="explore")
        observer.mission = "make_gold"
        self.assertEqual(observer.mission, "make_gold")

    def test_start_observer_wires_the_brain_mission(self):
        b = br.Brain(jev=FirstJev(), mission="explore", explore_new_zones=2, level_cap=CAP)
        cfg = config.Config(agent_name="Lua", http_port=1, http_bind="127.0.0.1", mission="explore")
        with mock.patch.object(main_mod, "start_server", return_value=object()):
            observer, _ = main_mod.start_observer(cfg, None, logging.getLogger("t"), brain=b)
        b.decide(snap(5, ZONE_A))
        self.assertEqual(observer.mission, "explore")
        b.decide(snap(5, ZONE_B))   # the second new zone: back to level_to_cap
        self.assertEqual(observer.mission, "level_to_cap")


class MissionConfigTest(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            cfg = config.load_config()
        self.assertEqual((cfg.explore_new_zones, cfg.level_cap), (5, 80))

    def test_both_are_read_from_the_environment(self):
        env = {"AGENT_EXPLORE_NEW_ZONES": "7", "AGENT_LEVEL_CAP": "70"}
        with mock.patch.dict("os.environ", env, clear=True):
            cfg = config.load_config()
        self.assertEqual((cfg.explore_new_zones, cfg.level_cap), (7, 70))

    def test_explore_new_zones_below_one_is_a_problem_naming_the_setting(self):
        cfg = config.Config(account="a", password="p", character="c", explore_new_zones=0)
        self.assertTrue(any("AGENT_EXPLORE_NEW_ZONES" in p for p in cfg.validate()))


if __name__ == "__main__":
    unittest.main()
