"""ADR 0020 slice 1 (#390): wowmap ingests the agent's `level` activity event from the audit line,
and an agent row replaces the database row for the same level-up."""
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

# #670: point CALIBRATION_FILE at a temp file before any wowmap import
import _calibration_env  # noqa: F401
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "wowmap"))
import activity  # noqa: E402

V3 = pathlib.Path(__file__).resolve().parents[3] / "agent" / "tests" / "fixtures" / "audit" / "v3.jsonl"


def _v3_record():
    with open(V3, encoding="utf-8") as f:
        return json.loads(f.readline())


def _level_event(character, t, to):
    return {"kind": "level", "t": t, "detail": {"to": to}}


def _db_level(character, t, to):
    return activity.event(character, t, "level", f"Reached level {to}", "db",
                          f"db:{character}:{t:.3f}:level:{to}", {"from": to - 1, "to": to})


class AgentEventsTests(unittest.TestCase):
    def test_v3_line_becomes_one_agent_level_event(self):
        (e,) = activity.agent_events(_v3_record(), "Jev1")
        self.assertEqual(e["character"], "Jev1")
        self.assertEqual(e["kind"], "level")
        self.assertEqual(e["text"], "Reached level 11")
        self.assertEqual(e["source"], "agent")
        self.assertFalse(e["inferred"])
        self.assertEqual(e["t"], 1731000009.5)
        self.assertEqual(e["uid"], "agent:Jev1:level:1731000009.500:11")
        self.assertEqual(e["detail"], {"to": 11})

    def test_bad_elements_are_skipped_and_the_rest_kept(self):
        bad = [
            {"kind": "loot_from_the_future", "t": 1.0, "detail": {"to": 3}},  # unknown kind
            {"kind": "level", "t": 1.0, "detail": {"to": "3"}},               # string
            {"kind": "level", "t": 1.0, "detail": {"to": True}},              # bool is not an int
            {"kind": "level", "t": 1.0},                                      # no detail
            {"kind": ["level"], "t": 1.0, "detail": {"to": 3}},               # unhashable kind
            {"kind": "level", "t": "now", "detail": {"to": 3}},               # no timestamp
            "level",                                                          # not a dict
        ]
        rec = {"events": bad + [_level_event("Jev1", 2.0, 4)]}
        out = activity.agent_events(rec, "Jev1")
        self.assertEqual([e["detail"]["to"] for e in out], [4])

    def test_events_that_are_not_a_list_give_nothing(self):
        for value in ("level", {"kind": "level"}, 7, None):
            with self.subTest(value=value):
                self.assertEqual(activity.agent_events({"events": value}, "Jev1"), [])
        self.assertEqual(activity.agent_events({}, "Jev1"), [])

    def test_fact_key_is_the_level_only(self):
        (e,) = activity.agent_events(_v3_record(), "Jev1")
        self.assertEqual(activity.fact_key(e), ("jev1", "level", 11))
        self.assertEqual(activity.fact_key(_db_level("Jev1", 1.0, 11)), ("jev1", "level", 11))
        self.assertIsNone(activity.fact_key(activity.event("Jev1", 1.0, "money", "x", "db", "u")))


class StoreRulesTests(unittest.TestCase):
    def setUp(self):
        self.store = activity.ActivityStore(":memory:")

    def rows(self, character="Jev1"):
        return self.store.recent(character, 50)

    def agent_level(self, to, t=10.0):
        return activity.agent_events({"events": [_level_event("Jev1", t, to)]}, "Jev1")

    def test_agent_first_then_db_leaves_one_agent_row(self):
        self.store.add(self.agent_level(11))
        self.assertEqual(self.store.add([_db_level("Jev1", 10.5, 11)]), 0)
        self.assertEqual([(r["source"], r["text"]) for r in self.rows()], [("agent", "Reached level 11")])

    def test_db_first_then_agent_leaves_one_agent_row(self):
        self.store.add([_db_level("Jev1", 10.5, 11)])
        self.assertEqual(self.store.add(self.agent_level(11)), 1)
        self.assertEqual([r["source"] for r in self.rows()], ["agent"])

    def test_other_character_level_kind_and_item_are_untouched(self):
        self.store.add(self.agent_level(11))
        others = [_db_level("Thrall", 10.5, 11),                  # another character
                  _db_level("Jev1", 10.5, 12),                    # another level
                  activity.event("Jev1", 10.5, "item", "Got Linen Cloth", "db", "db:1:10.500:item:+1",
                                 {"item": 2589, "count": 1})]     # another kind
        self.assertEqual(self.store.add(others), 3)
        self.assertEqual(sorted((r["source"], r["kind"]) for r in self.rows()),
                         [("agent", "level"), ("db", "item"), ("db", "level")])
        self.assertEqual(len(self.rows("Thrall")), 1)

    def test_a_db_level_for_another_name_case_is_the_same_character(self):
        self.store.add(self.agent_level(11))
        self.assertEqual(self.store.add([_db_level("jev1", 10.5, 11)]), 0)


class TailerTests(unittest.TestCase):
    """AuditTailer reads the audit files: the agent event lands once, with its decision event."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name
        os.makedirs(os.path.join(self.base, "Jev1"))
        # The tailer reads day files from yesterday on, so the file is named for today.
        self.path = os.path.join(self.base, "Jev1", time.strftime("%Y-%m-%d") + ".jsonl")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(V3.read_text(encoding="utf-8"))
        self.store = activity.ActivityStore(":memory:")

    def test_scan_stores_the_decision_and_the_agent_event(self):
        activity.AuditTailer(self.store, self.base).scan_once()
        kinds = sorted(r["kind"] for r in self.store.recent("Jev1", 50))
        self.assertEqual(kinds, ["level", "loot"])

    def test_reading_twice_and_rebuilding_the_tailer_adds_no_row(self):
        tailer = activity.AuditTailer(self.store, self.base)
        tailer.scan_once()
        self.assertEqual(tailer.scan_once(), [])
        self.assertEqual(activity.AuditTailer(self.store, self.base).scan_once(), [])
        self.assertEqual(self.store.count("Jev1"), 2)

    def test_a_malformed_events_field_is_counted_and_the_decision_still_stored(self):
        with open(self.path, "a", encoding="utf-8") as f:
            rec = _v3_record()
            rec["cycle"], rec["ts"], rec["events"] = 23, 1731000011.0, "level"
            f.write(json.dumps(rec) + "\n")
        tailer = activity.AuditTailer(self.store, self.base)
        tailer.scan_once()
        self.assertEqual(tailer.skipped_agent_events, 1)
        self.assertEqual(self.store.count("Jev1"), 3)

    def test_status_reports_the_skipped_count(self):
        act = activity.Activity(self.store, audit_dir=self.base)
        act.audit.scan_once()
        self.assertEqual(act.status()["agent"], {"skipped": 0})


if __name__ == "__main__":
    unittest.main()
