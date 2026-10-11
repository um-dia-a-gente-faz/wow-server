"""The history path of `candidates.generate` (#550, candidate_history.py) on the busy snapshots
of test_candidate_order: a history that drops and demotes nothing must leave the recorded order
of provider_order.json untouched. The fixture records no history, so this is the only check that
the history maths runs over those snapshots.

The rest pins three things a reader of the swallowed-error alert relies on (#552): the drop window,
the `candidates.history` counter key and the `agent.candidates` logger name for a malformed entry."""

import json
import logging
import unittest
from unittest import mock

from agent import candidate_history as ch
from agent import candidates as cand
from agent import metrics
from agent.tests.test_candidate_order import EXPECTED, cases
from agent.transport import ERROR_LOG_INTERVAL_S, _ErrorThrottle

# Successful, changed entries: nothing is dropped (no failure) and nothing is demoted (no no-op run).
HISTORY = [{"action": "loot", "args": {"guid": 20}, "ok": True, "changed": True}] * 3


class HistoryOrderTest(unittest.TestCase):
    def test_a_history_that_changes_nothing_keeps_the_recorded_order(self):
        expected = json.loads(EXPECTED.read_text())
        for name, (snap, kw) in cases().items():
            got = [c["id"] for c in cand.generate(snap, my_guid=1, limit=200, history=HISTORY, **kw)]
            self.assertEqual(got, expected[name], name)


class DropWindowTest(unittest.TestCase):
    REJECTED = {"action": "loot", "args": {"guid": 4661}, "ok": False, "error": "bad params: guid"}
    HARMLESS = {"action": "idle", "args": {}, "ok": True}

    def dropped(self, history) -> bool:
        drop, _ = ch.history_effects(history, cand.candidate)
        return "loot:guid=4661" in drop

    def test_a_rejection_drops_only_while_inside_the_window(self):
        # The literal 4 is deliberate: changing HISTORY_DROP_WINDOW changes what the agent offers,
        # so a change is a behaviour change to review and must fail here as well as below.
        self.assertEqual(ch.HISTORY_DROP_WINDOW, 4)
        w = ch.HISTORY_DROP_WINDOW
        self.assertTrue(self.dropped([self.REJECTED] + [self.HARMLESS] * (w - 1)))
        self.assertFalse(self.dropped([self.REJECTED] + [self.HARMLESS] * w))


class MalformedHistoryTest(unittest.TestCase):
    # Mixed-type keys: candidate() sorts the params keys, so the TypeError is raised inside
    # history_effects' try and that entry is swallowed under `candidates.history`.
    BAD = {"action": "loot", "args": {1: 0, "guid": 4661}, "ok": False, "error": "bad params: guid"}
    # One lootable corpse in reach: the provider offers loot:guid=4661 and then idle.
    SNAP = {"me": {"level": 5}, "nearby_units": [{"guid": 4661, "distance": 1.0, "lootable": True, "name": "Corpse"}]}

    def setUp(self):
        # Fresh counter and a fresh throttle, so the assertions do not depend on what ran before.
        for patcher in (mock.patch.dict(metrics.SWALLOWED, clear=True),
                        mock.patch.object(metrics, "_swallowed_throttle", _ErrorThrottle(ERROR_LOG_INTERVAL_S))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_malformed_entry_is_survived_and_counted_under_candidates_history(self):
        with self.assertLogs("agent.candidates", level="WARNING"):   # keeps the traceback out of the output
            ids = [c["id"] for c in cand.generate(self.SNAP, history=[self.BAD])]
        self.assertEqual(ids, ["loot:guid=4661", "idle"])
        self.assertEqual(metrics.SWALLOWED, {"candidates.history": 1})

    def test_a_malformed_entry_logs_one_warning_on_agent_candidates(self):
        with self.assertLogs("agent.candidates", level="WARNING") as cm:
            cand.generate(self.SNAP, history=[self.BAD])
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(cm.records[0].levelno, logging.WARNING)
        self.assertIn("swallowed error in candidates.history (loot)", cm.output[0])


if __name__ == "__main__":
    unittest.main()
