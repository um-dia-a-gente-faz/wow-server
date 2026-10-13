"""gh-674: the snapshot carries a unit's rank and whether its template is resolved; an unprovoked
pull skips an elite and a unit whose rank is not known yet; defence is always offered."""

import unittest

from agent import candidates as cand
from agent import death  # noqa: F401  registers release_spirit/reclaim_corpse
from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo
from agent.reflexes import follow  # noqa: F401
from agent.tests.test_perception import create_block

MY_GUID = 1
ENTRY = 4000


def unit(**kw) -> dict:
    return {"guid": 5, "distance": 8.0, "level": 5, "name": "Hogger", "in_combat": False, **kw}


def gen(*units: dict) -> list:
    snap = {"me": {"level": 5}, "nearby_units": list(units), "nearby_players": [], "spells": []}
    return cand.generate(snap, my_guid=MY_GUID)


def attacks(cands) -> list:
    return [c for c in cands if c["action"] in ("auto_attack", "move_towards")]


def world_with_unit() -> per.WorldState:
    ws = per.WorldState()
    ws.set_my_guid(MY_GUID)
    ws.set_my_map(0)
    ws.update_object(create_block(MY_GUID, uo.TYPEID_PLAYER, 0.0, 0.0, 0.0))
    ws.update_object(create_block(5, uo.TYPEID_UNIT, 8.0, 0.0, 0.0, fields={
        uf.OBJECT_FIELD_ENTRY: ENTRY, uf.UNIT_FIELD_LEVEL: 5}))
    return ws


def response(rank="normal", found=True) -> dict:
    if not found:
        return {"entry": ENTRY, "found": False}
    return {"entry": ENTRY, "found": True, "name": "Hogger", "subname": "", "rank": rank,
            "creature_type_name": "humanoid"}


def nearby(ws) -> dict:
    return ws.snapshot(max_range=50)["nearby_units"][0]


class PullRules(unittest.TestCase):
    def test_normal_unit_is_pulled(self):
        self.assertTrue(attacks(gen(unit())))

    def test_no_unprovoked_pull_on_elite_rareelite_worldboss(self):
        for rank in ("elite", "rareelite", "worldboss"):
            with self.subTest(rank=rank):
                self.assertEqual(attacks(gen(unit(rank=rank))), [])

    def test_rare_is_pullable(self):
        self.assertTrue(attacks(gen(unit(rank="rare"))))

    def test_unresolved_unit_is_not_pulled(self):
        self.assertEqual(attacks(gen(unit(resolved=False))), [])

    def test_elite_attacking_us_is_offered_labelled_with_its_rank(self):
        cands = attacks(gen(unit(rank="elite", in_combat=True, target_guid=MY_GUID)))
        self.assertEqual(len(cands), 1)
        self.assertIn("Hogger (elite)", cands[0]["label"])

    def test_unresolved_unit_attacking_us_is_offered(self):
        self.assertTrue(attacks(gen(unit(resolved=False, in_combat=True, target_guid=MY_GUID))))

    def test_offensive_spell_at_an_elite_attacker_is_offered(self):
        snap = {"me": {"level": 5}, "spells": [{"id": 133, "name": "Fireball"}], "nearby_players": [],
                "nearby_units": [unit(rank="worldboss", in_combat=True, target_guid=MY_GUID)]}
        self.assertTrue([c for c in cand.generate(snap, my_guid=MY_GUID) if c["action"] == "cast_spell"])

    def test_non_combat_candidates_for_an_unresolved_unit_are_kept(self):
        u = unit(resolved=False, lootable=True, distance=2.0)
        self.assertTrue([c for c in gen(u) if c["action"] == "loot"])


class SnapshotKeys(unittest.TestCase):
    def test_unresolved_until_answered_then_no_key(self):
        ws = world_with_unit()
        self.assertIs(nearby(ws)["resolved"], False)
        ws.apply_creature_query_response(response())
        self.assertNotIn("resolved", nearby(ws))

    def test_not_found_counts_as_resolved(self):
        ws = world_with_unit()
        ws.apply_creature_query_response(response(found=False))
        u = nearby(ws)
        self.assertNotIn("resolved", u)
        self.assertTrue(attacks(cand.generate(
            {"me": {"level": 5}, "nearby_units": [{**u, "guid": 5}], "nearby_players": []}, my_guid=MY_GUID)))

    def test_cached_entry_is_resolved_on_creation(self):
        ws = per.WorldState()
        ws.names.creatures[ENTRY] = response()
        ws.set_my_map(0)
        ws.update_object(create_block(5, uo.TYPEID_UNIT, 1.0, 0.0, 0.0, fields={uf.OBJECT_FIELD_ENTRY: ENTRY}))
        ws.set_my_guid(MY_GUID)
        ws.update_object(create_block(MY_GUID, uo.TYPEID_PLAYER, 0.0, 0.0, 0.0))
        self.assertNotIn("resolved", nearby(ws))

    def test_cached_not_found_entry_is_resolved_on_creation(self):
        ws = per.WorldState()
        ws.names.creatures[ENTRY] = None
        ws.set_my_map(0)
        ws.set_my_guid(MY_GUID)
        ws.update_object(create_block(MY_GUID, uo.TYPEID_PLAYER, 0.0, 0.0, 0.0))
        ws.update_object(create_block(5, uo.TYPEID_UNIT, 1.0, 0.0, 0.0, fields={uf.OBJECT_FIELD_ENTRY: ENTRY}))
        self.assertNotIn("resolved", nearby(ws))

    def test_rank_key_only_when_not_normal(self):
        ws = world_with_unit()
        ws.apply_creature_query_response(response("normal"))
        self.assertNotIn("rank", nearby(ws))
        ws.apply_creature_query_response(response("elite"))
        self.assertEqual(nearby(ws)["rank"], "elite")

    def test_players_and_gameobjects_never_get_resolved(self):
        ws = world_with_unit()
        ws.update_object(create_block(6, uo.TYPEID_PLAYER, 3.0, 0.0, 0.0))
        ws.update_object(create_block(7, uo.TYPEID_GAMEOBJECT, 4.0, 0.0, 0.0, fields={uf.OBJECT_FIELD_ENTRY: 99}))
        snap = ws.snapshot(max_range=50)
        for d in snap["nearby_players"] + snap["nearby_objects"]:
            self.assertNotIn("resolved", d)

    def test_elite_unit_from_the_world_yields_no_pull(self):
        ws = world_with_unit()
        ws.apply_creature_query_response(response("elite"))
        snap = ws.snapshot(max_range=50)
        self.assertEqual(attacks(cand.generate(snap, my_guid=MY_GUID, handles=ws.handles)), [])


if __name__ == "__main__":
    unittest.main()
