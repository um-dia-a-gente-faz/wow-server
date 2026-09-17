"""Unit tests for agent.tools.replay: reads a synthetic audit-log fixture
(agent/tests/fixtures/audit/luaprata_sample.jsonl) and checks the timeline
it prints, plus --from and --failures filtering. No network/game state."""

import io
import os
import unittest
from contextlib import redirect_stdout

from agent.tools import replay

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "audit", "luaprata_sample.jsonl")


class LoadRecordsTest(unittest.TestCase):
    def test_loads_all_records_from_fixture(self):
        recs = list(replay.load_records(FIXTURE))
        self.assertEqual(len(recs), 5)
        self.assertEqual(recs[0]["cycle"], 1)
        self.assertEqual(recs[-1]["cycle"], 5)

    def test_skips_malformed_lines(self, ):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write('{"cycle": 1}\n')
            f.write("not json\n")
            f.write('{"cycle": 2}\n')
            path = f.name
        self.addCleanup(os.remove, path)
        recs = list(replay.load_records(path))
        self.assertEqual([r["cycle"] for r in recs], [1, 2])


class IsFailureTest(unittest.TestCase):
    def test_invalid_is_a_failure(self):
        self.assertTrue(replay.is_failure({"valid": False, "result": {"ok": True}}))

    def test_failed_result_is_a_failure(self):
        self.assertTrue(replay.is_failure({"valid": True, "result": {"ok": False}}))

    def test_valid_and_ok_is_not_a_failure(self):
        self.assertFalse(replay.is_failure({"valid": True, "result": {"ok": True}}))


class MainCliTest(unittest.TestCase):
    def test_prints_all_cycles_by_default(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            replay.main([FIXTURE])
        out = buf.getvalue()
        for cycle in range(1, 6):
            self.assertIn(f"cycle={cycle}", out)

    def test_from_cycle_filters_earlier_cycles(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            replay.main([FIXTURE, "--from", "4"])
        out = buf.getvalue()
        self.assertNotIn("cycle=1 ", out)
        self.assertNotIn("cycle=2 ", out)
        self.assertIn("cycle=4", out)
        self.assertIn("cycle=5", out)

    def test_failures_only_shows_invalid_or_failed_cycles(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            replay.main([FIXTURE, "--failures"])
        out = buf.getvalue()
        self.assertIn("cycle=3", out)   # invalid tool call
        self.assertIn("cycle=4", out)   # failed execution
        self.assertNotIn("cycle=1 ", out)
        self.assertNotIn("cycle=2 ", out)
        self.assertNotIn("cycle=5 ", out)

    def test_full_snapshot_marker_shown_when_present(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            replay.main([FIXTURE])
        out = buf.getvalue()
        lines = out.splitlines()
        cycle3_line = next(l for l in lines if "cycle=3" in l)
        self.assertIn("full snapshot attached", cycle3_line)


if __name__ == "__main__":
    unittest.main()
