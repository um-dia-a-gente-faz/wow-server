"""think_and_act stages (#385): the audit record and history of a cycle are
unchanged by the split, and each stage runs without a session."""

import json
import pathlib
import unittest

from agent import actions as ac
from agent import perception as per
from agent import think
from agent.brain import Brain
from agent.llm import LLMError
from agent.tests.builders import FakeSession
from agent.tests.test_think import FakeLLMClient, RecordingAction, _world_with_self


# Recorded from the pre-split think_and_act (origin/main; regenerated after #472 added prompt_id).
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


def _cycle(session=None, world=None, brain=None, registry=None, audit=None, state=None):
    return think.Cycle(session, world if world is not None else per.WorldState(), brain, "", None,
                       registry or {}, audit, 0, None, state)


class _Act(RecordingAction):
    def __init__(self, result=None, exc=None):
        super().__init__()
        self.result, self.exc = result, exc

    def execute(self, session, world, **params):
        if self.exc:
            raise self.exc
        return self.result


class _RaisingAudit:
    def __init__(self, exc):
        self.exc = exc

    def record(self, **kw):
        raise self.exc


class StageTest(unittest.TestCase):
    def test_guards_run_every_hook_with_session_and_world(self):
        calls = []
        think.GUARDS.append(lambda s, w: calls.append((s, w)))
        self.addCleanup(think.GUARDS.pop)
        world = per.WorldState()
        think.guards(_cycle(session="S", world=world))
        self.assertEqual(calls[-1], ("S", world))

    def test_perceive_fingerprints_the_snapshot_after_self_status(self):
        prints = []
        for level in (1, 2):
            world = _world_with_self()
            world.get_my_object().level = level
            c = _cycle(session=FakeSession(), world=world)
            think.perceive(c)
            prints.append(c.fingerprint)
        self.assertNotEqual(prints[0], prints[1])

    def test_perceive_observes_the_fingerprint_in_state(self):
        state = think.ThinkState()
        state.record("a", {}, True, "old")
        c = _cycle(session=FakeSession(), state=state)
        think.perceive(c)
        self.assertTrue(state.history[-1]["changed"])

    def test_decide_stores_the_decision(self):
        c = _cycle(session=FakeSession(), brain=Brain(llm=FakeLLMClient("test_action", {"value": 1})))
        think.decide(c)
        self.assertEqual((c.action_name, c.params), ("test_action", {"value": 1}))
        self.assertFalse(c.done)

    def test_decide_brain_error_ends_the_cycle_without_history(self):
        c = _cycle(session=FakeSession(), brain=Brain(llm=FakeLLMClient(error=LLMError("down"))))
        think.decide(c)
        self.assertTrue(c.done)
        self.assertFalse(c.remember)
        self.assertIn("down", c.error)

    def test_execute_type_error_is_bad_params_but_valid(self):
        c = _cycle()
        c.action, c.action_name, c.params = _Act(exc=TypeError("x")), "test_action", {}
        think.execute(c)
        self.assertTrue(c.valid)
        self.assertTrue(c.error.startswith("bad params:"))

    def test_execute_failure_keeps_detail_on_the_result_not_in_history(self):
        state = think.ThinkState()
        c = _cycle(state=state)
        c.action = _Act(result=ac.ActionResult(ok=False, error="e", detail={"d": 1}))
        c.action_name, c.params = "test_action", {}
        think.execute(c)
        self.assertEqual((c.valid, c.error, c.detail), (True, "e", {"d": 1}))
        r = think.record(c)
        self.assertEqual(r.detail, {"d": 1})
        self.assertIsNone(state.history[-1]["detail"])

    def test_record_survives_an_oserror_from_the_audit_logger(self):
        state = think.ThinkState()
        c = _cycle(audit=_RaisingAudit(OSError("disk")), state=state)
        c.action_name, c.params = "a", {}
        self.assertIsInstance(think.record(c), think.ThinkResult)
        self.assertEqual(len(state.history), 1)

    def test_record_audits_before_history(self):
        state = think.ThinkState()
        c = _cycle(audit=_RaisingAudit(ValueError("bug")), state=state)
        c.action_name, c.params = "a", {}
        with self.assertRaises(ValueError):
            think.record(c)
        self.assertEqual(len(state.history), 0)

    def test_none_params_propagate_as_on_main(self):
        # main raises TypeError out of think_and_act here; production cannot produce it.
        class Needs(RecordingAction):
            required = ("value",)
        c = _cycle(registry={"test_action": Needs()})
        c.action_name, c.params = "test_action", None
        with self.assertRaises(TypeError):
            think.validate(c)
