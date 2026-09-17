"""Unit tests for agent.think.think_and_act: LLM tool call -> registry
validation -> exactly one action executed per cycle, with a mocked LLM
client (agent.llm.LLMClient is never instantiated here)."""

import unittest
from types import SimpleNamespace

from agent import actions as ac
from agent import perception as per
from agent import update_object as uo
from agent.llm import LLMError
from agent.think import think_and_act


def fake_session(race=10, player_guid=0x1, player_position=None):
    sent = []
    sess = SimpleNamespace(race=race, player_guid=player_guid, player_position=player_position)
    sess._send_packet = lambda opcode, payload=b'': sent.append((opcode, payload))
    sess._sent = sent
    return sess


def object_at(guid, x, y, z, object_type="unit"):
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT,
        guid=guid,
        object_type=uo.TYPEID_PLAYER if object_type == "player" else uo.TYPEID_UNIT,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": 0.0},
        fields={},
    )


class FakeLLMClient:
    """Records the snapshot/catalog it was called with, and returns a
    pre-programmed (action_name, params) or raises a pre-programmed error."""

    def __init__(self, action_name=None, params=None, error=None):
        self.action_name = action_name
        self.params = params or {}
        self.error = error
        self.calls = []

    def choose_action(self, snapshot, catalog, persona=""):
        self.calls.append({"snapshot": snapshot, "catalog": catalog, "persona": persona})
        if self.error is not None:
            raise self.error
        return self.action_name, self.params


class RecordingAction(ac.Action):
    """A minimal test-only Action so tests don't depend on real game
    actions' preconditions (e.g. set_target's confirm-timeout poll)."""

    name = "test_action"
    description = "records that it ran"
    params = {"value": {"type": "integer"}}
    required = ()

    def __init__(self):
        self.executed_with = None
        self.check_error = None

    def check(self, session, world, **params):
        return self.check_error

    def execute(self, session, world, **params):
        self.executed_with = params
        return ac.ActionResult(ok=True, detail={"ran": True, **params})


class ThinkAndActTest(unittest.TestCase):
    def test_executes_the_chosen_action_exactly_once(self):
        world = per.WorldState()
        world.set_my_guid(1)
        world.update_object(object_at(1, 0.0, 0.0, 0.0, object_type="player"))
        sess = fake_session(player_position=(0, 0.0, 0.0, 0.0, 0.0))

        action = RecordingAction()
        registry = {"test_action": action}
        llm = FakeLLMClient(action_name="test_action", params={"value": 7})

        result = think_and_act(sess, world, llm, my_position=sess.player_position, registry=registry)

        self.assertTrue(result.ok)
        self.assertEqual(result.action_name, "test_action")
        self.assertEqual(action.executed_with, {"value": 7})
        self.assertEqual(len(llm.calls), 1)

    def test_passes_snapshot_and_full_catalog_to_llm(self):
        world = per.WorldState()
        world.set_my_guid(1)
        world.set_my_map(0)
        world.update_object(object_at(1, 0.0, 0.0, 0.0, object_type="player"))
        world.update_object(object_at(5, 3.0, 4.0, 0.0))
        sess = fake_session(player_position=(0, 0.0, 0.0, 0.0, 0.0))

        llm = FakeLLMClient(action_name="set_target", params={"guid": 5})
        think_and_act(sess, world, llm, my_position=sess.player_position)

        call = llm.calls[0]
        self.assertEqual(call["snapshot"]["position"], {"map": 0, "x": 0.0, "y": 0.0, "z": 0.0})
        self.assertEqual(len(call["snapshot"]["nearby_units"]), 1)
        catalog_names = {schema["name"] for schema in call["catalog"]}
        self.assertIn("set_target", catalog_names)
        self.assertIn("face", catalog_names)

    def test_unknown_action_name_is_rejected_without_executing_anything(self):
        world = per.WorldState()
        sess = fake_session()
        registry = {"test_action": RecordingAction()}
        llm = FakeLLMClient(action_name="nonexistent_action", params={})

        result = think_and_act(sess, world, llm, registry=registry)

        self.assertFalse(result.ok)
        self.assertIn("unknown action", result.error)

    def test_missing_required_param_is_rejected_without_executing(self):
        world = per.WorldState()
        world.set_my_guid(1)
        world.update_object(object_at(1, 0.0, 0.0, 0.0, object_type="player"))
        sess = fake_session(player_position=(0, 0.0, 0.0, 0.0, 0.0))

        # set_target requires "guid" — the model didn't supply one.
        llm = FakeLLMClient(action_name="set_target", params={})
        result = think_and_act(sess, world, llm, my_position=sess.player_position)

        self.assertFalse(result.ok)
        self.assertIn("missing required params", result.error)
        self.assertNotIn(ac.CMSG_SET_SELECTION, [op for op, _ in sess._sent])

    def test_check_failure_prevents_execution(self):
        world = per.WorldState()
        sess = fake_session(player_position=(0, 0.0, 0.0, 0.0, 0.0))
        # guid 5 was never perceived -> SetTargetAction.check() rejects it.
        llm = FakeLLMClient(action_name="set_target", params={"guid": 5})

        result = think_and_act(sess, world, llm, my_position=sess.player_position)

        self.assertFalse(result.ok)
        self.assertNotIn(ac.CMSG_SET_SELECTION, [op for op, _ in sess._sent])

    def test_llm_error_yields_non_ok_result_without_raising(self):
        world = per.WorldState()
        sess = fake_session()
        llm = FakeLLMClient(error=LLMError("boom"))

        result = think_and_act(sess, world, llm)

        self.assertFalse(result.ok)
        self.assertIn("llm call failed", result.error)

    def test_hallucinated_extra_param_does_not_crash(self):
        world = per.WorldState()
        sess = fake_session()
        registry = {"test_action": RecordingAction()}
        llm = FakeLLMClient(action_name="test_action", params={"value": 1, "made_up_param": True})

        result = think_and_act(sess, world, llm, registry=registry)

        # RecordingAction.execute(**params) accepts arbitrary kwargs, so this
        # succeeds; the important thing is it never raises TypeError out of
        # think_and_act regardless of which branch it takes.
        self.assertTrue(result.ok)

    def test_action_execute_failure_is_reported_not_raised(self):
        world = per.WorldState()
        sess = fake_session()

        class AlwaysFails(RecordingAction):
            name = "always_fails"

            def execute(self, session, world, **params):
                return ac.ActionResult(ok=False, error="server rejected it")

        registry = {"always_fails": AlwaysFails()}
        llm = FakeLLMClient(action_name="always_fails", params={})

        result = think_and_act(sess, world, llm, registry=registry)

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "server rejected it")


if __name__ == "__main__":
    unittest.main()
