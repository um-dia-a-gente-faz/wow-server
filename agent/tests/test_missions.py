"""#314: the mission model end to end in the brain: the config value reaches the candidate
generator through Brain, and a fallback brain with nothing around never idles twice in a row."""
import unittest
from collections import Counter
from unittest import mock

from agent import brain as br
from agent import config
from agent import missions


def SNAP():
    """Alive, healthy, standing at a known spot, nothing around."""
    return {"me": {"level": 3, "health": "90/90"}, "nearby_units": [],
            "position": {"map": 1, "x": 100.0, "y": 50.0, "z": 0.0}}


class RecordingJev:
    """Scripted Jev: takes the first candidate and keeps what it was offered."""
    model = "scripted"

    def __init__(self):
        self.offered = []

    def choose_action(self, snapshot, options, persona="", history=None):
        self.offered.append(options)
        return options[0]["action"], options[0]["params"]


class MissionBrainTest(unittest.TestCase):
    def test_ten_cycles_with_nothing_around_never_idle_twice_in_a_row(self):
        b = br.Brain(jev=RecordingJev())
        history, chosen = [], []
        for _ in range(10):
            d = b.decide(SNAP(), history=history)
            chosen.append(d.action)
            history.append({"action": d.action, "args": d.params, "ok": True, "changed": True})
        self.assertNotIn("idle", chosen)
        self.assertEqual(chosen, ["move_to"] * 10)

    def test_twelve_fallback_cycles_visit_each_heading_three_times(self):
        jev = RecordingJev()
        b = br.Brain(jev=jev)
        for _ in range(12):
            b.decide(SNAP())
        labels = [options[0]["label"].rsplit(" ", 1)[1] for options in jev.offered]
        self.assertEqual(labels[:4], ["N", "E", "S", "W"])
        self.assertEqual(Counter(labels), {"N": 3, "E": 3, "S": 3, "W": 3})

    def test_the_step_only_advances_when_the_fallback_was_offered(self):
        jev = RecordingJev()
        b = br.Brain(jev=jev)
        with_unit = dict(SNAP(), nearby_units=[{"guid": 10, "entry": 1, "name": "Mob", "type": "unit",
                                                "distance": 4.0, "level": 1, "health_pct": 1.0,
                                                "in_combat": False}])
        b.decide(with_unit)
        self.assertEqual(b._mission_step, 0)
        b.decide(SNAP())
        self.assertEqual(b._mission_step, 1)

    def test_the_brain_passes_its_mission_to_the_generator(self):
        jev = RecordingJev()
        br.Brain(jev=jev, mission="explore").decide(SNAP())
        self.assertTrue(jev.offered[-1][0]["label"].startswith("explore: "))

    def test_from_config_takes_the_mission_setting(self):
        env = {"AGENT_BRAIN": "jev", "JEV_BASE_URL": "http://jev.invalid", "AGENT_MISSION": "make_gold"}
        with mock.patch.dict("os.environ", env, clear=True):
            b = br.Brain.from_config(config.load_config())
        self.assertEqual(b.mission, "make_gold")

    def test_the_mission_list_is_the_one_in_agent_missions(self):
        self.assertEqual(missions.MISSIONS, ("level_to_cap", "explore", "make_gold"))
        self.assertEqual(missions.DEFAULT_MISSION, "level_to_cap")


if __name__ == "__main__":
    unittest.main()
