"""Unit tests for tools.agent_dev.capture_scenario (gh-475): the snippet it prints is valid Python
whose snapshot literal equals the audit record's snapshot, and a record without one exits 2.
Reads the synthetic v2 audit fixture; no network or game state."""

import ast
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tools.agent_dev import capture_scenario, replay

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "audit", "v2.jsonl")


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
        rec = dict(record(20), history=history)
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(rec) + "\n")
        self.addCleanup(os.remove, f.name)
        code, out, _ = run([f.name])
        self.assertEqual(code, 0)
        kw = {k.arg: k.value for k in entry_of(out).values[0].keywords}
        self.assertEqual(ast.literal_eval(kw["history"]), history)


if __name__ == "__main__":
    unittest.main()
