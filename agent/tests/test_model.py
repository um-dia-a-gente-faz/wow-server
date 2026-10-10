"""agent.model (#288) describes the dicts the code really produces: every key a
producer emits is declared, and every required key is emitted. Without this the
TypedDicts could drift from the snapshot and mypy would check a fiction."""

import ast
import json
import pathlib
import typing
import unittest
from unittest import mock

from agent import candidates as cand
from agent import model
from agent import think
from agent import update_fields as uf
from agent.tests import test_perception_golden as golden
from agent.tests.test_think import FakeLLMClient, fake_session


class ModelMatchesProducersTest(unittest.TestCase):
    def check(self, value, typed_dict):
        keys, required = set(value), typed_dict.__required_keys__
        self.assertLessEqual(required, keys, f"{typed_dict.__name__}: required key not produced")
        self.assertLessEqual(keys, required | typed_dict.__optional_keys__,
                             f"{typed_dict.__name__}: produced key not declared")

    def test_golden_snapshot_matches_the_model(self):
        snap = json.loads(golden.GOLDEN.read_text())
        # WorldState.snapshot() emits every key that is not added later by think.
        self.assertEqual(set(snap), set(model.Snapshot.__required_keys__))
        for position in (snap["position"], snap["corpse_position"]):
            self.check(position, model.Position)
        for bucket in ("nearby_units", "nearby_players", "nearby_objects"):
            self.assertTrue(snap[bucket])
            for unit in snap[bucket]:
                self.check(unit, model.Unit)
        for item in [*snap["equipment"].values(), *snap["inventory"]]:
            self.check(item, model.Item)
        self.assertTrue(snap["quest_log"])
        for quest in snap["quest_log"]:
            self.check(quest, model.QuestEntry)

    def test_think_snapshot_candidates_and_history_match_the_model(self):
        with mock.patch("time.monotonic", return_value=100.0):
            world = golden.build_world()
        me = world.get_my_object()
        me.level, me.health, me.max_health = 5, 40, 50
        me.power, me.max_power = {name: 1 for name in uf.POWER_NAMES}, {"mana": 10}
        me.raw_fields.update({uf.PLAYER_XP: 1, uf.PLAYER_NEXT_LEVEL_XP: 2})
        session = fake_session()
        session.spellbook = {635}  # Holy Light, known to agent.spells
        llm = FakeLLMClient("no_such_action")
        state = think.ThinkState()
        for _ in range(2):  # the second cycle settles `changed` on the first
            think.think_and_act(session, world, llm, state=state)

        snap = llm.calls[-1]["snapshot"]
        self.check(snap, model.Snapshot)
        self.assertEqual(set(snap["me"]), model.Me.__required_keys__ | model.Me.__optional_keys__)
        self.check(snap["spells"][0], model.KnownSpell)
        for option in cand.generate(snap, my_guid=world.my_guid, handles=world.handles):
            self.check(option, model.Candidate)
        for record in state.for_prompt():
            self.check(record, model.ActionRecord)
        self.assertEqual({k for r in state.for_prompt() for k in r},
                         {"action", "args", "ok", "error", "changed"})


# Keys of the dicts agent.model still leaves as plain `dict` (the open window, an
# item template, the reflex state, a pending invite). Shrinks as those get types.
UNTYPED_KEYS = {
    "kind", "npc_guid", "quests", "options", "coded", "text", "spell_id", "reward_choice_items",
    "quality", "inventory_type", "trigger", "follow", "enabled", "leader_name", "assist",
    "inviter_name",
}


class GetKeysAreDeclaredTest(unittest.TestCase):
    """mypy rejects `snapshot["typo"]` on a TypedDict but accepts `.get("typo")`
    (it types the result as `object`). So a field renamed in agent.model would
    still be read, as always missing, by a stale `.get()`. This closes that gap
    for the modules that read the snapshot."""

    def test_every_literal_get_key_is_a_model_key(self):
        declared = set(UNTYPED_KEYS)
        for value in vars(model).values():
            if typing.is_typeddict(value):
                declared |= set(value.__annotations__)
        for module in (cand, think):
            tree = ast.parse(pathlib.Path(module.__file__).read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "get" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    self.assertIn(node.args[0].value, declared,
                                  f"{module.__name__}:{node.lineno}: not a key of any agent.model type")


if __name__ == "__main__":
    unittest.main()
