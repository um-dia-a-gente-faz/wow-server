"""#319: agent.unmet records unmet intents (record shape, bounds, dedup) and the audit
writes them to <agent>/unmet/, never to the decision day file."""

import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from agent import unmet
from agent.audit import AuditLogger
from agent.unmet import DEDUP_S, MAX_RECENT, STATE_MAX_CHARS, UnmetIntents


class FakeClock:
    def __init__(self, now=1700000000.0):
        self.now = now

    def __call__(self):
        return self.now


def snapshot(units=2, quests=1, dead=False):
    return {"position": {"map": 1, "x": 10.04, "y": -3.26, "z": 7.0},
            "is_dead": dead,
            "me": {"level": 12, "class_id": 1, "health": "40/60", "in_combat": True},
            "nearby_units": [{"distance": 3.0}] * units,
            "quest_log": [{"id": i} for i in range(quests)],
            "inventory": [{"huge": "x" * 1000}]}  # not part of the state at all


class RecordShapeTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.intents = UnmetIntents("Luaprata", "explore", clock=self.clock)

    def test_record_has_the_agreed_shape(self):
        rec = self.intents.record("unknown_action", "fly_to_moon", {"speed": 9}, snapshot())
        self.assertEqual(rec, {
            "schema": 1, "ts": self.clock.now, "agent": "Luaprata", "mission": "explore",
            "kind": "unknown_action",
            "wanted": {"action": "fly_to_moon", "params": {"speed": 9}},
            "state": {"level": 12, "position": {"map": 1, "x": 10.0, "y": -3.3, "z": 7.0},
                      "health": "40/60", "dead": False, "in_combat": True,
                      "nearby_units": 2, "quests": 1},
        })
        self.assertEqual(self.intents.recent(), [rec])

    def test_no_candidates_has_no_action_and_no_params(self):
        rec = self.intents.record("no_candidates", None, None, {})
        self.assertEqual(rec["kind"], "no_candidates")
        self.assertEqual(rec["wanted"], {"action": None, "params": {}})
        self.assertEqual(rec["state"], {"level": None, "position": None, "health": None, "dead": False,
                                        "in_combat": False, "nearby_units": 0, "quests": 0})

    def test_long_params_are_replaced_by_a_truncated_preview(self):
        big = {"text": "y" * 1000}
        rec = self.intents.record("unknown_action", "a", big, snapshot())
        self.assertEqual(list(rec["wanted"]["params"]), ["truncated"])
        preview = rec["wanted"]["params"]["truncated"]
        self.assertEqual(preview, json.dumps(big, separators=(",", ":"), sort_keys=True)[:250])
        self.assertEqual(len(preview), 250)

    def test_params_at_the_limit_are_kept_whole(self):
        rec = self.intents.record("unknown_action", "a", {"s": "z" * 200}, snapshot())
        self.assertEqual(rec["wanted"]["params"], {"s": "z" * 200})

    def test_state_stays_bounded_with_500_nearby_units(self):
        rec = self.intents.record("no_candidates", None, None, snapshot(units=500, quests=300))
        self.assertEqual(rec["state"]["nearby_units"], 500)
        self.assertLessEqual(len(json.dumps(rec["state"], sort_keys=True)), STATE_MAX_CHARS)
        self.assertNotIn("inventory", json.dumps(rec))

    def test_long_action_names_are_cut(self):
        rec = self.intents.record("unknown_action", "n" * 500, None, snapshot())
        self.assertEqual(rec["wanted"]["action"], "n" * 64)


class DedupTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.intents = UnmetIntents("Luaprata", "level_to_cap", clock=self.clock)

    def test_same_kind_and_action_within_the_window_is_not_recorded_again(self):
        self.assertIsNotNone(self.intents.record("no_candidates", None, None, {}))
        self.clock.now += DEDUP_S - 0.1
        self.assertIsNone(self.intents.record("no_candidates", None, None, {}))
        self.assertEqual(len(self.intents.recent()), 1)

    def test_recorded_again_once_the_window_has_passed(self):
        self.intents.record("unknown_action", "fly", None, {})
        self.clock.now += DEDUP_S
        self.assertIsNotNone(self.intents.record("unknown_action", "fly", None, {}))
        self.assertEqual(len(self.intents.recent()), 2)

    def test_a_different_action_or_kind_is_not_a_duplicate(self):
        self.intents.record("unknown_action", "fly", None, {})
        self.assertIsNotNone(self.intents.record("unknown_action", "dig", None, {}))
        self.assertIsNotNone(self.intents.record("no_candidates", None, None, {}))
        self.assertEqual(len(self.intents.recent()), 3)

    def test_dedup_memory_does_not_grow_with_every_name_the_model_invents(self):
        for i in range(500):
            self.intents.record("unknown_action", f"made_up_{i}", None, {})
            self.clock.now += DEDUP_S   # each entry ages out before the next one
        self.assertLessEqual(len(self.intents._last), 1)


class RecentTest(unittest.TestCase):
    def test_recent_is_capped_and_keeps_the_newest_oldest_first(self):
        intents = UnmetIntents("Luaprata", "explore", clock=FakeClock())
        for i in range(MAX_RECENT + 10):
            intents.record("unknown_action", f"a{i}", None, {})
        recent = intents.recent()
        self.assertEqual(len(recent), MAX_RECENT)
        self.assertEqual(recent[0]["wanted"]["action"], "a10")
        self.assertEqual(recent[-1]["wanted"]["action"], f"a{MAX_RECENT + 9}")

    def test_empty_when_nothing_was_recorded(self):
        self.assertEqual(UnmetIntents("Luaprata", "explore").recent(), [])


class AuditWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = AuditLogger("Luaprata", base_dir=self.tmp.name, retention_days=14)
        self.audit.on_record = self.fail  # unmet records never reach the decision observer
        self.intents = UnmetIntents("Luaprata", "explore", audit_logger=self.audit, clock=FakeClock())

    def test_line_goes_to_the_unmet_subdirectory_not_the_day_file(self):
        rec = self.intents.record("unknown_action", "fly", None, snapshot())
        day = time.strftime("%Y-%m-%d", time.localtime(rec["ts"]))
        path = os.path.join(self.tmp.name, "Luaprata", "unmet", day + ".jsonl")
        with open(path) as f:
            self.assertEqual([json.loads(line) for line in f], [rec])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "Luaprata", day + ".jsonl")))

    def test_a_deduplicated_record_writes_no_line(self):
        self.intents.record("no_candidates", None, None, {})
        self.intents.record("no_candidates", None, None, {})
        day = time.strftime("%Y-%m-%d", time.localtime(FakeClock().now))
        with open(os.path.join(self.tmp.name, "Luaprata", "unmet", day + ".jsonl")) as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_an_audit_io_error_is_logged_not_raised(self):
        with mock.patch.object(self.audit, "record_unmet_intent", side_effect=OSError("disk full")):
            rec = self.intents.record("no_candidates", None, None, {})
        self.assertIsNotNone(rec)
        self.assertEqual(self.intents.recent(), [rec])

    def test_retention_also_cleans_the_unmet_subdirectory(self):
        directory = os.path.join(self.tmp.name, "Luaprata", "unmet")
        os.makedirs(directory)
        now = time.time()
        old = os.path.join(directory, time.strftime("%Y-%m-%d", time.localtime(now - 30 * 86400)) + ".jsonl")
        with open(old, "w") as f:
            f.write("{}\n")
        self.assertEqual(self.audit.clean_retention(now=now), [old])


class NoNetworkTest(unittest.TestCase):
    def test_module_imports_nothing_network_related(self):
        source = pathlib.Path(unmet.__file__).read_text()
        for word in ("urllib", "http", "socket", "requests", "github"):
            self.assertNotIn(f"import {word}", source)
            self.assertNotIn(f"from {word}", source)


if __name__ == "__main__":
    unittest.main()
