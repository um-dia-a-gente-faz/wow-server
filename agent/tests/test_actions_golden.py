"""Golden pin for gh-289: the candidate lists, the action catalog and the LLM tool schema
must not change when action names move out of candidates.py. Regenerate deliberately with
`python3 -m agent.tests.test_actions_golden --write`."""

import json
import pathlib
import sys
import unittest

from agent import actions as ac
from agent import candidates as cand
from agent import llm
from agent.reflexes import follow, rest  # noqa: F401  register follow/assist/stop_following/rest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden" / "actions_golden.json"


def _fixture(name):
    return json.loads((FIXTURES / "candidates" / f"{name}.json").read_text())


def _cases() -> dict:
    gear = {"me": {"class_id": 8}, "equipment": {}, "inventory": [
        {"slot": 23, "template": {"inventory_type": 5, "class_": 4, "quality": 1, "item_level": 20,
                                  "allowable_class": -1, "subclass": 1, "stats": []}}]}
    cases = {
        "trainer": ({"window": {"kind": "trainer", "npc_guid": 8, "spells": [{"spell_id": 587}]}}, {}),
        "reward": ({"window": {"kind": "quest_offer_reward", "npc_guid": 8, "quest_id": 9,
                               "reward_choice_items": [{"entry": 1}, {"entry": 2}]}}, {}),
        "details": ({"window": {"kind": "quest_details", "npc_guid": 8, "quest_id": 9}}, {}),
        "request_items": ({"window": {"kind": "quest_request_items", "npc_guid": 8, "quest_id": 9}}, {}),
        "dead": ({"is_dead": True}, {}),
        "ghost": ({"is_ghost": True, "corpse_position": {"x": 1, "y": 2, "z": 3}}, {}),
        "giver": ({"me": {"level": 3}, "nearby_units": [
            {"guid": 5, "quest_giver_status": "available", "distance": 3, "name": "trainer"}]}, {}),
        "threat_spell": ({"spells": [{"id": 133, "name": "Fireball"}, {"id": 585}], "nearby_units": [
            {"guid": 2, "target_guid": 1, "in_combat": True, "distance": 3, "health_pct": 1}]},
            {"my_guid": 1}),
        "heal": ({"me": {"health": "5/58", "mana": "40/40"}, "spells": [{"id": 635, "name": "Holy Light"}]}, {}),
        "gear": (gear, {}),
        "usable": ({"me": {"health": "20/100"}, "inventory": [
            {"slot": 23, "template": {"spells": [{"trigger": 0}]}}]}, {}),
        "vendor": ({"window": {"kind": "vendor", "npc_guid": 8}, "inventory": [
            {"slot": 23 + i, "name": "grey", "template": {"quality": 0}} for i in range(5)]}, {}),
        "invite": ({"pending_invite": {"inviter_name": "Bob"}}, {}),
        "players": ({"nearby_players": [{"name": "Ann", "distance": 4}, {"name": "Bob", "distance": 2}]}, {}),
        "following": ({}, {"reflex_state": {"follow": {"enabled": True, "leader_name": "Ann"}}}),
        "assisting": ({}, {"reflex_state": {"follow": {"enabled": True, "assist": True}}}),
        "history_drop": (_fixture("combat"), {"my_guid": 1, "history": [
            {"action": "move_towards", "args": {"guid": 4663, "stop_distance": 3.0}, "ok": False,
             "error": "bad params: x"}]}),
    }
    for name in ("combat", "quest", "loot", "idle"):
        cases[f"fixture_{name}"] = (_fixture(name), {"my_guid": 1})
    return {name: cand.generate(snap, **kw) for name, (snap, kw) in cases.items()}


def _current() -> dict:
    cat = ac.catalog()
    return {"candidates": _cases(), "catalog": cat, "tools": llm.build_tools(cat)}


class ActionsGoldenTest(unittest.TestCase):
    def test_unchanged(self):
        want = GOLDEN.read_text()
        got = json.dumps(_current(), indent=1, sort_keys=True) + "\n"
        self.assertEqual(got, want)


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        GOLDEN.write_text(json.dumps(_current(), indent=1, sort_keys=True) + "\n")
    else:
        unittest.main()
