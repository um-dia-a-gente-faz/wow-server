"""The history path of `candidates.generate` (#550, candidate_history.py) on the busy snapshots
of test_candidate_order: a history that drops and demotes nothing must leave the recorded order
of provider_order.json untouched. The fixture records no history, so this is the only check that
the history maths runs over those snapshots."""

import json
import unittest

from agent import candidates as cand
from agent.tests.test_candidate_order import EXPECTED, cases

# Successful, changed entries: nothing is dropped (no failure) and nothing is demoted (no no-op run).
HISTORY = [{"action": "loot", "args": {"guid": 20}, "ok": True, "changed": True}] * 3


class HistoryOrderTest(unittest.TestCase):
    def test_a_history_that_changes_nothing_keeps_the_recorded_order(self):
        expected = json.loads(EXPECTED.read_text())
        for name, (snap, kw) in cases().items():
            got = [c["id"] for c in cand.generate(snap, my_guid=1, limit=200, history=HISTORY, **kw)]
            self.assertEqual(got, expected[name], name)


if __name__ == "__main__":
    unittest.main()
