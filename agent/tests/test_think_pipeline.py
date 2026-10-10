"""think_and_act stages (#385): the audit record and history of a cycle are
unchanged by the split, and each stage runs without a session."""

import json
import pathlib
import unittest

from agent import perception as per
from agent import think
from agent.llm import LLMError
from agent.tests.builders import FakeSession
from agent.tests.test_think import FakeLLMClient, RecordingAction, _world_with_self


# Recorded from the pre-split think_and_act (origin/main at 08a2555).
GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "think_pipeline" / "golden.json"


class RejectingAction(RecordingAction):
    name = "bad"

    def check(self, session, world, **params):
        return "nope"


class RecordingAudit:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)


def run_cycles():
    """Seven cycles (ok, unknown action, brain error, rejected x4 so the last is
    loop-guarded) -> audit kwargs, history and results, JSON-normalised."""
    audit, state = RecordingAudit(), think.ThinkState()
    registry = {"test_action": RecordingAction(), "bad": RejectingAction()}
    results = []
    for i, (name, params, err) in enumerate([
            ("test_action", {"value": 7}, None), ("nope", {"x": 1}, None),
            (None, {}, LLMError("down")), ("bad", {"a": 1}, None),
            ("bad", {"a": 1}, None), ("bad", {"a": 1}, None), ("bad", {"a": 1}, None)]):
        r = think.think_and_act(FakeSession(), _world_with_self(), FakeLLMClient(name, params, err),
                                persona="goal", registry=registry, audit_logger=audit,
                                cycle=i, reflex_state={"r": 1}, state=state)
        results.append((r.ok, r.action_name, r.params, r.error, r.detail))
    return json.loads(json.dumps(
        {"audit": audit.records, "history": list(state.history), "results": results}, default=str))


class PipelineTest(unittest.TestCase):
    def test_cycle_records_match_the_pre_split_golden(self):
        self.assertEqual(run_cycles(), json.loads(GOLDEN.read_text()))

    def test_a_guard_hook_runs_without_editing_think_and_act(self):
        seen = []
        think.GUARDS.append(lambda session, world: seen.append("guard"))
        self.addCleanup(think.GUARDS.pop)
        think.think_and_act(FakeSession(), per.WorldState(), FakeLLMClient(error=LLMError("x")))
        self.assertEqual(seen, ["guard"])

    def test_validate_stage_alone_rejects_an_unknown_action(self):
        c = think.Cycle(None, per.WorldState(), None, "", None, {}, None, 0, None, None)
        c.action_name, c.params = "ghost", {}
        think.validate(c)
        self.assertTrue(c.done)
        self.assertIn("unknown action: 'ghost'", c.error)

    def test_validate_stage_alone_applies_the_loop_guard(self):
        state = think.ThinkState()
        for _ in range(think.LOOP_GUARD_REPEATS):
            state.record("test_action", {}, False, "fp", error="x")
        c = think.Cycle(None, per.WorldState(), None, "", None, {"test_action": RecordingAction()},
                        None, 0, None, state)
        c.action_name, c.params = "test_action", {}
        think.validate(c)
        self.assertTrue(c.error.startswith("loop guard:"))

    def test_record_stage_alone_skips_history_when_remember_is_off(self):
        state = think.ThinkState()
        c = think.Cycle(None, per.WorldState(), None, "", None, {}, None, 0, None, state)
        c.remember = False
        c.fail("boom")
        self.assertFalse(think.record(c).ok)
        self.assertEqual(len(state.history), 0)

    def test_think_has_no_private_send_left(self):
        self.assertNotIn("_send_packet", pathlib.Path(think.__file__).read_text())
