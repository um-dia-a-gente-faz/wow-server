"""#316: MissionState, the mission in force and its automatic transitions (pure, no packets)."""
import unittest

from agent.mission_state import EXPLORE, LEVEL_TO_CAP, MissionState

CAP = 80


def state(base=LEVEL_TO_CAP, n=5, cap=CAP):
    return MissionState(base, n, cap)


class LevelToCapTest(unittest.TestCase):
    def test_at_the_cap_level_to_cap_goes_to_explore(self):
        m = state()
        self.assertIsNone(m.observe(CAP - 1, 1))
        self.assertEqual(m.observe(CAP, 1), EXPLORE)
        self.assertEqual(m.current, EXPLORE)

    def test_above_the_cap_also_goes_to_explore(self):
        self.assertEqual(state().observe(CAP + 3, None), EXPLORE)

    def test_the_switch_resets_the_new_zone_count(self):
        m = state(n=2)
        m.observe(10, 1)                 # level_to_cap: zone 1 counts toward nothing yet
        m.observe(CAP, 2)                # switch to explore here
        self.assertEqual(m.new_since_switch, 0)


class ExploreTest(unittest.TestCase):
    def test_below_the_cap_explore_returns_after_n_new_zones(self):
        m = state(base=EXPLORE, n=3)
        self.assertIsNone(m.observe(20, 1))
        self.assertIsNone(m.observe(20, 2))
        self.assertEqual(m.observe(20, 3), LEVEL_TO_CAP)
        self.assertEqual(m.current, LEVEL_TO_CAP)
        self.assertEqual(m.new_since_switch, 0)

    def test_at_the_cap_explore_stays_explore_however_many_zones(self):
        m = state(base=EXPLORE, n=2)
        self.assertIsNone(m.observe(CAP, 1))
        self.assertIsNone(m.observe(CAP, 2))
        self.assertIsNone(m.observe(CAP, 3))
        self.assertEqual(m.current, EXPLORE)

    def test_a_revisited_zone_does_not_count(self):
        m = state(base=EXPLORE, n=2)
        m.observe(20, 1)
        for _ in range(5):
            self.assertIsNone(m.observe(20, 1))   # the same zone again and again
        self.assertEqual(m.new_since_switch, 1)
        self.assertEqual(m.current, EXPLORE)

    def test_the_count_restarts_at_each_switch(self):
        m = state(base=EXPLORE, n=2)
        m.observe(20, 1)
        m.observe(20, 2)                 # back to level_to_cap here
        self.assertEqual(m.current, LEVEL_TO_CAP)
        self.assertIsNone(m.observe(20, 3))   # level_to_cap still notices new zones, from zero
        self.assertEqual(m.new_since_switch, 1)

    def test_n_is_configurable(self):
        for n in (1, 4):
            m = state(base=EXPLORE, n=n)
            fired = [m.observe(20, z) for z in range(1, n + 1)]
            self.assertEqual(fired[-1], LEVEL_TO_CAP, n)
            self.assertEqual(fired[:-1], [None] * (n - 1), n)

    def test_an_unknown_level_or_zone_never_moves_the_mission(self):
        m = state(base=EXPLORE, n=1)
        self.assertIsNone(m.observe(None, 1))   # no level: no transition, even with a new zone counted
        self.assertEqual(m.current, EXPLORE)
        m = state(base=EXPLORE, n=1)
        self.assertIsNone(m.observe(20, None))  # no zone: nothing counted, nothing fires
        self.assertEqual(m.current, EXPLORE)


class MakeGoldTest(unittest.TestCase):
    def test_make_gold_never_moves(self):
        m = state(base="make_gold", n=1)
        for level, zone in ((1, 1), (CAP, 2), (CAP + 9, 3), (20, 4)):
            self.assertIsNone(m.observe(level, zone))
        self.assertEqual(m.current, "make_gold")


class OwnerPinTest(unittest.TestCase):
    def test_set_by_owner_sets_and_pins_so_transitions_stop(self):
        m = state(n=1)
        m.set_by_owner("make_gold")
        self.assertEqual(m.current, "make_gold")
        m.set_by_owner(EXPLORE)
        self.assertIsNone(m.observe(CAP, 1))   # pinned: no transition
        self.assertIsNone(m.observe(20, 2))
        self.assertIsNone(m.observe(20, 3))
        self.assertEqual(m.current, EXPLORE)

    def test_pinned_level_to_cap_does_not_switch_at_the_cap(self):
        m = state()
        m.set_by_owner(LEVEL_TO_CAP)
        self.assertIsNone(m.observe(CAP, 1))
        self.assertEqual(m.current, LEVEL_TO_CAP)

    def test_unpin_resumes_the_automatic_transitions(self):
        m = state()
        m.set_by_owner(LEVEL_TO_CAP)
        m.observe(CAP, 1)
        m.unpin()
        self.assertEqual(m.observe(CAP, 2), EXPLORE)

    def test_set_by_owner_rejects_an_unknown_mission(self):
        with self.assertRaises(ValueError):
            state().set_by_owner("fishing")


if __name__ == "__main__":
    unittest.main()
