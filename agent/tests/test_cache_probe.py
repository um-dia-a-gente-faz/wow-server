"""Unit tests for tools.agent_dev.cache_probe (#167): the situation key and the
repeat/agreement arithmetic, on the candidate-generator fixtures."""

import copy
import json
import os
import tempfile
import unittest

from agent import candidates as cand
from tools.agent_dev import cache_probe as cp
from tools.agent_dev.replay import load_records

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


class PathsTest(unittest.TestCase):
    """#772: a directory argument reads only the per-agent day files, never the
    lifecycle/ and unmet/ subdirectories (docs/adr/0018-persisted-state-versioning.md)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = tmp.name
        self.agent = os.path.join(self.base, "jev")
        for sub in ("", "lifecycle", "unmet"):
            os.makedirs(os.path.join(self.agent, sub))
        self.day = self._write("", "2026-10-18.jsonl", 1)
        self._write("lifecycle", "2026-10-18.jsonl", 2)
        self._write("unmet", "2026-10-18.jsonl", 3)
        # a fresh agent: lifecycle and unmet lines, no day file yet
        self.fresh = os.path.join(self.base, "fresh")
        for sub in ("lifecycle", "unmet"):
            os.makedirs(os.path.join(self.fresh, sub))
        for sub, cycle in (("lifecycle", 4), ("unmet", 5)):
            with open(os.path.join(self.fresh, sub, "2026-10-18.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({"cycle": cycle, "agent": "fresh"}) + "\n")

    def _write(self, sub, name, cycle):
        path = os.path.join(self.agent, sub, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"cycle": cycle, "agent": "jev"}) + "\n")
        return path

    def _cycles(self, arg):
        return sorted(r["cycle"] for p in cp._paths([arg]) for r in load_records(p))

    def test_agent_dir_reads_only_day_files(self):
        self.assertEqual(self._cycles(self.agent), [1])

    def test_base_dir_reads_only_day_files(self):
        self.assertEqual(self._cycles(self.base), [1])

    def test_agent_dir_without_day_files_reads_nothing(self):
        self.assertEqual(self._cycles(self.fresh), [])

    def test_file_argument_is_read_as_given(self):
        self.assertEqual(self._cycles(self.day), [1])
        self.assertEqual(self._cycles(os.path.join(self.agent, "unmet", "2026-10-18.jsonl")), [3])


if __name__ == "__main__":
    unittest.main()
