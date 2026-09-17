"""Unit tests for agent.perception.WorldState against synthetic UpdateBlocks
(agent.update_object.UpdateBlock), so no byte-level parsing is needed here —
that's agent/tests/test_update_object_parser.py's job."""

import pathlib
import tempfile
import unittest
from unittest import mock

from agent import perception as per
from agent import update_object as uo

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "update_object"


def create_block(guid, object_type=uo.TYPEID_UNIT, x=0.0, y=0.0, z=0.0, o=0.0,
                  fields=None):
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=object_type,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": o},
        fields=fields or {},
    )


def values_block(guid, fields):
    return uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=guid, fields=fields)


def movement_block(guid, x, y, z, o=0.0):
    return uo.UpdateBlock(update_type=uo.UPDATETYPE_MOVEMENT, guid=guid,
                           movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": o})


class UpdateObjectTest(unittest.TestCase):
    def test_create_adds_object(self):
        ws = per.WorldState()
        ws.update_object(create_block(1, fields={0x03: 6368}))
        obj = ws.get_object(1)
        self.assertIsNotNone(obj)
        self.assertEqual(obj.entry, 6368)
        self.assertEqual(obj.object_type, "unit")

    def test_values_merges_into_existing(self):
        ws = per.WorldState()
        ws.update_object(create_block(1, fields={0x03: 6368}))
        # UNIT_FIELD_HEALTH=0x18, UNIT_FIELD_MAXHEALTH=0x20 (see update_fields.py)
        ws.update_object(values_block(1, {0x18: 5, 0x20: 100}))
        obj = ws.get_object(1)
        self.assertEqual(obj.entry, 6368)  # earlier field preserved
        self.assertEqual(obj.health, 5)
        self.assertEqual(obj.max_health, 100)

    def test_values_for_unknown_guid_is_ignored_and_counted(self):
        ws = per.WorldState()
        ws.update_object(values_block(99, {0x18: 5}))
        self.assertIsNone(ws.get_object(99))
        self.assertEqual(ws.unknown_field_updates, 1)

    def test_movement_updates_position(self):
        ws = per.WorldState()
        ws.set_my_map(530)
        ws.update_object(create_block(1, x=0, y=0, z=0))
        ws.update_object(movement_block(1, 10.0, 20.0, 30.0))
        obj = ws.get_object(1)
        self.assertEqual(obj.position, (530, 10.0, 20.0, 30.0, 0.0))

    def test_movement_for_unknown_guid_is_ignored_and_counted(self):
        ws = per.WorldState()
        ws.update_object(movement_block(99, 1, 2, 3))
        self.assertIsNone(ws.get_object(99))
        self.assertEqual(ws.unknown_field_updates, 1)

    def test_create_replaces_existing(self):
        ws = per.WorldState()
        ws.update_object(create_block(1, fields={0x03: 111}))
        ws.update_object(create_block(1, fields={0x03: 222}))
        self.assertEqual(ws.get_object(1).entry, 222)

    def test_out_of_range_removes(self):
        ws = per.WorldState()
        ws.update_object(create_block(1))
        ws.update_object(create_block(2))
        ws.remove_guids([1])
        self.assertIsNone(ws.get_object(1))
        self.assertIsNotNone(ws.get_object(2))


class NameResolutionTest(unittest.TestCase):
    """UM-35: WorldState enqueues name queries for unknown entries/players on
    CREATE, resolves immediately from the cache when already known, and
    backfills .name (etc.) on every matching object when a response arrives."""

    def setUp(self):
        # apply_creature_query_response()/apply_gameobject_query_response()
        # save the on-disk cache on every "found" response — redirect every
        # WorldState in this class to a throwaway file so tests never touch
        # the real ~/.cache/wow-agent/names.json.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def _new_ws(self) -> per.WorldState:
        ws = per.WorldState()
        ws.names.cache_path = f"{self._tmpdir.name}/names.json"
        return ws

    def test_unknown_creature_entry_enqueues_a_query(self):
        ws = self._new_ws()
        ws.update_object(create_block(1, object_type=uo.TYPEID_UNIT, fields={0x03: 17213}))
        self.assertEqual(ws.names.drain(), [("creature", 17213, 1)])
        self.assertEqual(ws.get_object(1).name, "")

    def test_cached_creature_entry_resolves_immediately_no_query(self):
        ws = self._new_ws()
        ws.names.creatures[17213] = {"name": "Broom", "subname": "", "rank": "normal",
                                      "creature_type_name": "critter"}
        ws.update_object(create_block(1, object_type=uo.TYPEID_UNIT, fields={0x03: 17213}))
        self.assertEqual(ws.get_object(1).name, "Broom")
        self.assertEqual(ws.get_object(1).creature_type, "critter")
        self.assertEqual(ws.names.drain(), [])

    def test_unknown_player_guid_enqueues_a_query(self):
        ws = self._new_ws()
        ws.set_my_guid(99)  # a different guid — this player isn't "us"
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, fields={0x03: 0}))
        self.assertEqual(ws.names.drain(), [("player", 1, 1)])

    def test_own_object_is_never_queried(self):
        ws = self._new_ws()
        ws.set_my_guid(1)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, fields={0x03: 0}))
        self.assertEqual(ws.names.drain(), [])

    def test_creature_query_response_backfills_every_matching_object(self):
        ws = self._new_ws()
        ws.update_object(create_block(1, object_type=uo.TYPEID_UNIT, fields={0x03: 37543}))
        ws.update_object(create_block(2, object_type=uo.TYPEID_UNIT, fields={0x03: 37543}))
        ws.apply_creature_query_response({"entry": 37543, "found": True, "name": "Shaker",
                                           "subname": "", "rank": "normal", "creature_type_name": "humanoid"})
        self.assertEqual(ws.get_object(1).name, "Shaker")
        self.assertEqual(ws.get_object(2).name, "Shaker")
        self.assertEqual(ws.names.creatures[37543]["name"], "Shaker")

    def test_creature_query_response_not_found_is_cached_and_leaves_name_blank(self):
        ws = self._new_ws()
        ws.update_object(create_block(1, object_type=uo.TYPEID_UNIT, fields={0x03: 999}))
        ws.apply_creature_query_response({"entry": 999, "found": False})
        self.assertEqual(ws.get_object(1).name, "")
        self.assertIsNone(ws.names.creatures[999])

    def test_name_query_response_backfills_the_player(self):
        ws = self._new_ws()
        ws.set_my_guid(99)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, fields={0x03: 0}))
        ws.apply_name_query_response({"guid": 1, "found": True, "name": "Rubens", "realm": "",
                                       "race": 2, "sex": 0, "class_": 1, "has_declined_names": False})
        self.assertEqual(ws.get_object(1).name, "Rubens")

    def test_gameobject_query_response_backfills(self):
        ws = self._new_ws()
        ws.update_object(create_block(1, object_type=uo.TYPEID_GAMEOBJECT, fields={0x03: 181646}))
        ws.apply_gameobject_query_response({"entry": 181646, "found": True, "name": "Ship"})
        self.assertEqual(ws.get_object(1).name, "Ship")

    def test_elite_rank_is_kept_but_normal_is_blank(self):
        ws = self._new_ws()
        ws.update_object(create_block(1, object_type=uo.TYPEID_UNIT, fields={0x03: 1}))
        ws.apply_creature_query_response({"entry": 1, "found": True, "name": "Boss",
                                           "subname": "", "rank": "worldboss", "creature_type_name": "humanoid"})
        self.assertEqual(ws.get_object(1).rank, "worldboss")

    def test_snapshot_includes_resolved_name(self):
        ws = self._new_ws()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, x=0, y=0, z=0, fields={0x03: 0}))
        ws.update_object(create_block(2, object_type=uo.TYPEID_UNIT, x=1, y=0, z=0, fields={0x03: 6368}))
        ws.apply_creature_query_response({"entry": 6368, "found": True, "name": "Cat",
                                           "subname": "", "rank": "normal", "creature_type_name": "beast"})
        snap = ws.snapshot(max_range=50)
        self.assertEqual(snap["nearby_units"][0]["name"], "Cat")

def spline_movement_block(guid, x, y, z, destination, duration, time_passed=0):
    """`duration`/`time_passed` are milliseconds, matching the real wire
    values agent.update_object._parse_create_object_spline_block produces."""
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_MOVEMENT, guid=guid,
        movement={"update_flags": uo.UPDATEFLAG_LIVING, "move_flags": uo.MOVEMENTFLAG_SPLINE_ENABLED,
                  "x": x, "y": y, "z": z, "o": 0.0,
                  "spline": {"destination": destination, "duration": duration, "time_passed": time_passed}})


def living_movement_block(guid, x, y, z, move_flags=0):
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_MOVEMENT, guid=guid,
        movement={"update_flags": uo.UPDATEFLAG_LIVING, "move_flags": move_flags, "x": x, "y": y, "z": z, "o": 0.0})


class SplineInterpolationTest(unittest.TestCase):
    """UM-64: NPC/player positions interpolate toward an in-flight spline's
    destination between update-object/MONSTER_MOVE ticks."""

    def test_spline_movement_block_sets_spline_state(self):
        ws = per.WorldState()
        ws.set_my_map(530)
        ws.update_object(create_block(1, x=0, y=0, z=0))
        ws.update_object(spline_movement_block(1, 0.0, 0.0, 0.0, (10.0, 0.0, 0.0), duration=2000))
        obj = ws.get_object(1)
        self.assertIsNotNone(obj.spline)
        self.assertEqual(obj.spline["destination"], (10.0, 0.0, 0.0))

    def test_current_position_interpolates_halfway(self):
        ws = per.WorldState()
        ws.set_my_map(530)
        ws.update_object(create_block(1, x=0, y=0, z=0))
        with mock.patch("agent.perception.time.monotonic", return_value=100.0):
            ws.update_object(spline_movement_block(1, 0.0, 0.0, 0.0, (10.0, 0.0, 0.0), duration=2000))
        obj = ws.get_object(1)
        with mock.patch("agent.perception.time.monotonic", return_value=101.0):
            pos = obj.current_position()
        self.assertAlmostEqual(pos[1], 5.0)

    def test_current_position_clamps_at_destination_after_duration(self):
        ws = per.WorldState()
        ws.set_my_map(530)
        ws.update_object(create_block(1, x=0, y=0, z=0))
        with mock.patch("agent.perception.time.monotonic", return_value=100.0):
            ws.update_object(spline_movement_block(1, 0.0, 0.0, 0.0, (10.0, 0.0, 0.0), duration=2000))
        obj = ws.get_object(1)
        with mock.patch("agent.perception.time.monotonic", return_value=1000.0):
            pos = obj.current_position()
        self.assertEqual(pos[1:4], (10.0, 0.0, 0.0))

    def test_fresh_living_block_without_spline_clears_it(self):
        ws = per.WorldState()
        ws.set_my_map(530)
        ws.update_object(create_block(1, x=0, y=0, z=0))
        ws.update_object(spline_movement_block(1, 0.0, 0.0, 0.0, (10.0, 0.0, 0.0), duration=2000))
        ws.update_object(living_movement_block(1, 3.0, 0.0, 0.0))
        self.assertIsNone(ws.get_object(1).spline)

    def test_apply_monster_move_starts_spline(self):
        ws = per.WorldState()
        ws.set_my_map(530)
        ws.update_object(create_block(1, x=0, y=0, z=0))
        ws.apply_monster_move({"mover_guid": 1, "move_type": uo.MONSTER_MOVE_NORMAL,
                                "pos": (0.0, 0.0, 0.0), "destination": (10.0, 0.0, 0.0), "move_time": 2000})
        obj = ws.get_object(1)
        self.assertIsNotNone(obj.spline)
        self.assertEqual(obj.spline["destination"], (10.0, 0.0, 0.0))

    def test_apply_monster_move_stop_clears_spline(self):
        ws = per.WorldState()
        ws.set_my_map(530)
        ws.update_object(create_block(1, x=0, y=0, z=0))
        ws.apply_monster_move({"mover_guid": 1, "move_type": uo.MONSTER_MOVE_NORMAL,
                                "pos": (0.0, 0.0, 0.0), "destination": (10.0, 0.0, 0.0), "move_time": 2000})
        ws.apply_monster_move({"mover_guid": 1, "move_type": uo.MONSTER_MOVE_STOP, "pos": (10.0, 0.0, 0.0)})
        self.assertIsNone(ws.get_object(1).spline)

    def test_apply_monster_move_unknown_guid_ignored_and_counted(self):
        ws = per.WorldState()
        ws.apply_monster_move({"mover_guid": 99, "move_type": uo.MONSTER_MOVE_STOP, "pos": (0.0, 0.0, 0.0)})
        self.assertIsNone(ws.get_object(99))
        self.assertEqual(ws.unknown_field_updates, 1)

    def test_apply_movement_info_updates_position(self):
        ws = per.WorldState()
        ws.set_my_map(530)
        ws.update_object(create_block(1, x=0, y=0, z=0))
        ws.apply_movement_info(1, {"move_flags": 0, "x": 5.0, "y": 6.0, "z": 7.0, "o": 0.0})
        self.assertEqual(ws.get_object(1).position, (530, 5.0, 6.0, 7.0, 0.0))

    def test_apply_movement_info_unknown_guid_ignored_and_counted(self):
        ws = per.WorldState()
        ws.apply_movement_info(99, {"move_flags": 0, "x": 0.0, "y": 0.0, "z": 0.0})
        self.assertIsNone(ws.get_object(99))
        self.assertEqual(ws.unknown_field_updates, 1)


class ObjectInfoHelpersTest(unittest.TestCase):
    def test_is_player(self):
        obj = per.ObjectInfo(guid=1, object_type="player")
        self.assertTrue(obj.is_player())
        self.assertFalse(per.ObjectInfo(guid=2, object_type="unit").is_player())

    def test_is_dead(self):
        self.assertTrue(per.ObjectInfo(guid=1, health=0).is_dead())
        self.assertFalse(per.ObjectInfo(guid=1, health=1).is_dead())

    def test_is_lootable(self):
        obj = per.ObjectInfo(guid=1, dynamic_flags=per.UNIT_DYNFLAG_LOOTABLE)
        self.assertTrue(obj.is_lootable())
        self.assertFalse(per.ObjectInfo(guid=2, dynamic_flags=0).is_lootable())
        self.assertFalse(per.ObjectInfo(guid=3).is_lootable())

    def test_npc_flag_helpers(self):
        obj = per.ObjectInfo(guid=1, npc_flags=per.UNIT_NPC_FLAG_QUESTGIVER | per.UNIT_NPC_FLAG_GOSSIP)
        self.assertTrue(obj.is_quest_giver())
        self.assertTrue(obj.is_gossip())
        self.assertFalse(obj.is_vendor())
        self.assertFalse(obj.is_trainer())

    def test_distance_to_same_map(self):
        obj = per.ObjectInfo(guid=1, position=(530, 0.0, 0.0, 0.0, 0.0))
        self.assertAlmostEqual(obj.distance_to((530, 3.0, 4.0, 0.0)), 5.0)

    def test_distance_to_different_map_is_none(self):
        obj = per.ObjectInfo(guid=1, position=(530, 0.0, 0.0, 0.0, 0.0))
        self.assertIsNone(obj.distance_to((1, 3.0, 4.0, 0.0)))

    def test_distance_to_unknown_position_is_none(self):
        obj = per.ObjectInfo(guid=1)
        self.assertIsNone(obj.distance_to((530, 0.0, 0.0, 0.0)))

    def test_is_hostile_to_unknown_when_factions_missing(self):
        obj = per.ObjectInfo(guid=1, faction=None)
        self.assertIsNone(obj.is_hostile_to(14))
        self.assertIsNone(per.ObjectInfo(guid=1, faction=14).is_hostile_to(None))

    def test_is_hostile_to_different_faction(self):
        obj = per.ObjectInfo(guid=1, faction=14)
        self.assertTrue(obj.is_hostile_to(1))
        self.assertFalse(obj.is_hostile_to(14))


class SnapshotTest(unittest.TestCase):
    def _ws_with_objects(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, x=0, y=0, z=0,
                                       fields={0x03: 0}))
        ws.update_object(create_block(2, object_type=uo.TYPEID_UNIT, x=10, y=0, z=0,
                                       fields={0x03: 6368, 0x18: 1, 0x20: 1, 0x36: 1}))
        ws.update_object(create_block(3, object_type=uo.TYPEID_PLAYER, x=5, y=0, z=0,
                                       fields={0x03: 0}))
        ws.update_object(create_block(4, object_type=uo.TYPEID_GAMEOBJECT, x=1000, y=0, z=0,
                                       fields={0x03: 181646}))
        return ws

    def test_snapshot_shape_and_categories(self):
        ws = self._ws_with_objects()
        snap = ws.snapshot(max_range=50)
        self.assertEqual(snap["position"], {"map": 530, "x": 0.0, "y": 0.0, "z": 0.0})
        self.assertEqual(len(snap["nearby_units"]), 1)
        self.assertEqual(snap["nearby_units"][0]["entry"], 6368)
        self.assertEqual(len(snap["nearby_players"]), 1)
        self.assertEqual(snap["nearby_players"][0]["guid"], 3)
        # gameobject at distance 1000 is outside max_range=50
        self.assertEqual(snap["nearby_objects"], [])

    def test_snapshot_excludes_self(self):
        ws = self._ws_with_objects()
        snap = ws.snapshot(max_range=50)
        all_guids = [o["guid"] for bucket in ("nearby_units", "nearby_players", "nearby_objects")
                     for o in snap[bucket]]
        self.assertNotIn(1, all_guids)

    def test_snapshot_sorted_by_distance(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, x=0, y=0, z=0))
        ws.update_object(create_block(2, object_type=uo.TYPEID_UNIT, x=30, y=0, z=0, fields={0x03: 1}))
        ws.update_object(create_block(3, object_type=uo.TYPEID_UNIT, x=10, y=0, z=0, fields={0x03: 2}))
        snap = ws.snapshot(max_range=50)
        self.assertEqual([u["entry"] for u in snap["nearby_units"]], [2, 1])

    def test_snapshot_respects_limit(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, x=0, y=0, z=0))
        for i in range(2, 8):
            ws.update_object(create_block(i, object_type=uo.TYPEID_UNIT, x=float(i), y=0, z=0, fields={0x03: i}))
        snap = ws.snapshot(max_range=50, limit=3)
        self.assertEqual(len(snap["nearby_units"]), 3)

    def test_snapshot_without_position_returns_empty_buckets(self):
        ws = per.WorldState()
        snap = ws.snapshot()
        self.assertIsNone(snap["position"])
        self.assertEqual(snap["nearby_units"], [])

    def test_snapshot_my_position_override(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        ws.update_object(create_block(2, object_type=uo.TYPEID_UNIT, x=0, y=0, z=0, fields={0x03: 6368}))
        snap = ws.snapshot(my_position=(530, 0.0, 0.0, 0.0, 0.0), max_range=50)
        self.assertEqual(snap["position"]["map"], 530)
        self.assertEqual(len(snap["nearby_units"]), 1)


class RealFixtureIntegrationTest(unittest.TestCase):
    """Feeds a real captured payload through the full parse -> WorldState
    pipeline (agent/tests/fixtures/update_object/README.md has the
    provenance and expected values used here)."""

    def test_busy_zone_populates_world_state(self):
        data = (FIXTURES_DIR / "busy_zone.bin").read_bytes()
        blocks = uo.parse_update_object(data)
        ws = per.WorldState()
        ws.set_my_map(530)
        for block in blocks:
            ws.update_object(block)

        self.assertEqual(len(ws.get_objects()), 74)
        self.assertEqual(ws.unknown_field_updates, 0)

        entries = {o.entry for o in ws.get_objects().values()}
        self.assertIn(37543, entries)   # "[DND] Shaker", level 60
        self.assertIn(182325, entries)  # "Farstrider Square" gameobject

        shaker = next(o for o in ws.get_objects().values() if o.entry == 37543)
        self.assertEqual(shaker.level, 60)
        self.assertEqual(shaker.health, shaker.max_health)
        self.assertEqual(shaker.object_type, "unit")
        self.assertIsNotNone(shaker.position)

        snap = ws.snapshot(my_position=(530, 9487.69, -7279.2, 14.29, 0.0), max_range=500)
        self.assertGreater(len(snap["nearby_units"]) + len(snap["nearby_objects"]), 0)


if __name__ == '__main__':
    unittest.main()
