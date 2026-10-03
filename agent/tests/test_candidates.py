"""Unit tests for agent.candidates (UM-97): concrete (action, params)
options built from a perception snapshot, for a choice-only brain (Jev)."""

import json
import pathlib
import unittest

from agent import actions as ac
from agent import candidates as cand
from agent import perception as per
from agent import update_object as uo
from agent.reflexes import follow  # noqa: F401  registers follow/assist/stop_following

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
MY_GUID = 1

# Params that would carry text a brain had to write itself.
FREE_TEXT_PARAMS = {"message", "text", "body", "subject", "to", "code", "target_name", "name", "channel"}


def load(name: str) -> dict:
    return json.loads((FIXTURES / "candidates" / f"{name}.json").read_text())


def ids(cands) -> list:
    return [c["id"] for c in cands]


def _strings(value, out: set):
    if isinstance(value, str):
        out.add(value)
    elif isinstance(value, dict):
        for v in value.values():
            _strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _strings(v, out)
    return out


class CandidateShapeMixin:
    """Invariants every generated list must satisfy."""

    def assert_well_formed(self, snapshot, cands):
        self.assertGreaterEqual(len(cands), 1)
        self.assertLessEqual(len(cands), cand.MAX_CANDIDATES)
        self.assertEqual(cands[-1]["action"], "idle")
        self.assertEqual(len(set(ids(cands))), len(cands), "ids must be unique")
        snapshot_strings = _strings(snapshot, set())
        for c in cands:
            self.assertEqual(set(c), {"id", "label", "action", "params"})
            self.assertIsInstance(c["label"], str)
            self.assertTrue(c["label"])
            action = ac.REGISTRY.get(c["action"])
            self.assertIsNotNone(action, f"{c['action']} is not a registered action")
            self.assertLessEqual(set(c["params"]), set(action.params), c)
            for req in action.required:
                self.assertIn(req, c["params"], c)
            # "Jev never generates args": no free-text params at all, except a
            # name copied verbatim out of the snapshot.
            for key, value in c["params"].items():
                if isinstance(value, str):
                    self.assertIn(value, snapshot_strings, f"{c['id']}: {key}={value!r} not from the snapshot")
                    self.assertEqual(c["action"], "follow")
                else:
                    self.assertIsInstance(value, (int, float, bool), c)
                    self.assertNotIn(key, FREE_TEXT_PARAMS, c)
            self.assertEqual(c["id"], cand.candidate(c["action"], c["params"], "")["id"])
        json.dumps(cands)  # plain JSON for the Jev client and the audit log


class CombatScenarioTest(CandidateShapeMixin, unittest.TestCase):
    def test_combat(self):
        snap = load("combat")
        cands = cand.generate(snap, my_guid=MY_GUID)
        self.assert_well_formed(snap, cands)
        self.assertEqual(ids(cands), [
            "auto_attack:guid=4660",                    # attacking us, in melee range: first
            "loot:guid=4661",
            "move_towards:guid=4663,stop_distance=3.0",  # idle wyrm 14 yd away
            "idle",
        ])
        self.assertIn("attacking you", cands[0]["label"])
        # 4662 fights someone else; 4670 is 7 levels above us.
        self.assertNotIn(4662, [c["params"].get("guid") for c in cands])
        self.assertNotIn(4670, [c["params"].get("guid") for c in cands])

    def test_no_unprovoked_attacks_without_own_level(self):
        snap = load("combat")
        del snap["me"]
        self.assertEqual(ids(cand.generate(snap, my_guid=MY_GUID)),
                         ["auto_attack:guid=4660", "loot:guid=4661", "idle"])

    def test_without_my_guid_threats_are_plain_targets(self):
        cands = cand.generate(load("combat"))
        self.assertIn("auto_attack:guid=4660", ids(cands))
        self.assertNotIn("attacking you", cands[ids(cands).index("auto_attack:guid=4660")]["label"])


class QuestScenarioTest(CandidateShapeMixin, unittest.TestCase):
    def test_quest(self):
        snap = load("quest")
        cands = cand.generate(snap, my_guid=MY_GUID)
        self.assert_well_formed(snap, cands)
        self.assertEqual(ids(cands), [
            "complete_quest:npc_guid=5001,quest_id=8325",  # already in the log
            "accept_quest:npc_guid=5001,quest_id=8326",
            "gossip_select:option_index=0",                # coded option 1 is never offered
            "close_window",
            "move_towards:guid=5002,stop_distance=3.0",    # reward ready, 20 yd away
            "abandon_quest:slot=1",                        # the failed quest
            "idle",
        ])

    def test_quest_details_window(self):
        snap = load("quest")
        snap["window"] = {"kind": "quest_details", "npc_guid": 5001, "quest_id": 8326,
                          "title": "Unfortunate Measures"}
        cands = cand.generate(snap, my_guid=MY_GUID)
        self.assertEqual(ids(cands)[:2], ["accept_quest:npc_guid=5001,quest_id=8326", "close_window"])
        self.assertEqual(cands[0]["label"], "accept quest Unfortunate Measures")

    def test_request_items_window(self):
        snap = load("quest")
        snap["window"] = {"kind": "quest_request_items", "npc_guid": 5001, "quest_id": 8325, "title": "x"}
        self.assertEqual(ids(cand.generate(snap))[0], "complete_quest:npc_guid=5001,quest_id=8325")

    def test_offer_reward_lists_each_choice(self):
        snap = load("quest")
        snap["window"] = {"kind": "quest_offer_reward", "npc_guid": 5001, "quest_id": 8325, "title": "x",
                          "reward_choice_items": [{"entry": 20982, "count": 1}, {"entry": 20983, "count": 1}]}
        cands = cand.generate(snap)
        self.assert_well_formed(snap, cands)
        self.assertEqual(ids(cands)[:3], [
            "turn_in_quest:npc_guid=5001,quest_id=8325,reward_choice=0",
            "turn_in_quest:npc_guid=5001,quest_id=8325,reward_choice=1",
            "close_window",
        ])

    def test_offer_reward_without_choices_offers_choice_zero(self):
        snap = load("quest")
        snap["window"] = {"kind": "quest_offer_reward", "npc_guid": 5001, "quest_id": 8325,
                          "reward_choice_items": []}
        self.assertEqual(ids(cand.generate(snap))[0], "turn_in_quest:npc_guid=5001,quest_id=8325,reward_choice=0")

    def test_questgiver_in_reach_gets_interact(self):
        snap = load("quest")
        snap["window"] = None
        snap["nearby_units"][1]["distance"] = 4.0
        self.assertIn("interact:guid=5002", ids(cand.generate(snap)))


class LootScenarioTest(CandidateShapeMixin, unittest.TestCase):
    def test_loot(self):
        snap = load("loot")
        cands = cand.generate(snap, my_guid=MY_GUID)
        self.assert_well_formed(snap, cands)
        # In reach: loot. 12 yd: walk over. 60 yd: too far for a hop. Not lootable: skipped.
        self.assertEqual(ids(cands), [
            "loot:guid=4661",
            "move_towards:guid=4664,stop_distance=2.0",
            "idle",
        ])


class IdleScenarioTest(CandidateShapeMixin, unittest.TestCase):
    def test_idle_with_invite_and_players(self):
        snap = load("idle")
        cands = cand.generate(snap, my_guid=MY_GUID)
        self.assert_well_formed(snap, cands)
        self.assertEqual(ids(cands), [
            "accept_group",
            "follow:player_name=Rubens",   # unnamed player skipped; vendor never a target
            "follow:player_name=Luaprata",
            "idle",
        ])
        self.assertEqual(cands[0]["label"], "accept the group invite from Rubens")

    def test_idle_action_sends_nothing(self):
        result = ac.REGISTRY["idle"].run(None, None)  # touching session/world would raise
        self.assertTrue(result.ok)

    def test_empty_snapshot_is_just_idle(self):
        self.assertEqual(cand.generate({}), [cand.candidate("idle", {}, "do nothing this cycle")])

    def test_following_offers_stop_and_assist_toggle(self):
        snap = load("idle")
        snap["pending_invite"] = None
        reflex = {"follow": {"enabled": True, "leader_guid": 42, "leader_name": "Rubens", "assist": False}}
        cands = cand.generate(snap, reflex_state=reflex)
        self.assert_well_formed(snap, cands)
        self.assertEqual(ids(cands), ["stop_following", "assist:on=True", "idle"])
        reflex["follow"]["assist"] = True
        self.assertIn("assist:on=False", ids(cand.generate(snap, reflex_state=reflex)))


class DeadScenarioTest(CandidateShapeMixin, unittest.TestCase):
    def test_ghost_runs_to_corpse_only(self):
        snap = load("combat")
        snap.update(is_dead=False, is_ghost=True, corpse_position={"map": 530, "x": 10300.5, "y": -6350.0, "z": 20.0})
        cands = cand.generate(snap, my_guid=MY_GUID)
        self.assert_well_formed(snap, cands)
        self.assertEqual(ids(cands), ["move_to:x=10300.5,y=-6350.0,z=20.0", "idle"])

    def test_dead_before_release_is_idle(self):
        snap = load("combat")
        snap["is_dead"] = True
        self.assertEqual(ids(cand.generate(snap, my_guid=MY_GUID)), ["idle"])


class BoundsTest(CandidateShapeMixin, unittest.TestCase):
    def test_capped_and_idle_kept(self):
        snap = load("combat")
        snap["nearby_units"] = [
            {"guid": 100 + i, "name": "Mana Wyrm", "distance": 2.0 + i, "level": 2, "health_pct": 1.0,
             "in_combat": True, "target_guid": MY_GUID} for i in range(30)
        ] + [
            {"guid": 300 + i, "name": "Mana Wyrm", "distance": 3.0, "health_pct": 0.0, "lootable": True}
            for i in range(30)
        ]
        cands = cand.generate(snap, my_guid=MY_GUID, limit=5)
        self.assertEqual(len(cands), 5)
        self.assertEqual(cands[-1]["action"], "idle")
        full = cand.generate(snap, my_guid=MY_GUID)
        self.assert_well_formed(snap, full)
        self.assertEqual(sum(c["action"] == "loot" for c in full), cand.MAX_LOOT)

    def test_ids_stable_across_cycles(self):
        snap = load("combat")
        a = cand.generate(snap, my_guid=MY_GUID)
        snap["nearby_units"][0]["health_pct"] = 0.3  # the fight moved on
        b = cand.generate(snap, my_guid=MY_GUID)
        self.assertEqual(ids(a), ids(b))


class RealSnapshotTest(CandidateShapeMixin, unittest.TestCase):
    """The busy Silvermoon capture (fixtures/update_object/busy_zone.bin)
    through the real parser and WorldState.snapshot(), so the generator is
    exercised against the snapshot shape perception actually produces."""

    def test_busy_zone(self):
        ws = per.WorldState()
        ws.set_my_map(530)
        for block in uo.parse_update_object((FIXTURES / "update_object" / "busy_zone.bin").read_bytes()):
            ws.update_object(block)
        snap = ws.snapshot(my_position=(530, 9487.69, -7279.2, 14.29, 0.0), max_range=500)
        flagged = {u["guid"] for u in snap["nearby_units"] if u.get("npc_flags")}
        self.assertTrue(flagged, "fixture should contain NPCs with npc_flags")

        # Own level unknown: no unprovoked attack options at all.
        cands = cand.generate(snap, my_guid=MY_GUID)
        self.assert_well_formed(snap, cands)
        self.assertNotIn("auto_attack", [c["action"] for c in cands])
        self.assertNotIn("move_towards", [c["action"] for c in cands])

        # A level-5 agent: Silvermoon's level 57-65 guards and the flagged
        # service NPCs never show up as targets.
        snap["me"] = {"level": 5}
        cands = cand.generate(snap, my_guid=MY_GUID)
        self.assert_well_formed(snap, cands)
        levels = {u["guid"]: u.get("level") for u in snap["nearby_units"]}
        for c in cands:
            if c["action"] in ("auto_attack", "move_towards"):
                self.assertNotIn(c["params"]["guid"], flagged)
                self.assertLessEqual(levels[c["params"]["guid"]], 5 + cand.ATTACK_LEVEL_MARGIN)


class SnapshotFieldsTest(unittest.TestCase):
    """The two snapshot fields UM-97 added to perception's object dicts."""

    def _snap_unit(self, fields):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        mv = {"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": 0, "y": 0, "z": 0, "o": 0}
        ws.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=1,
                                        object_type=uo.TYPEID_PLAYER, movement=dict(mv), fields={}))
        ws.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=2,
                                        object_type=uo.TYPEID_UNIT, movement=dict(mv), fields=fields))
        return ws.snapshot()["nearby_units"][0]

    def test_plain_unit_has_neither(self):
        unit = self._snap_unit({})
        self.assertNotIn("lootable", unit)
        self.assertNotIn("npc_flags", unit)

    def test_lootable_and_npc_flags(self):
        from agent import update_fields as uf
        unit = self._snap_unit({uf.UNIT_DYNAMIC_FLAGS: per.UNIT_DYNFLAG_LOOTABLE, uf.UNIT_NPC_FLAGS: 2})
        self.assertIs(unit["lootable"], True)
        self.assertEqual(unit["npc_flags"], 2)


if __name__ == "__main__":
    unittest.main()
