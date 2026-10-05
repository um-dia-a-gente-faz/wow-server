"""Unit tests for agent.candidates (UM-97): concrete (action, params)
options built from a perception snapshot, for a choice-only brain (Jev)."""

import json
import pathlib
import unittest

from agent import actions as ac
from agent import candidates as cand
from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo
from agent.reflexes import follow  # noqa: F401  registers follow/assist/stop_following
from agent.reflexes import rest  # noqa: F401 registers rest for candidate registry coverage

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
                    self.assertIn(c["action"], {"follow", "cast_spell", "train_spell", "sell_item"})
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
    def test_ghost_without_corpse_position_can_reclaim(self):
        snap = load("combat")
        snap.update(is_dead=False, is_ghost=True, corpse_position=None)
        self.assertEqual(ids(cand.generate(snap, my_guid=MY_GUID)), ["reclaim_corpse", "idle"])

    def test_ghost_runs_to_corpse_only(self):
        snap = load("combat")
        snap.update(is_dead=False, is_ghost=True, corpse_position={"map": 530, "x": 10300.5, "y": -6350.0, "z": 20.0})
        cands = cand.generate(snap, my_guid=MY_GUID)
        self.assert_well_formed(snap, cands)
        self.assertEqual(ids(cands), ["reclaim_corpse", "move_to:x=10300.5,y=-6350.0,z=20.0", "idle"])

    def test_dead_before_release_offers_release_spirit(self):
        snap = load("combat")
        snap["is_dead"] = True
        self.assertEqual(ids(cand.generate(snap, my_guid=MY_GUID)), ["release_spirit", "idle"])


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


class HandleSnapshotTest(unittest.TestCase):
    """GH-144 regression: what WorldState.snapshot() actually produces is
    handle-encoded (UM-89) — unit "guid"/"target_guid" are strings like
    "u3", not ints. generate() must resolve them through the world's
    HandleMap (passed in from think.py via Brain.decide) or every
    unit-based candidate silently vanishes and Jev is offered only idle."""

    QG = 0xF1300000000000C8        # quest giver with a quest on offer, in reach
    ATK = 0xF1300000000000C9       # in combat, targeting us, in melee range
    FIGHTER = 0xF1300000000000CA   # in combat, but targeting someone else
    OTHER = 0xF1300000000000CB     # the someone else
    PEACEFUL = 0xF1300000000000CC  # attackable mob, out of melee range

    def _block(self, guid, object_type=uo.TYPEID_UNIT, x=0.0, fields=None):
        return uo.UpdateBlock(
            update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=object_type,
            movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION,
                      "x": x, "y": 0.0, "z": 0.0, "o": 0.0},
            fields=fields or {})

    def _snapshot(self):
        ws = per.WorldState()
        ws.set_my_guid(MY_GUID)
        ws.set_my_map(530)
        ws.update_object(self._block(MY_GUID, object_type=uo.TYPEID_PLAYER))
        ws.update_object(self._block(self.QG, x=2.0))
        ws.apply_questgiver_status({"guid": self.QG, "status": 4, "status_name": "available"})
        combat = {uf.UNIT_FIELD_FLAGS: 0x00080000, uf.UNIT_FIELD_LEVEL: 2}  # UNIT_FLAG_IN_COMBAT
        ws.update_object(self._block(self.ATK, x=1.0, fields={
            **combat, uf.UNIT_FIELD_TARGET: MY_GUID, uf.UNIT_FIELD_TARGET + 1: 0}))
        ws.update_object(self._block(self.FIGHTER, x=3.0, fields={
            **combat, uf.UNIT_FIELD_TARGET: self.OTHER, uf.UNIT_FIELD_TARGET + 1: 0}))
        ws.update_object(self._block(self.OTHER, x=4.0))
        ws.update_object(self._block(self.PEACEFUL, x=10.0, fields={uf.UNIT_FIELD_LEVEL: 2}))
        snap = ws.snapshot(my_position=(530, 0.0, 0.0, 0.0, 0.0))
        snap["me"] = {"level": 5}  # think.py's _self_status(); enables unprovoked attacks
        return ws, snap

    def test_snapshot_is_really_handle_encoded(self):
        _, snap = self._snapshot()
        for unit in snap["nearby_units"]:
            self.assertIsInstance(unit["guid"], str)

    def test_unit_candidates_on_a_handle_encoded_snapshot(self):
        ws, snap = self._snapshot()
        cands = cand.generate(snap, my_guid=MY_GUID, handles=ws.handles)
        by_action = {}
        for c in cands:
            by_action.setdefault(c["action"], []).append(c)

        attack = by_action["auto_attack"][0]
        self.assertIn("attacking you", attack["label"])
        self.assertEqual(ws.handles.resolve(attack["params"]["guid"]), self.ATK)

        talk = by_action["interact"][0]
        self.assertIn("has a quest", talk["label"])
        self.assertEqual(ws.handles.resolve(talk["params"]["guid"]), self.QG)

        approach = by_action["move_towards"][0]
        self.assertEqual(ws.handles.resolve(approach["params"]["guid"]), self.PEACEFUL)

        # someone else's fight is not ours to take, handles or not
        engaged = {ws.handles.resolve(c["params"]["guid"]) for c in cands
                   if "guid" in c["params"]}
        self.assertNotIn(self.FIGHTER, engaged)
        self.assertEqual(cands[-1]["action"], "idle")

    def test_handle_params_resolve_back_for_the_action(self):
        # think.py runs actions with handles.resolve_params(candidate params).
        ws, snap = self._snapshot()
        cands = cand.generate(snap, my_guid=MY_GUID, handles=ws.handles)
        attack = next(c for c in cands if c["action"] == "auto_attack")
        self.assertEqual(ws.handles.resolve_params(attack["params"]), {"guid": self.ATK})


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


class RegistryCoverageTest(unittest.TestCase):
    def test_every_registered_action_is_offered_or_documented(self):
        offered = set()
        # Gather actions emitted by actual scenario shapes, rather than a
        # manually maintained list that could outlive a deleted branch.
        for name in ("combat", "quest", "loot", "idle"):
            offered.update(c["action"] for c in cand.generate(load(name), my_guid=MY_GUID))
        trainer = {"window": {"kind": "trainer", "npc_guid": 8,
                              "spells": [{"spell_id": 587}]}}
        offered.update(c["action"] for c in cand.generate(trainer))
        reward = {"window": {"kind": "quest_offer_reward", "npc_guid": 8, "quest_id": 9}}
        offered.update(c["action"] for c in cand.generate(reward))
        offered.update(c["action"] for c in cand.generate({"is_dead": True}))
        offered.update(c["action"] for c in cand.generate({"is_ghost": True,
                                                            "corpse_position": {"x": 1, "y": 2, "z": 3}}))
        offered.update(c["action"] for c in cand.generate({
            "me": {"level": 3}, "nearby_units": [{"guid": 5, "quest_giver_status": "available",
                                                       "distance": 3, "name": "trainer"}] }))
        threat_case = {"spells": [{"id": 133, "name": "Fireball"}], "nearby_units": [
            {"guid": 2, "target_guid": 1, "in_combat": True, "distance": 3, "health_pct": 1}
        ]}
        offered.update(c["action"] for c in cand.generate(threat_case, my_guid=1))
        offered.update(c["action"] for c in cand.generate({}, reflex_state={"follow": {"enabled": True}}))
        gear = {"me": {"class_id": 8}, "equipment": {}, "inventory": [
            {"slot": 23, "template": {"inventory_type": 5, "class_": 4,
                                         "quality": 1, "item_level": 20,
                                         "allowable_class": -1, "subclass": 1, "stats": []}}
        ]}
        offered.update(c["action"] for c in cand.generate(gear))
        usable = {"me": {"health": "20/100"}, "inventory": [
            {"slot": 23, "template": {"spells": [{"trigger": 0}]}}
        ]}
        offered.update(c["action"] for c in cand.generate(usable))
        vendor = {"window": {"kind": "vendor", "npc_guid": 8}, "inventory": [
            {"slot": 23, "name": "grey cloth", "template": {"quality": 0}}
        ]}
        offered.update(c["action"] for c in cand.generate(vendor))
        self.assertEqual(set(ac.REGISTRY) - offered - set(cand.NOT_OFFERED), set())
        self.assertTrue(all(isinstance(reason, str) and reason for reason in cand.NOT_OFFERED.values()))

    def test_low_health_offers_self_heal_without_offensive_spell(self):
        snap = {"me": {"health": "5/58", "mana": "40/40"}, "spells": [{"id": 635, "name": "Holy Light"}],
                "nearby_units": [], "inventory": [], "equipment": {}, "window": None}
        cands = cand.generate(snap, my_guid=1)
        heal = [c for c in cands if c["action"] == "cast_spell"]
        self.assertEqual([c["params"] for c in heal], [{"spell_id": 635}])
        healthy = dict(snap, me={"health": "58/58", "mana": "40/40"})
        self.assertNotIn("cast_spell", [c["action"] for c in cand.generate(healthy, my_guid=1)])
        # #206 in combat: a Holy-Light-only Paladin under attack still gets the cast, ahead of auto_attack.
        threat = {"guid": 2, "target_guid": 1, "in_combat": True, "distance": 3, "name": "rat", "health_pct": 1}
        actions = [c["action"] for c in cand.generate(dict(snap, nearby_units=[threat]), my_guid=1)]
        self.assertLess(actions.index("cast_spell"), actions.index("auto_attack"))

    def test_new_candidates_require_their_snapshot_preconditions(self):
        threat = {"guid": 2, "target_guid": 1, "in_combat": True, "distance": 3,
                  "name": "rat", "health_pct": 1}
        base = {"me": {"class_id": 8}, "spells": [{"id": 133, "name": "Fireball"}],
                "nearby_units": [threat], "inventory": [], "equipment": {}, "window": None}
        actions = [c["action"] for c in cand.generate(base, my_guid=1)]
        self.assertIn("cast_spell", actions)
        self.assertNotIn("train_spell", actions)
        self.assertNotIn("sell_item", actions)
        trainer = dict(base, window={"kind": "trainer", "npc_guid": "u2", "spells": [{"spell_id": 587}]})
        self.assertIn("train_spell", [c["action"] for c in cand.generate(trainer, my_guid=1)])
        self.assertNotIn("cast_spell", [c["action"] for c in cand.generate(dict(base, spells=[]), my_guid=1)])

    def test_inventory_candidates_are_backpack_only_and_need_driven(self):
        good = {"quality": 0, "inventory_type": 5, "class_": 4, "subclass": 1,
                "allowable_class": -1, "item_level": 20, "stats": [],
                "spells": [{"trigger": 0}]}
        snap = {"me": {"class_id": 8, "health": "20/100", "mana": "20/100"},
                "equipment": {}, "inventory": [
                    {"slot": 19, "name": "bag", "template": dict(good)},
                    {"slot": 23, "name": "backpack item", "template": dict(good)},
                ], "window": {"kind": "vendor", "npc_guid": "u1"}}
        actions = [c["action"] for c in cand.generate(snap)]
        self.assertEqual(actions.count("use_item"), 1)
        self.assertEqual(actions.count("equip_item"), 1)
        self.assertEqual(actions.count("sell_item"), 1)
        snap["me"].update(health="100/100", mana="100/100")
        self.assertNotIn("use_item", [c["action"] for c in cand.generate(snap)])

    def test_sell_and_use_skip_equipped_bag_slots_and_non_equippables(self):
        snap = {"me": {"class_id": 1, "health": "10/100", "mana": "10/100"},
                "equipment": {}, "inventory": [
                    {"slot": 19, "template": {"quality": 0, "spells": [{"trigger": 0}]}}
                ], "window": {"kind": "vendor", "npc_guid": 1}}
        actions = [c["action"] for c in cand.generate(snap)]
        self.assertNotIn("sell_item", actions)
        self.assertNotIn("use_item", actions)
        snap["inventory"] = [{"slot": 23, "template": {"inventory_type": 0,
                          "class_": 4, "stats": [{"type": 4, "value": 100}]}}]
        self.assertNotIn("equip_item", [c["action"] for c in cand.generate(snap)])

    def test_ring_upgrade_compares_against_weakest_ring(self):
        old_weak = {"template": {"stats": [{"type": 4, "value": 1}], "item_level": 1}}
        old_strong = {"template": {"stats": [{"type": 4, "value": 10}], "item_level": 1}}
        ring = {"slot": 23, "template": {"inventory_type": 11, "class_": 4,
                "allowable_class": -1, "stats": [{"type": 4, "value": 2}], "item_level": 1}}
        snap = {"me": {"class_id": 1}, "inventory": [ring],
                "equipment": {10: old_weak, 11: old_strong}}
        self.assertIn("equip_item", [c["action"] for c in cand.generate(snap)])
        snap["equipment"][10] = old_strong
        self.assertNotIn("equip_item", [c["action"] for c in cand.generate(snap)])

    def test_equipment_mapping_handles_pairs_and_ignores_non_equippable(self):
        def item(slot, score, inv_type):
            return {"slot": slot, "template": {"quality": 1, "inventory_type": inv_type,
                    "class_": 4, "subclass": 1, "allowable_class": -1,
                    "item_level": score, "stats": []}}
        snap = {"me": {"class_id": 8}, "inventory": [item(23, 15, 11), item(24, 30, 0)],
                "equipment": {10: {"template": {"item_level": 10, "stats": []}},
                              11: {"template": {"item_level": 20, "stats": []}}}}
        self.assertEqual([c["params"]["slot"] for c in cand.generate(snap)
                          if c["action"] == "equip_item"], [23])

    def test_item_template_snapshot_projection_excludes_unneeded_fields(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        ws.items.items[42] = {"entry": 42, "name": "test", "quality": 1,
                              "inventory_type": 5, "stats": [], "spells": [],
                              "guid": 0xF130000000000042, "description": "large"}
        player_fields = {uf.PLAYER_FIELD_INV_SLOT_HEAD: 42,
                         uf.PLAYER_FIELD_INV_SLOT_HEAD + 1: 0,
                         uf.PLAYER_FIELD_INV_SLOT_HEAD + 19 * 2: 0,
                         uf.PLAYER_FIELD_PACK_SLOT_1: 42,
                         uf.PLAYER_FIELD_PACK_SLOT_1 + 1: 0}
        ws.objects[1] = per.ObjectInfo(guid=1, object_type="player", raw_fields=player_fields)
        ws.objects[42] = per.ObjectInfo(guid=42, object_type="item", entry=42)
        _, inventory = ws.build_equipment_and_inventory()
        template = inventory[0]["template"]
        self.assertEqual(set(template), {"quality", "inventory_type", "stats", "spells"})
        self.assertNotIn("guid", template)


class HistoryAwareTest(unittest.TestCase):
    """GH-166: history drops options that cannot work and demotes no-op repeats."""

    BASE = ["auto_attack:guid=4660", "loot:guid=4661",
            "move_towards:guid=4663,stop_distance=3.0", "idle"]

    def gen(self, history, notes=None):
        return cand.generate(load("combat"), my_guid=MY_GUID, history=history, notes=notes)

    @staticmethod
    def entry(action, args, ok=True, error=None, changed=None):
        e = {"action": action, "args": args, "ok": ok}
        if error:
            e["error"] = error
        if changed is False:
            e["changed"] = False
        return e

    def test_no_history_matches_baseline(self):
        base = ids(cand.generate(load("combat"), my_guid=MY_GUID))
        self.assertEqual(base, self.BASE)
        for h in (None, [], [None, "x", {}, {"action": 3}]):
            self.assertEqual(ids(self.gen(h)), base)

    def test_drops_deterministic_rejection(self):
        notes = []
        h = [self.entry("loot", {"guid": 4661}, False, "bad params: x")]
        self.assertEqual(ids(self.gen(h, notes)),
                         ["auto_attack:guid=4660", "move_towards:guid=4663,stop_distance=3.0", "idle"])
        self.assertEqual([(n["id"], n["effect"]) for n in notes], [("loot:guid=4661", "dropped")])

    def test_drops_same_error_twice_but_not_once(self):
        move = {"guid": 4663, "stop_distance": 3.0}
        once = [self.entry("move_towards", move, False, "stuck")]
        self.assertEqual(ids(self.gen(once)), self.BASE)
        twice = once * 2
        self.assertNotIn("move_towards:guid=4663,stop_distance=3.0", ids(self.gen(twice)))

    def test_drop_expires_and_is_cleared_by_later_success(self):
        rej = self.entry("loot", {"guid": 4661}, False, "missing required params: ['guid']")
        fill = [self.entry("idle", {})] * cand.HISTORY_DROP_WINDOW
        self.assertEqual(ids(self.gen([rej] + fill)), self.BASE)
        self.assertEqual(ids(self.gen([rej, self.entry("loot", {"guid": 4661})])), self.BASE)

    def test_demotes_consecutive_no_effect_repeats(self):
        notes = []
        loot = self.entry("loot", {"guid": 4661}, changed=False)
        self.assertEqual(ids(self.gen([loot], [])), self.BASE)   # once is not enough
        self.assertEqual(ids(self.gen([loot, loot], notes)),
                         ["auto_attack:guid=4660", "move_towards:guid=4663,stop_distance=3.0",
                          "loot:guid=4661", "idle"])
        self.assertEqual([n["effect"] for n in notes], ["demoted"])

    def test_no_demotion_when_changed_unknown_or_not_consecutive(self):
        loot = self.entry("loot", {"guid": 4661}, changed=False)
        changed_unknown = self.entry("loot", {"guid": 4661})   # `changed` absent: it had an effect
        self.assertEqual(ids(self.gen([changed_unknown] * 3)), self.BASE)
        self.assertEqual(ids(self.gen([loot, self.entry("idle", {}), loot])), self.BASE)

    def test_threat_is_never_dropped_or_demoted(self):
        atk = {"guid": 4660}
        rejected = [self.entry("auto_attack", atk, False, "bad params: x")]
        repeated = [self.entry("auto_attack", atk, changed=False)] * 3
        for h in (rejected, repeated):
            self.assertEqual(ids(self.gen(h)), self.BASE)

    def test_malformed_entries_are_skipped(self):
        h = [{"action": "loot"}, {"action": "loot", "args": object(), "ok": False},
             {"args": {}}, self.entry("loot", {"guid": 4661}, False, "bad params: x")]
        self.assertNotIn("loot:guid=4661", ids(self.gen(h)))


if __name__ == "__main__":
    unittest.main()
