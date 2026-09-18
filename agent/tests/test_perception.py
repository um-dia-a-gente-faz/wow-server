"""Unit tests for agent.perception.WorldState against synthetic UpdateBlocks
(agent.update_object.UpdateBlock), so no byte-level parsing is needed here —
that's agent/tests/test_update_object_parser.py's job."""

import pathlib
import tempfile
import unittest
from unittest import mock

from agent import perception as per
from agent import trade
from agent import update_fields as uf
from agent import update_fields as uo_fields
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

    def test_partial_power_update_keeps_other_powers(self):
        # UM-70: a VALUES update reporting only the slot(s) that changed
        # (e.g. focus regen) must not wipe out previously-known powers
        # (e.g. mana) by replacing the whole dict.
        ws = per.WorldState()
        ws.update_object(create_block(1, fields={
            uf.UNIT_FIELD_POWER1: 145, uf.UNIT_FIELD_POWER1 + 2: 90,
            uf.UNIT_FIELD_MAXPOWER1: 145, uf.UNIT_FIELD_MAXPOWER1 + 2: 100,
        }))
        # Only focus (slot 2) changed this tick.
        ws.update_object(values_block(1, {uf.UNIT_FIELD_POWER1 + 2: 100}))
        obj = ws.get_object(1)
        self.assertEqual(obj.power, {"mana": 145, "focus": 100})
        self.assertEqual(obj.max_power, {"mana": 145, "focus": 100})

    def test_power_filtered_to_unit_own_power_type_once_known(self):
        # UM-83: a hunter's raw fields carry baseline values for power types
        # it doesn't use (server template noise). power_type=0 (mana) means
        # only "mana" should survive in power/max_power.
        ws = per.WorldState()
        ws.update_object(create_block(1, fields={
            uf.UNIT_FIELD_BYTES_0: 0,  # race/class/gender all 0, power_type (byte 3) = 0 = mana
            uf.UNIT_FIELD_POWER1: 145, uf.UNIT_FIELD_POWER1 + 3: 100,        # mana=145, energy=100 (phantom)
            uf.UNIT_FIELD_MAXPOWER1: 145, uf.UNIT_FIELD_MAXPOWER1 + 1: 1000,  # max mana=145, max rage=1000 (phantom)
            uf.UNIT_FIELD_MAXPOWER1 + 3: 100, uf.UNIT_FIELD_MAXPOWER1 + 6: 1000,  # max energy/runic_power (phantom)
        }))
        obj = ws.get_object(1)
        self.assertEqual(obj.power, {"mana": 145})
        self.assertEqual(obj.max_power, {"mana": 145})

    def test_power_kept_unfiltered_until_power_type_known(self):
        # No UNIT_FIELD_BYTES_0 in this update, so power_type is still
        # unknown — don't drop data we can't yet judge as unusable.
        ws = per.WorldState()
        ws.update_object(create_block(1, fields={
            uf.UNIT_FIELD_POWER1: 145, uf.UNIT_FIELD_POWER1 + 3: 100,
        }))
        obj = ws.get_object(1)
        self.assertEqual(obj.power, {"mana": 145, "energy": 100})

    def test_power_filtered_once_power_type_arrives_in_a_later_update(self):
        ws = per.WorldState()
        ws.update_object(create_block(1, fields={
            uf.UNIT_FIELD_POWER1: 145, uf.UNIT_FIELD_POWER1 + 3: 100,
        }))
        ws.update_object(values_block(1, {uf.UNIT_FIELD_BYTES_0: 0}))
        obj = ws.get_object(1)
        self.assertEqual(obj.power, {"mana": 145})

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
        ws.apply_gameobject_query_response({"entry": 181646, "found": True, "name": "Ship", "type": 15})
        self.assertEqual(ws.get_object(1).name, "Ship")
        self.assertEqual(ws.get_object(1).gameobject_type, 15)

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


class MyServerPositionTest(unittest.TestCase):
    """UM-38: my_server_position tracks only what the server actually said
    about our own position — distinct from ObjectInfo.position, which
    update_my_position_from_simulation() also writes for other perception
    consumers. Movement's stuck detection needs the untouched value to
    compare our simulation against something other than itself."""

    def test_create_with_position_sets_it(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.update_object(create_block(1, x=5.0, y=6.0, z=7.0))
        self.assertEqual(ws.my_server_position[1:4], (5.0, 6.0, 7.0))

    def test_movement_block_with_position_updates_it(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.update_object(create_block(1, x=0.0, y=0.0, z=0.0))
        ws.update_object(movement_block(1, 50.0, 50.0, 0.0))
        self.assertEqual(ws.my_server_position[1:4], (50.0, 50.0, 0.0))

    def test_other_objects_never_set_it(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.update_object(create_block(2, x=9.0, y=9.0, z=9.0))
        self.assertIsNone(ws.my_server_position)

    def test_simulation_mirror_does_not_touch_it(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        ws.update_object(create_block(1, x=0.0, y=0.0, z=0.0))
        real_server_pos = ws.my_server_position
        # Movement's Mover calls this every tick while simulating; it must
        # update ObjectInfo.position (for distance_to/snapshot) but never
        # clobber my_server_position — that's the whole point of the field.
        ws.update_my_position_from_simulation((530, 123.0, 456.0, 0.0, 1.0))
        self.assertEqual(ws.get_my_object().position, (530, 123.0, 456.0, 0.0, 1.0))
        self.assertEqual(ws.my_server_position, real_server_pos)

    def test_values_only_block_does_not_touch_it(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.update_object(create_block(1, x=1.0, y=2.0, z=3.0, fields={0x03: 0}))
        # A VALUES-only update (e.g. health regen) has no position component.
        ws.update_object(values_block(1, {0x18: 50}))
        self.assertEqual(ws.my_server_position[1:4], (1.0, 2.0, 3.0))


class ObjectInfoHelpersTest(unittest.TestCase):
    def test_is_player(self):
        obj = per.ObjectInfo(guid=1, object_type="player")
        self.assertTrue(obj.is_player())
        self.assertFalse(per.ObjectInfo(guid=2, object_type="unit").is_player())

    def test_is_dead(self):
        self.assertTrue(per.ObjectInfo(guid=1, health=0).is_dead())
        self.assertFalse(per.ObjectInfo(guid=1, health=1).is_dead())

    def test_is_ghost(self):
        self.assertTrue(per.ObjectInfo(guid=1, player_flags=per.PLAYER_FLAGS_GHOST).is_ghost())
        self.assertTrue(per.ObjectInfo(guid=1, player_flags=per.PLAYER_FLAGS_GHOST | 0x20).is_ghost())
        self.assertFalse(per.ObjectInfo(guid=1, player_flags=0x20).is_ghost())
        self.assertFalse(per.ObjectInfo(guid=1, player_flags=None).is_ghost())

    def test_is_spirit_healer(self):
        obj = per.ObjectInfo(guid=1, npc_flags=per.UNIT_NPC_FLAG_SPIRITHEALER)
        self.assertTrue(obj.is_spirit_healer())
        self.assertFalse(per.ObjectInfo(guid=2, npc_flags=0).is_spirit_healer())

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

    def test_snapshot_exposes_is_dead_is_ghost_and_corpse_position(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, x=0, y=0, z=0,
                                       fields={0x03: 0, uf.UNIT_FIELD_HEALTH: 1,
                                               uf.PLAYER_FLAGS: per.PLAYER_FLAGS_GHOST}))
        snap = ws.snapshot(corpse_position=(530, 12.0, 34.0, 5.0))
        self.assertFalse(snap["is_dead"])  # health=1 (ghost), not 0
        self.assertTrue(snap["is_ghost"])
        self.assertEqual(snap["corpse_position"], {"map": 530, "x": 12.0, "y": 34.0, "z": 5.0})

    def test_snapshot_defaults_is_dead_is_ghost_false_and_corpse_position_none(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, x=0, y=0, z=0, fields={0x03: 0}))
        snap = ws.snapshot()
        self.assertFalse(snap["is_dead"])
        self.assertFalse(snap["is_ghost"])
        self.assertIsNone(snap["corpse_position"])


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


class NpcUiStateTest(unittest.TestCase):
    """UM-40: gossip/vendor/trainer windows land in WorldState.ui_state and
    surface through snapshot()'s 'window' key."""

    def test_snapshot_window_defaults_to_none(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, x=0, y=0, z=0))
        self.assertIsNone(ws.snapshot()["window"])

    def test_gossip_message_opens_window_and_queues_text(self):
        ws = per.WorldState()
        data = {"npc_guid": 5, "menu_id": 1, "text_id": 999, "options": [], "quests": []}
        ws.apply_gossip_message(data)
        window = ws.get_ui_state()
        self.assertEqual(window["kind"], "gossip")
        self.assertEqual(window["npc_guid"], 5)
        self.assertNotIn("body_text", window)  # not cached yet
        self.assertEqual(ws.npc_texts.drain(), [(999, 5)])

    def test_npc_text_update_backfills_open_gossip_window(self):
        ws = per.WorldState()
        ws.apply_gossip_message({"npc_guid": 5, "menu_id": 1, "text_id": 999,
                                  "options": [], "quests": []})
        ws.apply_npc_text_update({"text_id": 999, "found": True,
                                   "options": [{"text0": "Welcome!", "text1": "", "probability": 1.0,
                                                "language": 0, "emotes": []}]})
        self.assertEqual(ws.get_ui_state()["body_text"], "Welcome!")

    def test_gossip_complete_closes_window(self):
        ws = per.WorldState()
        ws.apply_gossip_message({"npc_guid": 5, "menu_id": 1, "text_id": 999,
                                  "options": [], "quests": []})
        ws.apply_gossip_complete()
        self.assertIsNone(ws.get_ui_state())

    def test_list_inventory_opens_vendor_window(self):
        ws = per.WorldState()
        ws.apply_list_inventory({"vendor_guid": 5, "items": [], "reason": None})
        self.assertEqual(ws.get_ui_state()["kind"], "vendor")
        self.assertEqual(ws.snapshot()["window"]["vendor_guid"], 5)

    def test_trainer_list_opens_trainer_window(self):
        ws = per.WorldState()
        ws.apply_trainer_list({"trainer_guid": 5, "trainer_type": 0, "spells": [], "greeting": ""})
        self.assertEqual(ws.get_ui_state()["kind"], "trainer")

    def test_close_window_clears_state(self):
        ws = per.WorldState()
        ws.apply_list_inventory({"vendor_guid": 5, "items": [], "reason": None})
        ws.close_window()
        self.assertIsNone(ws.get_ui_state())


class MailboxStateTest(unittest.TestCase):
    """UM-60: is_mailbox() detection, world.mailbox's open/fill flow, and
    has_new_mail."""

    def test_is_mailbox_from_gameobject_type(self):
        ws = per.WorldState()
        ws.update_object(create_block(5, object_type=uo.TYPEID_GAMEOBJECT, fields={0x03: 187640}))
        ws.apply_gameobject_query_response({"entry": 187640, "found": True, "name": "Mailbox",
                                             "type": per.GAMEOBJECT_TYPE_MAILBOX})
        self.assertTrue(ws.get_object(5).is_mailbox())

    def test_is_mailbox_resolved_from_a_pre_warmed_name_cache(self):
        # Regression test (found live testing, 2026-09-17): NameCache
        # persists gameobject templates to disk across runs. A gameobject
        # whose entry is already cached (this test simulates that by
        # pre-populating names.gameobjects before the object is even
        # perceived) must still get gameobject_type backfilled — a second
        # live agent process, on its very first perception of the same
        # mailbox, saw is_mailbox() stuck at False forever because only
        # .name was being backfilled from the cached branch.
        ws = per.WorldState()
        ws.names.gameobjects[182363] = {"entry": 182363, "found": True, "name": "Mailbox",
                                          "type": per.GAMEOBJECT_TYPE_MAILBOX}
        ws.update_object(create_block(5, object_type=uo.TYPEID_GAMEOBJECT, fields={0x03: 182363}))
        obj = ws.get_object(5)
        self.assertEqual(obj.name, "Mailbox")
        self.assertTrue(obj.is_mailbox())

    def test_non_mailbox_gameobject_is_not_a_mailbox(self):
        ws = per.WorldState()
        ws.update_object(create_block(5, object_type=uo.TYPEID_GAMEOBJECT, fields={0x03: 1}))
        ws.apply_gameobject_query_response({"entry": 1, "found": True, "name": "Chair", "type": 6})
        self.assertFalse(ws.get_object(5).is_mailbox())

    def test_unresolved_gameobject_type_is_not_a_mailbox_yet(self):
        ws = per.WorldState()
        ws.update_object(create_block(5, object_type=uo.TYPEID_GAMEOBJECT, fields={0x03: 187640}))
        self.assertFalse(ws.get_object(5).is_mailbox())

    def test_is_mailbox_from_npc_flag(self):
        ws = per.WorldState()
        ws.update_object(create_block(5, object_type=uo.TYPEID_UNIT,
                                       fields={uf.UNIT_NPC_FLAGS: per.UNIT_NPC_FLAG_MAILBOX}))
        self.assertTrue(ws.get_object(5).is_mailbox())

    def test_open_mailbox_request_sets_pending_state(self):
        ws = per.WorldState()
        ws.open_mailbox_request(0x1234)
        mailbox = ws.get_mailbox()
        self.assertEqual(mailbox["mailbox_guid"], 0x1234)
        self.assertIsNone(mailbox["mails"])

    def test_mail_list_result_fills_pending_request_and_resolves_item_name(self):
        ws = per.WorldState()
        ws.items.items[20812] = {"entry": 20812, "name": "Tattered Pelt", "found": True}
        ws.open_mailbox_request(0x1234)
        ws.apply_mail_list_result({"total_records": 1, "mails": [{
            "mail_id": 1, "sender_type": 0, "sender_guid": 3, "alt_sender_id": None,
            "cod": 0, "package_id": 0, "stationery_id": 41, "money": 100, "flags": 0,
            "is_read": False, "days_left": 29.0, "mail_template_id": 0,
            "subject": "Hi", "body": "", "attachments": [{"position": 0, "attach_id": 22,
                                                            "entry": 20812, "count": 1}],
        }]})
        mailbox = ws.get_mailbox()
        self.assertEqual(mailbox["mailbox_guid"], 0x1234)  # kept from the request
        self.assertEqual(mailbox["total_records"], 1)
        self.assertEqual(mailbox["mails"][0]["attachments"][0]["name"], "Tattered Pelt")

    def test_mail_list_result_with_no_pending_request_is_ignored(self):
        ws = per.WorldState()
        ws.apply_mail_list_result({"total_records": 0, "mails": []})
        self.assertIsNone(ws.get_mailbox())

    def test_mail_list_result_queues_item_query_for_unresolved_entry(self):
        ws = per.WorldState()
        ws.open_mailbox_request(0x1234)
        ws.apply_mail_list_result({"total_records": 1, "mails": [{
            "mail_id": 1, "sender_type": 0, "sender_guid": 3, "alt_sender_id": None,
            "cod": 0, "package_id": 0, "stationery_id": 41, "money": 0, "flags": 0,
            "is_read": False, "days_left": 29.0, "mail_template_id": 0,
            "subject": "Hi", "body": "", "attachments": [{"position": 0, "attach_id": 22,
                                                            "entry": 99999, "count": 1}],
        }]})
        self.assertIn(99999, ws.items.drain())

    def test_received_mail_sets_flag(self):
        ws = per.WorldState()
        self.assertFalse(ws.snapshot()["has_new_mail"])
        ws.apply_received_mail({"delay": 0.0})
        self.assertTrue(ws.snapshot()["has_new_mail"])

    def test_opening_mailbox_clears_has_new_mail(self):
        ws = per.WorldState()
        ws.apply_received_mail({"delay": 0.0})
        ws.open_mailbox_request(0x1234)
        ws.apply_mail_list_result({"total_records": 0, "mails": []})
        self.assertFalse(ws.snapshot()["has_new_mail"])

class TradeStateTest(unittest.TestCase):
    """UM-59: world.trade's phase machine, driven by agent.trade.
    parse_trade_status/parse_trade_status_extended shaped dicts, and the
    optimistic local mutators actions.py calls (the server never echoes our
    own offer back — see agent/trade.py's docstring)."""

    def test_defaults_to_none(self):
        ws = per.WorldState()
        self.assertIsNone(ws.get_trade())
        self.assertIsNone(ws.snapshot()["trade"])

    def test_start_trade_request_sets_requested_phase(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        trade = ws.get_trade()
        self.assertEqual(trade["phase"], "requested")
        self.assertEqual(trade["partner_guid"], 0x999)
        self.assertTrue(trade["initiated_by_me"])

    def test_incoming_begin_trade_opens_request_and_fires_event(self):
        ws = per.WorldState()
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_BEGIN_TRADE,
                                         "status_name": "begin_trade", "trader_guid": 0x555})
        self.assertEqual(result, ("trade_requested", {"by": 0x555}))
        trade_state = ws.get_trade()
        self.assertEqual(trade_state["phase"], "requested")
        self.assertEqual(trade_state["partner_guid"], 0x555)
        self.assertFalse(trade_state["initiated_by_me"])

    def test_status_with_no_active_trade_is_ignored(self):
        ws = per.WorldState()
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_BACK_TO_TRADE,
                                         "status_name": "back_to_trade"})
        self.assertIsNone(result)
        self.assertIsNone(ws.get_trade())

    def test_open_window_advances_phase(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_OPEN_WINDOW,
                                         "status_name": "open_window"})
        self.assertIsNone(result)
        self.assertEqual(ws.get_trade()["phase"], "open")

    def test_trade_accept_sets_their_accepted(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        ws.apply_trade_status({"status": trade.TRADE_STATUS_TRADE_ACCEPT, "status_name": "trade_accept"})
        self.assertTrue(ws.get_trade()["their_accepted"])
        self.assertFalse(ws.get_trade()["my_accepted"])

    def test_back_to_trade_resets_both_accepted_flags(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        ws.set_my_trade_accepted()
        ws.apply_trade_status({"status": trade.TRADE_STATUS_TRADE_ACCEPT, "status_name": "trade_accept"})
        self.assertTrue(ws.get_trade()["my_accepted"])
        self.assertTrue(ws.get_trade()["their_accepted"])
        ws.apply_trade_status({"status": trade.TRADE_STATUS_BACK_TO_TRADE, "status_name": "back_to_trade"})
        self.assertFalse(ws.get_trade()["my_accepted"])
        self.assertFalse(ws.get_trade()["their_accepted"])

    def test_not_on_taplist_rejects_without_closing_trade(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_NOT_ON_TAPLIST,
                                         "status_name": "not_on_taplist", "slot": 2})
        self.assertEqual(result, ("trade_offer_rejected", {"slot": 2, "reason": "not_on_taplist"}))
        self.assertIsNotNone(ws.get_trade())  # window stays open

    def test_trade_complete_fires_summary_and_clears_state(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        ws.set_my_trade_gold(500)
        ws.set_my_trade_item(0, {"entry": 6948, "name": "Buckler"})
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_TRADE_COMPLETE,
                                         "status_name": "trade_complete"})
        kind, fields = result
        self.assertEqual(kind, "trade_completed")
        self.assertEqual(fields["summary"]["my_gold"], 500)
        self.assertEqual(fields["summary"]["my_items"], [{"entry": 6948, "name": "Buckler"}])
        self.assertIsNone(ws.get_trade())

    def test_cancel_fires_trade_cancelled_and_clears_state(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_TRADE_CANCELED,
                                         "status_name": "trade_canceled"})
        self.assertEqual(result[0], "trade_cancelled")
        self.assertEqual(result[1]["reason"], "trade_canceled")
        self.assertIsNone(ws.get_trade())

    def test_close_window_cancel_uses_result_name_as_reason(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_CLOSE_WINDOW,
                                         "status_name": "close_window", "result": 29,
                                         "result_name": "not_enough_money"})
        self.assertEqual(result, ("trade_cancelled",
                                   {"reason": "not_enough_money",
                                    "summary": {"partner_guid": 0x999, "my_gold": 0, "their_gold": 0,
                                                "my_items": [], "their_items": []}}))
        self.assertIsNone(ws.get_trade())

    def test_extended_updates_their_offer_and_resolves_cached_item_name(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        ws.items.items[6948] = {"entry": 6948, "name": "Weather Beaten Buckler", "found": True}
        changed = ws.apply_trade_status_extended({
            "is_trader_data": True, "money": 250,
            "items": {0: {"slot": 0, "entry": 6948, "count": 1}},
        })
        self.assertEqual(changed["gold"], 250)
        self.assertEqual(changed["items"][0]["name"], "Weather Beaten Buckler")
        trade_state = ws.get_trade()
        self.assertEqual(trade_state["their_gold"], 250)
        self.assertEqual(trade_state["their_items"][0]["name"], "Weather Beaten Buckler")

    def test_extended_queues_item_query_for_unresolved_entry(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        ws.apply_trade_status_extended({
            "is_trader_data": True, "money": 0,
            "items": {0: {"slot": 0, "entry": 12345, "count": 1}},
        })
        self.assertIn(12345, ws.items.drain())

    def test_extended_own_data_flag_is_ignored(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        changed = ws.apply_trade_status_extended({"is_trader_data": False, "money": 999, "items": {}})
        self.assertIsNone(changed)
        self.assertEqual(ws.get_trade()["their_gold"], 0)

    def test_extended_with_no_active_trade_is_ignored(self):
        ws = per.WorldState()
        changed = ws.apply_trade_status_extended({"is_trader_data": True, "money": 999, "items": {}})
        self.assertIsNone(changed)

    def test_my_trade_item_mutators(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        ws.set_my_trade_item(1, {"entry": 159, "name": "Refreshing Spring Water"})
        self.assertEqual(ws.get_trade()["my_items"][1]["entry"], 159)
        ws.clear_my_trade_item(1)
        self.assertNotIn(1, ws.get_trade()["my_items"])

    def test_mutators_are_no_ops_without_an_active_trade(self):
        ws = per.WorldState()
        ws.set_my_trade_item(0, {"entry": 1})  # no trade pending — must not raise
        ws.set_my_trade_gold(100)
        ws.set_my_trade_accepted()
        self.assertIsNone(ws.get_trade())


class InventoryModelTest(unittest.TestCase):
    """UM-42: building session.inventory/equipment from CREATE blocks (self
    player + item objects) and the item-template cache."""

    def _make_self(self, ws, guid, slot_guids):
        raw = {}
        for slot, item_guid in slot_guids.items():
            if slot < 19 + 4:
                base = uo_fields.PLAYER_FIELD_INV_SLOT_HEAD + slot * 2
            else:
                base = uo_fields.PLAYER_FIELD_PACK_SLOT_1 + (slot - 23) * 2
            raw[base] = item_guid & 0xFFFFFFFF
            raw[base + 1] = item_guid >> 32
        ws.set_my_guid(guid)
        ws.update_object(create_block(guid, object_type=uo.TYPEID_PLAYER, fields=raw))

    def test_equipment_and_inventory_from_item_objects(self):
        ws = per.WorldState()
        me_guid = 0x1
        head_item_guid = 0xF120000000000005
        food_item_guid = 0xF120000000000006
        self._make_self(ws, me_guid, {0: head_item_guid, 23: food_item_guid})

        ws.update_object(create_block(
            head_item_guid, object_type=uo.TYPEID_ITEM,
            fields={uf_object_entry(): 1234, uo_fields.ITEM_FIELD_STACK_COUNT: 1}))
        ws.update_object(create_block(
            food_item_guid, object_type=uo.TYPEID_ITEM,
            fields={uf_object_entry(): 159, uo_fields.ITEM_FIELD_STACK_COUNT: 4}))

        equipment, inventory = ws.build_equipment_and_inventory()
        self.assertEqual(equipment[0]["guid"], head_item_guid)
        self.assertEqual(equipment[0]["entry"], 1234)

        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["guid"], food_item_guid)
        self.assertEqual(inventory[0]["entry"], 159)
        self.assertEqual(inventory[0]["count"], 4)
        self.assertEqual(inventory[0]["slot"], 23)

    def test_missing_item_object_still_reports_bare_guid(self):
        ws = per.WorldState()
        me_guid = 0x1
        item_guid = 0xF120000000000099
        self._make_self(ws, me_guid, {23: item_guid})
        # No CREATE block for the item itself has arrived yet.
        equipment, inventory = ws.build_equipment_and_inventory()
        self.assertEqual(inventory, [{"guid": item_guid, "slot": 23}])

    def test_no_self_object_yet(self):
        ws = per.WorldState()
        ws.set_my_guid(0x1)
        self.assertEqual(ws.build_equipment_and_inventory(), ({}, []))

    def test_snapshot_includes_equipment_and_inventory_keys(self):
        ws = per.WorldState()
        ws.set_my_map(0)
        self._make_self(ws, 0x1, {})
        snap = ws.snapshot(my_position=(0, 0.0, 0.0, 0.0, 0.0))
        self.assertIn("equipment", snap)
        self.assertIn("inventory", snap)

    def test_item_query_response_backfills_name(self):
        ws = per.WorldState()
        item_guid = 0xF120000000000042
        ws.update_object(create_block(
            item_guid, object_type=uo.TYPEID_ITEM,
            fields={uf_object_entry(): 159, uo_fields.ITEM_FIELD_STACK_COUNT: 1}))
        ws.apply_item_query_response({"entry": 159, "found": True, "name": "Tough Jerky"})
        self.assertEqual(ws.get_object(item_guid).name, "Tough Jerky")


def uf_object_entry():
    return uo_fields.OBJECT_FIELD_ENTRY


if __name__ == '__main__':
    unittest.main()
