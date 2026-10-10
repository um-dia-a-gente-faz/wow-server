"""agent.deciders (#278): ScriptedDecider replays a fixed script or calls a rule,
with no network, and is accepted by Brain in both the jev and the llm slot."""

import inspect
import unittest
from unittest import mock

from agent import brain
from agent.deciders import ScriptedDecider
from agent.jev import JevClient, JevError
from agent.llm import LLMClient

CANDIDATES = [
    {"id": "idle", "action": "idle", "params": {}},
    {"id": "test_action(value=2)", "action": "test_action", "params": {"value": 2}},
]


class ScriptedDeciderTest(unittest.TestCase):
    def test_replays_the_script_in_order(self):
        d = ScriptedDecider([("a", {"x": 1}), ("b", {})])
        self.assertEqual(d.choose_action({}, CANDIDATES), ("a", {"x": 1}))
        self.assertEqual(d.choose_action({}, CANDIDATES), ("b", {}))

    def test_repeats_the_last_entry_once_exhausted(self):
        d = ScriptedDecider([("a", {}), ("b", {"n": 2})])
        for _ in range(2):
            d.choose_action({}, CANDIDATES)
        self.assertEqual(d.choose_action({}, CANDIDATES), ("b", {"n": 2}))
        self.assertEqual(d.choose_action({}, CANDIDATES), ("b", {"n": 2}))

    def test_callable_form_receives_snapshot_and_options(self):
        seen = []

        def rule(snapshot, options):
            seen.append((snapshot, options))
            return "test_action", {"value": len(options)}

        d = ScriptedDecider(rule)
        self.assertEqual(d.choose_action({"hp": 5}, CANDIDATES), ("test_action", {"value": 2}))
        self.assertEqual(seen, [({"hp": 5}, CANDIDATES)])

    def test_empty_list_with_fail_when_empty_raises_jev_error(self):
        d = ScriptedDecider([], fail_when_empty=True)
        with self.assertRaises(JevError):
            d.choose_action({}, CANDIDATES)

    def test_empty_list_without_fail_flag_chooses_idle(self):
        self.assertEqual(ScriptedDecider([]).choose_action({}, CANDIDATES), ("idle", {}))

    def test_accepts_both_call_shapes_brain_uses(self):
        # Jev slot: candidates + persona + history kwarg. LLM slot: catalog,
        # and no history kwarg at all when history is None.
        d = ScriptedDecider([("a", {})])
        self.assertEqual(d.choose_action({}, CANDIDATES, persona="p", history=[{"action": "x"}]),
                         ("a", {}))
        self.assertEqual(d.choose_action({}, CANDIDATES, persona="p"), ("a", {}))

    def test_readouts_match_what_brain_reads(self):
        d = ScriptedDecider([("a", {})])
        d.choose_action({}, CANDIDATES)
        self.assertEqual(d.last_usage, {})
        self.assertEqual(d.last_latency_ms, 0.0)
        self.assertEqual(d.last_confidence, 1.0)
        self.assertEqual(d.model, "scripted")

    def test_call_signature_is_compatible_with_the_real_clients(self):
        # The call shapes Brain.decide() makes must bind on every decider.
        shapes = ({"persona": "p", "history": None}, {"persona": "p"}, {})
        for method in (JevClient.choose_action, LLMClient.choose_action,
                       ScriptedDecider.choose_action):
            for shape in shapes:
                inspect.signature(method).bind(None, {}, CANDIDATES, **shape)


class BrainWithScriptedDeciderTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("agent.brain.cand.generate",
                             side_effect=lambda *a, **k: [dict(c) for c in CANDIDATES])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_jev_slot_decides_through_the_scripted_decider(self):
        b = brain.Brain(jev=ScriptedDecider([("test_action", {"value": 2})]))
        d = b.decide({})
        self.assertEqual((d.brain, d.action, d.params), (brain.BRAIN_JEV, "test_action", {"value": 2}))
        self.assertEqual(d.jev_status, "success")
        self.assertEqual(d.confidence, 1.0)

    def test_llm_slot_decides_through_the_scripted_decider(self):
        b = brain.Brain(llm=ScriptedDecider([("test_action", {"value": 7})]))
        d = b.decide({}, persona="")
        self.assertEqual((d.brain, d.action, d.params), (brain.BRAIN_LLM, "test_action", {"value": 7}))
        self.assertEqual(d.model, "scripted")


if __name__ == "__main__":
    unittest.main()
