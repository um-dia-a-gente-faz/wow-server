"""Unit tests for agent.tools.cache_probe (#167): the situation key and the
repeat/agreement arithmetic, on the candidate-generator fixtures."""

import copy
import json
import os
import unittest

from agent import candidates as cand
from agent.tools import cache_probe as cp

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "candidates")


def _combat():
    with open(os.path.join(FIX, "combat.json")) as f:
        return json.load(f)


def _key(snap):
    return cp.situation_key(snap, cand.generate(snap, my_guid=1))


class KeyTest(unittest.TestCase):
    def test_bands(self):
        self.assertEqual([cp.band(v) for v in (0.1, 0.4, 0.7, 1.0, None)],
                         ["crit", "low", "mid", "full", "?"])

    def test_position_jitter_and_health_within_band_keep_key(self):
        a = _combat()
        b = copy.deepcopy(a)
        b["position"]["x"] += 0.37
        b["me"]["health"] = "42/90"  # still "low"
        self.assertEqual(_key(a), _key(b))

    def test_new_spawn_of_same_mob_keeps_key(self):
        a = _combat()
        b = copy.deepcopy(a)
        for u in b["nearby_units"]:
            u["guid"] += 1000
        self.assertEqual(_key(a), _key(b))

    def test_health_band_change_changes_key(self):
        a = _combat()
        b = copy.deepcopy(a)
        b["me"]["health"] = "80/90"
        self.assertNotEqual(_key(a), _key(b))

    def test_refusal(self):
        k = _key(_combat())
        self.assertTrue(cp.refused("auto_attack", k))  # which mob to pull: never served
        self.assertFalse(cp.refused("loot", k))
        self.assertTrue(cp.refused("idle", k))  # in combat: resting is not obvious


class AnalyseTest(unittest.TestCase):
    def _rec(self, snap, name, args):
        return {"tool_call": {"name": name, "args": args}, "snapshot": snap}

    def test_repeat_and_agreement(self):
        a = _combat()
        b = copy.deepcopy(a)
        b["position"]["y"] += 0.2
        recs = [self._rec(a, "loot", {"guid": 4661}), self._rec(b, "loot", {"guid": 4661}),
                self._rec(b, "idle", {})]
        s = cp.analyse(recs)
        self.assertEqual(s["keyed_cycles"], 3)
        self.assertEqual(s["repeats"], 2)
        self.assertEqual(s["same_choice"], 1)
        self.assertAlmostEqual(s["agreement"], 0.5)

    def test_records_without_snapshot_or_key_are_skipped(self):
        s = cp.analyse([{"tool_call": {"name": "idle", "args": {}}}])
        self.assertEqual(s["keyed_cycles"], 0)
        self.assertIsNone(s["repeat_rate"])


if __name__ == "__main__":
    unittest.main()
