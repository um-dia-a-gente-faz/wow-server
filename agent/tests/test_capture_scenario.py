"""Unit tests for tools.agent_dev.capture_scenario (gh-475): the snippet it prints is valid Python
whose snapshot literal equals the audit record's snapshot, a record without one exits 2, and a
live-shaped record (handle strings, as the brain saw it) yields a scenario that offers the attack
on the idle mob and labels the attacker "attacking you" when run the way the suite runs it.
Reads the synthetic v2 audit fixture and temp files; no network or game state."""

import ast
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agent import candidates as cand
from agent.audit_schema import AuditRecord
from agent.handles import HandleMap
from agent.tests import test_brain_scenarios as bs
from tools.agent_dev import capture_scenario, replay

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "audit", "v2.jsonl")
PLAYER = 1                    # high bits 0: a player, handle "p<n>"
IDLE_MOB = 0xF130 << 48 | 5   # HighGuid Unit: handle "u<n>"
ATTACKER = 0xF130 << 48 | 6   # attacks the player
FIGHTING_MOB = 0xF130 << 48 | 7   # in combat with another player: not ours to take
OTHER_PLAYER = 2


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = capture_scenario.main(argv)
    return code, out.getvalue(), err.getvalue()


def record(cycle):
    return next(r for r in replay.load_records(FIXTURE) if r["cycle"] == cycle)


def entry_of(text):
    """Parse the printed entry as the body of a dict literal, the way it is pasted into SCENARIOS."""
    return ast.parse("{" + text + "}").body[0].value


def scenario_from(text):
    """Evaluate the printed entry as Python does once pasted: `...` stays Ellipsis, nothing else runs."""
    entries = eval("{" + text + "}", {"__builtins__": {}}, {"dict": dict})
    (key, value), = entries.items()
    assert key.startswith("captured_"), key
    return dict(value)


def assert_one_line_exit_2(test, argv):
    """The capture command refuses `argv` with exit 2, empty stdout, one stderr line, no traceback."""
    code, out, err = run(argv)
    test.assertEqual(code, 2)
    test.assertEqual(out, "")
    test.assertEqual(len(err.splitlines()), 1, err)
    test.assertNotIn("Traceback", err)
    return err


def write_lines(*lines):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write("".join(line + "\n" for line in lines))
    return f.name


class CaptureScenarioTest(unittest.TestCase):
    def test_snippet_is_valid_python_and_carries_the_record_snapshot(self):
        code, out, _ = run([FIXTURE, "--cycle", "20"])
        self.assertEqual(code, 0)
        entry = entry_of(out)
        self.assertEqual(ast.literal_eval(entry.keys[0]), "captured_Jev1_20")
        call = entry.values[0]
        self.assertEqual(call.func.id, "dict")
        kw = {k.arg: k.value for k in call.keywords}
        self.assertEqual(set(kw), {"snapshot", "offered", "chosen"})
        self.assertEqual(ast.literal_eval(kw["snapshot"]), record(20)["snapshot"])
        self.assertIs(kw["offered"].value, Ellipsis)
        self.assertIs(kw["chosen"].value, Ellipsis)
        self.assertIn("# brain chose: 'loot' {'guid': 7}", out)

    def test_default_is_the_last_record_and_a_record_without_a_snapshot_exits_2(self):
        code, out, err = run([FIXTURE])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertIn("21", err)

    def test_history_is_carried_when_the_record_has_one(self):
        history = [{"action": "loot", "args": {"guid": 7}, "ok": False, "error": "too far"}]
        path = write_lines(json.dumps(dict(record(20), history=history)))
        self.addCleanup(os.remove, path)
        code, out, _ = run([path])
        self.assertEqual(code, 0)
        kw = {k.arg: k.value for k in entry_of(out).values[0].keywords}
        self.assertEqual(ast.literal_eval(kw["history"]), history)


class LiveShapedRecordTest(unittest.TestCase):
    """A record as AuditLogger writes it: GUID fields are handle strings from HandleMap.encode."""

    def setUp(self):
        self.hm = HandleMap()
        live = bs.snap([bs.unit(IDLE_MOB, 4, level=1),
                        bs.unit(ATTACKER, 3, level=1, target_guid=PLAYER, in_combat=True),
                        bs.unit(FIGHTING_MOB, 5, level=1, target_guid=OTHER_PLAYER, in_combat=True)])
        rec = AuditRecord(agent="Jev1", cycle=7, snapshot=self.hm.encode(live),
                          tool_call={"name": "idle", "args": {}}, valid=True, result={"ok": True, "error": None})
        self.path = write_lines(json.dumps(rec.to_json()))
        self.addCleanup(os.remove, self.path)
        self.idle_h, self.att_h, self.me_h, self.other_h = (
            self.hm.handle_for(g) for g in (IDLE_MOB, ATTACKER, PLAYER, FIGHTING_MOB))

    def test_captured_scenario_offers_the_attack_and_labels_the_attacker(self):
        code, out, err = run([self.path, "--me", self.me_h])
        self.assertEqual(code, 0, err)
        s = scenario_from(out)
        self.assertEqual(s["my_guid"], s["handles"][self.me_h])
        s["offered"], s["chosen"] = bs.offering_attack_on(self.idle_h), bs.first_is("auto_attack", guid=self.att_h)
        self.assertEqual(bs.violations(s), [])
        self.assertNotIn(self.other_h, [c["params"].get("guid") for c in bs.offered_for(s)])
        labels = {c["params"].get("guid"): c["label"] for c in bs.offered_for(s) if c["action"] == "auto_attack"}
        self.assertIn("attacking you", labels[self.att_h])
        self.assertNotIn("attacking you", labels[self.idle_h])

    def test_the_runner_without_the_handle_table_offers_only_idle(self):
        # The bug this guards: a snapshot with handle strings and no handles resolves to nothing.
        with open(self.path, encoding="utf-8") as f:
            snapshot = json.loads(f.read())["snapshot"]
        self.assertEqual([c["action"] for c in cand.generate(snapshot, my_guid=PLAYER)], ["idle"])

    def test_me_the_snapshot_does_not_contain_exits_2_naming_it(self):
        known = {self.idle_h, self.att_h, self.me_h, self.other_h}
        for bad in ("p99", self.me_h.upper()):
            with self.subTest(bad):
                self.assertNotIn(bad, known)
                err = assert_one_line_exit_2(self, [self.path, "--me", bad])
                self.assertIn(bad, err)

    def test_without_me_the_entry_leaves_my_guid_for_the_author(self):
        code, out, _ = run([self.path])
        self.assertEqual(code, 0)
        self.assertIn("my_guid=...", out)


class BadInputTest(unittest.TestCase):
    """Each of these exits 2 with one stderr line and no traceback."""

    def assert_one_line_exit_2(self, argv):
        return assert_one_line_exit_2(self, argv)

    def test_missing_path(self):
        with tempfile.TemporaryDirectory() as d:
            err = self.assert_one_line_exit_2([os.path.join(d, "nope.jsonl")])
        self.assertIn("cannot read", err)

    def test_directory(self):
        with tempfile.TemporaryDirectory() as d:
            err = self.assert_one_line_exit_2([d])
        self.assertIn("cannot read", err)

    def test_line_that_is_not_an_object(self):
        path = write_lines("[1, 2]")
        self.addCleanup(os.remove, path)
        err = self.assert_one_line_exit_2([path])
        self.assertIn("not a JSON object", err)

    def test_tool_call_that_is_not_an_object(self):
        path = write_lines(json.dumps(dict(record(20), tool_call=5)))
        self.addCleanup(os.remove, path)
        err = self.assert_one_line_exit_2([path])
        self.assertIn("tool_call", err)


if __name__ == "__main__":
    unittest.main()
