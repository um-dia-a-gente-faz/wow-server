"""Golden and writer-contract tests for the versioned audit record (#277)."""

import contextlib
import glob
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import fields

from agent import metrics
from agent.audit import AuditLogger
from agent.audit_schema import SCHEMA_VERSION, AuditRecord
from tools.agent_dev import replay

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "audit")
FIELD_NAMES = {f.name for f in fields(AuditRecord)} - {"extra"}


def _lines(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class GoldenUpgradeTests(unittest.TestCase):
    def test_v0_record_without_schema_loads_with_defaults(self):
        old, _ = _lines("v0.jsonl")
        rec = AuditRecord.from_json(old)
        self.assertEqual((rec.agent, rec.cycle, rec.valid), ("Old", 7, True))
        self.assertIsNone(rec.brain)
        self.assertIsNone(rec.jev_status)
        self.assertIsNone(rec.history_notes)
        self.assertIsNone(rec.snapshot)
        self.assertEqual(rec.events, [])
        self.assertNotIn("events", rec.to_json())
        self.assertEqual(rec.to_json()["schema"], SCHEMA_VERSION)

    def test_v0_unknown_keys_are_kept_in_extra_and_written_back(self):
        _, odd = _lines("v0.jsonl")
        rec = AuditRecord.from_json(odd)
        self.assertEqual(rec.extra, {"legacy_field": "kept"})
        out = rec.to_json()
        self.assertEqual(out["legacy_field"], "kept")
        self.assertNotIn("extra", out)

    def test_v1_lines_upgrade_to_the_current_schema_with_no_prompt_id(self):
        for line in _lines("v1.jsonl"):
            rec = AuditRecord.from_json(line)
            up = rec.to_json()
            self.assertEqual(up["schema"], SCHEMA_VERSION)
            self.assertIsNone(up["prompt_id"])
            self.assertEqual(rec.events, [])
            self.assertNotIn("events", up)
            self.assertEqual({k: v for k, v in up.items() if k not in ("schema", "prompt_id")},
                             {k: v for k, v in line.items() if k != "schema"})

    def test_v2_lines_load_with_no_events_and_keep_every_other_field(self):
        lines = _lines("v2.jsonl")
        self.assertEqual(lines[0]["prompt_id"], "system@1a2b3c4d+goal.level_up@5e6f7a8b")
        for line in lines:
            rec = AuditRecord.from_json(line)
            self.assertEqual(rec.events, [])
            up = rec.to_json()
            self.assertNotIn("events", up)
            self.assertEqual({k: v for k, v in up.items() if k != "schema"},
                             {k: v for k, v in line.items() if k != "schema"})

    def test_v3_lines_round_trip_exactly_with_their_events(self):
        (line,) = _lines("v3.jsonl")
        self.assertEqual(AuditRecord.from_json(line).events,
                         [{"kind": "level", "t": 1731000009.5, "detail": {"to": 11}}])
        self.assertEqual(AuditRecord.from_json(line).to_json(), line)

    def test_v3_quest_line_round_trips_exactly_with_its_event(self):
        (line,) = _lines("v3-quest.jsonl")
        self.assertEqual(AuditRecord.from_json(line).events,
                         [{"kind": "quest", "t": 1731000011.75, "detail": {"quest_id": 8325}}])
        self.assertEqual(AuditRecord.from_json(line).to_json(), line)

    def test_v3_kill_and_death_lines_round_trip_exactly_with_their_events(self):
        (kill,) = _lines("v3-kill.jsonl")
        self.assertEqual(AuditRecord.from_json(kill).events,
                         [{"kind": "kill", "t": 1731000013.25,
                           "detail": {"victim_guid": 17379390982725828761, "victim_entry": 1234}}])
        self.assertEqual(AuditRecord.from_json(kill).to_json(), kill)
        (death,) = _lines("v3-death.jsonl")
        self.assertEqual(AuditRecord.from_json(death).events,
                         [{"kind": "death", "t": 1731000015.5, "detail": {"killer_guess": 17379390963314589701}}])
        self.assertEqual(AuditRecord.from_json(death).to_json(), death)

    def test_to_json_puts_schema_first(self):
        out = AuditRecord(ts=1.0, agent="a", cycle=1, snapshot_hash="h").to_json()
        self.assertEqual(next(iter(out)), "schema")


def _write_tmp(testcase, lines):
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    path = os.path.join(tmp.name, "2024-11-07.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


class SchemalessReaderTests(unittest.TestCase):
    def test_replay_from_skips_a_line_without_cycle_as_cycle_zero(self):
        path = _write_tmp(self, [
            {"ts": 1700000000.0, "agent": "Old", "valid": True, "result": {"ok": True}},
            {"ts": 1700000001.0, "agent": "Old", "cycle": 9, "valid": True, "result": {"ok": True}},
        ])
        out = io.StringIO()
        with redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(replay.main([path, "--from", "5"]), 0)
        shown = out.getvalue().splitlines()
        self.assertEqual(len(shown), 1)
        self.assertIn("cycle=9", shown[0])

    def test_replay_formats_a_schemaless_line_without_agent_as_question_mark(self):
        path = _write_tmp(self, [{"ts": 1700000000.0, "cycle": 3, "valid": True,
                                  "result": {"ok": True}}])
        (rec,) = list(replay.load_records(path))
        self.assertRegex(replay.format_record(rec), r"cycle=3\s+\?\s+OK")

    def test_metrics_lands_a_schemaless_line_without_agent_under_unknown(self):
        path = _write_tmp(self, [{"ts": 1700000000.0, "cycle": 3, "valid": True,
                                  "result": {"ok": True}}])
        by_agent = metrics.derive_metrics(metrics.iter_records(path))
        self.assertEqual(set(by_agent), {"unknown"})
        self.assertEqual(by_agent["unknown"].cycles_total, 1)


class WriterContractTests(unittest.TestCase):
    def test_writer_emits_exactly_the_schema_fields_when_all_populated(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditLogger("Jev1", base_dir=tmp)
            audit.record(
                cycle=20, snapshot={"position": None}, tool_call={"name": "loot", "args": {}},
                valid=True, result={"ok": True, "error": None}, reflex={"follow": True},
                goal="gather", prompt_tokens=1, completion_tokens=2, model="m",
                latency_ms=3.0, brain="jev", confidence=0.9, fallback=None, candidates=4,
                confidence_threshold=0.5, confidence_rule="acted", overridden=None,
                usage={"input_tokens": 1, "output_tokens": 2, "cost": 0.1},
                jev_status="success", history_notes=[{"id": "x"}], substituted=False,
                prompt_id="system@abcd1234",
                events=[{"kind": "level", "t": 1731000000.0, "detail": {"to": 2}}],
                ts=1731000000.0)
            (path,) = glob.glob(os.path.join(tmp, "Jev1", "*.jsonl"))
            with open(path, encoding="utf-8") as f:
                line = f.readline()
        self.assertEqual(json.loads(line)["prompt_id"], "system@abcd1234")
        self.assertEqual(json.loads(line)["events"][0]["kind"], "level")
        keys = set(json.loads(line))
        self.assertLessEqual(keys, FIELD_NAMES | {"schema"})
        self.assertEqual(keys, FIELD_NAMES | {"schema"})
        self.assertTrue(line.startswith('{"schema": %d,' % SCHEMA_VERSION))


if __name__ == "__main__":
    unittest.main()
