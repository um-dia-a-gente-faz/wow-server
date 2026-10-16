"""#319: the two unmet-intent triggers in agent.think, end to end through think_and_act:
an action the brain names that does not exist, and a Jev cycle whose candidates were idle only."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from agent import actions as ac
from agent import brain
from agent.audit import AuditLogger
from agent.think import think_and_act
from agent.tests.test_think import FakeJevClient, FakeLLMClient, RecordingAction, _world_with_self, fake_session
from agent.unmet import UnmetIntents

TWO_CANDIDATES = [
    {"id": "test_action:value=1", "label": "one", "action": "test_action", "params": {"value": 1}},
    {"id": "idle", "label": "do nothing", "action": "idle", "params": {}},
]


class UnmetTriggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = AuditLogger("Luaprata", base_dir=self.tmp.name)
        self.unmet = UnmetIntents("Luaprata", "explore", audit_logger=self.audit)
        self.world = _world_with_self()
        self.sess = fake_session(player_position=(0, 0.0, 0.0, 0.0, 0.0))
        self.action = RecordingAction()
        self.registry = {"test_action": self.action, "idle": ac.REGISTRY["idle"]}
        patcher = mock.patch("agent.brain.cand.generate", side_effect=lambda *a, **k: [dict(c) for c in TWO_CANDIDATES])
        self.generate = patcher.start()
        self.addCleanup(patcher.stop)

    def _think(self, b):
        return think_and_act(self.sess, self.world, b, my_position=self.sess.player_position,
                             registry=self.registry, audit_logger=self.audit, cycle=1, unmet=self.unmet)

    def _day_lines(self):
        with open(os.path.join(self.tmp.name, "Luaprata", time.strftime("%Y-%m-%d") + ".jsonl")) as f:
            return [json.loads(line) for line in f]

    def _unmet_lines(self):
        path = os.path.join(self.tmp.name, "Luaprata", "unmet")
        if not os.path.isdir(path):
            return []
        lines = []
        for name in os.listdir(path):
            with open(os.path.join(path, name)) as f:
                lines += [json.loads(line) for line in f]
        return lines

    def test_a_brain_naming_a_missing_action_writes_one_unknown_action_record(self):
        result = self._think(FakeLLMClient("fly_to_moon", {"speed": 9}))

        self.assertFalse(result.ok)
        recs = self.unmet.recent()
        self.assertEqual([r["kind"] for r in recs], ["unknown_action"])
        self.assertEqual(recs[0]["wanted"], {"action": "fly_to_moon", "params": {"speed": 9}})
        self.assertEqual(self._unmet_lines(), recs)
        # the decision day file gets its one line for the cycle, and no unmet record in it
        self.assertEqual([r["cycle"] for r in self._day_lines()], [1])
        self.assertNotIn("kind", self._day_lines()[0])

    def test_a_jev_cycle_with_only_idle_writes_one_no_candidates_record(self):
        self.generate.side_effect = lambda *a, **k: [{"id": "idle", "action": "idle", "params": {}}]
        result = self._think(brain.Brain(jev=FakeJevClient()))

        self.assertTrue(result.ok, result.error)
        self.assertEqual([r["kind"] for r in self.unmet.recent()], ["no_candidates"])
        self.assertEqual(self.unmet.recent()[0]["wanted"], {"action": None, "params": {}})
        self.assertEqual(len(self._unmet_lines()), 1)

    def test_repeated_idle_only_cycles_do_not_write_every_time(self):
        self.generate.side_effect = lambda *a, **k: [{"id": "idle", "action": "idle", "params": {}}]
        b = brain.Brain(jev=FakeJevClient())
        for _ in range(5):   # a 3 s think loop, all inside the dedup window
            self._think(b)
        self.assertEqual(len(self.unmet.recent()), 1)
        self.assertEqual(len(self._unmet_lines()), 1)

    def test_a_cycle_with_a_real_choice_is_not_an_unmet_intent(self):
        jev = FakeJevClient(pick=0)
        self._think(brain.Brain(jev=jev))
        self.assertEqual(self.unmet.recent(), [])

    def test_a_known_action_is_not_an_unmet_intent(self):
        self._think(FakeLLMClient("idle", {}))
        self.assertEqual(self.unmet.recent(), [])

    def test_think_and_act_without_unmet_still_works(self):
        result = think_and_act(self.sess, self.world, FakeLLMClient("fly_to_moon", {}),
                               registry=self.registry)
        self.assertFalse(result.ok)
        self.assertIn("unknown action", result.error)


if __name__ == "__main__":
    unittest.main()
