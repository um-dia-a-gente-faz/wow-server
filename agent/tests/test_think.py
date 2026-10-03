"""Unit tests for agent.think.think_and_act: LLM tool call -> registry
validation -> exactly one action executed per cycle, with a mocked LLM
client (agent.llm.LLMClient is never instantiated here)."""

import unittest
from types import SimpleNamespace

from agent import actions as ac
from agent import perception as per
from agent import think
from agent import trade as tr
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

    def choose_action(self, snapshot, catalog, persona="", history=None):
        self.calls.append({"snapshot": snapshot, "catalog": catalog, "persona": persona,
                           "history": history})
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


class IdleTradeTimeoutTest(unittest.TestCase):
    """UM-59: a trade nobody has touched in TRADE_IDLE_TIMEOUT_S gets
    cancelled by code, before the LLM even gets a turn this cycle."""

    def test_maybe_cancel_idle_trade_no_trade_is_a_no_op(self):
        sess = fake_session()
        world = per.WorldState()
        think._maybe_cancel_idle_trade(sess, world)
        self.assertEqual(sess._sent, [])

    def test_maybe_cancel_idle_trade_leaves_a_fresh_trade_alone(self):
        sess = fake_session()
        world = per.WorldState()
        world.start_trade_request(0x5, initiated_by_me=True)
        think._maybe_cancel_idle_trade(sess, world)
        self.assertEqual(sess._sent, [])

    def test_maybe_cancel_idle_trade_cancels_after_timeout(self):
        sess = fake_session()
        world = per.WorldState()
        world.start_trade_request(0x5, initiated_by_me=True)
        world.trade["last_activity_at"] -= think.TRADE_IDLE_TIMEOUT_S + 1
        think._maybe_cancel_idle_trade(sess, world)
        self.assertEqual(sess._sent, [(tr.CMSG_CANCEL_TRADE, tr.build_cancel_trade())])

    def test_think_and_act_cancels_idle_trade_before_the_llm_call(self):
        world = per.WorldState()
        sess = fake_session()
        world.start_trade_request(0x5, initiated_by_me=True)
        world.trade["last_activity_at"] -= think.TRADE_IDLE_TIMEOUT_S + 1
        llm = FakeLLMClient(error=LLMError("no free think this cycle"))
        think_and_act(sess, world, llm)
        self.assertIn((tr.CMSG_CANCEL_TRADE, tr.build_cancel_trade()), sess._sent)


class FailingAction(RecordingAction):
    """check() always rejects, like accept_quest on a gossip that's gone."""

    def check(self, session, world, **params):
        return "nope"


def _world_with_self():
    world = per.WorldState()
    world.set_my_guid(1)
    world.set_my_map(0)
    world.update_object(object_at(1, 0.0, 0.0, 0.0, object_type="player"))
    return world


class ThinkStateHistoryTest(unittest.TestCase):
    """UM-90: recent actions + results go into the next prompt."""

    def test_history_records_args_as_sent_and_reaches_the_next_prompt(self):
        world = _world_with_self()
        sess = fake_session(player_position=(0, 0.0, 0.0, 0.0, 0.0))
        state = think.ThinkState()
        registry = {"test_action": RecordingAction()}
        llm = FakeLLMClient(action_name="test_action", params={"value": 7})

        think_and_act(sess, world, llm, my_position=sess.player_position, registry=registry, state=state)
        think_and_act(sess, world, llm, my_position=sess.player_position, registry=registry, state=state)

        self.assertEqual(llm.calls[0]["history"], [])
        second = llm.calls[1]["history"]
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["action"], "test_action")
        self.assertEqual(second[0]["args"], {"value": 7})
        self.assertTrue(second[0]["ok"])
        # Nothing in the snapshot moved between the two cycles.
        self.assertIs(second[0]["changed"], False)

    def test_failures_are_recorded_with_their_error(self):
        state = think.ThinkState()
        llm = FakeLLMClient(action_name="nonexistent_action", params={})
        think_and_act(fake_session(), _world_with_self(), llm, registry={}, state=state)
        entry = state.for_prompt()[0]
        self.assertFalse(entry["ok"])
        self.assertIn("unknown action", entry["error"])

    def test_llm_failures_are_not_recorded(self):
        state = think.ThinkState()
        llm = FakeLLMClient(error=LLMError("boom"))
        think_and_act(fake_session(), per.WorldState(), llm, state=state)
        self.assertEqual(len(state.history), 0)

    def test_history_is_bounded(self):
        state = think.ThinkState()
        for i in range(20):
            state.record("face", {"guid": i}, True, "fp")
        self.assertEqual(len(state.for_prompt()), think.HISTORY_LEN)
        self.assertEqual(state.for_prompt()[-1]["args"], {"guid": 19})

    def test_long_detail_is_truncated(self):
        state = think.ThinkState()
        state.record("x", {}, True, "fp", detail={"blob": "a" * 1000})
        self.assertLessEqual(len(state.for_prompt()[0]["result"]), think._DETAIL_MAX_CHARS)

    def test_self_status_exposes_known_spells_only(self):
        world = _world_with_self()
        sess = fake_session(player_position=(0, 0.0, 0.0, 0.0, 0.0))
        sess.spellbook = {635, 668}  # Holy Light (in agent.spells' table) + a language passive
        llm = FakeLLMClient(error=LLMError("skip"))
        think_and_act(sess, world, llm, my_position=sess.player_position)
        self.assertEqual(llm.calls[0]["snapshot"]["spells"], [{"id": 635, "name": "Holy Light"}])
        self.assertIn("me", llm.calls[0]["snapshot"])


class LoopGuardTest(unittest.TestCase):
    """UM-90: the same failed/no-op call N times in a row is not executed again."""

    def setUp(self):
        self.world = _world_with_self()
        self.sess = fake_session(player_position=(0, 0.0, 0.0, 0.0, 0.0))
        self.state = think.ThinkState()

    def _cycle(self, action, params, pos=(0, 0.0, 0.0, 0.0, 0.0)):
        llm = FakeLLMClient(action_name="test_action", params=params)
        return think_and_act(self.sess, self.world, llm, my_position=pos,
                             registry={"test_action": action}, state=self.state)

    def test_blocks_fourth_identical_failed_call(self):
        action = FailingAction()
        for _ in range(think.LOOP_GUARD_REPEATS):
            self.assertEqual(self._cycle(action, {"value": 1}).error, "nope")
        result = self._cycle(action, {"value": 1})
        self.assertFalse(result.ok)
        self.assertIn("loop guard", result.error)
        self.assertIn("Try something different", result.error)
        # The block is recorded, so the model sees it next cycle.
        self.assertIn("loop guard", self.state.for_prompt()[-1]["error"])
        # And it stays blocked while the model keeps repeating itself.
        self.assertIn("loop guard", self._cycle(action, {"value": 1}).error)

    def test_blocks_repeated_successful_call_that_changes_nothing(self):
        # Live A3: accept_quest 8325 eight times, each "ok", nothing changed.
        action = RecordingAction()
        for _ in range(think.LOOP_GUARD_REPEATS):
            self.assertTrue(self._cycle(action, {"value": 8325}).ok)
        action.executed_with = None
        result = self._cycle(action, {"value": 8325})
        self.assertIn("loop guard", result.error)
        self.assertIsNone(action.executed_with)

    def test_does_not_block_repeats_that_make_progress(self):
        action = RecordingAction()
        for i in range(think.LOOP_GUARD_REPEATS + 2):
            # Position moves 5 yd each cycle: the action is doing something.
            result = self._cycle(action, {"value": 1}, pos=(0, 5.0 * i, 0.0, 0.0, 0.0))
            self.assertTrue(result.ok, result.error)

    def test_does_not_block_when_args_differ(self):
        action = FailingAction()
        for i in range(think.LOOP_GUARD_REPEATS):
            self._cycle(action, {"value": i})
        self.assertEqual(self._cycle(action, {"value": 99}).error, "nope")

    def test_different_action_breaks_the_streak(self):
        action = FailingAction()
        for _ in range(think.LOOP_GUARD_REPEATS - 1):
            self._cycle(action, {"value": 1})
        self.state.record("face", {"guid": 5}, True, "other")
        self._cycle(action, {"value": 1})
        self.assertEqual(self._cycle(action, {"value": 1}).error, "nope")

    def test_without_state_no_guard(self):
        action = FailingAction()
        llm = FakeLLMClient(action_name="test_action", params={"value": 1})
        for _ in range(5):
            result = think_and_act(self.sess, self.world, llm, registry={"test_action": action})
            self.assertEqual(result.error, "nope")


if __name__ == "__main__":
    unittest.main()
