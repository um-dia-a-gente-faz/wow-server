"""Unit tests for agent.reflexes.rest: the rest reflex (UM-43). Fake
session/world only, no network, no real sleeping."""

import struct
import unittest
from types import SimpleNamespace

from agent import actions as ac
from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo
from agent.reflexes import rest as rf


def fake_session(guid=0xF130000000000099):
    sent = []
    sess = SimpleNamespace(player_guid=guid, events=[])
    sess._send_packet = lambda opcode, payload=b'': sent.append((opcode, payload))
    sess._sent = sent
    return sess


def world_with_self(guid, health, max_health, power=None, max_power=None,
                     power_type=None, unit_flags=None):
    world = per.WorldState()
    world.set_my_guid(guid)
    fields = {uf.UNIT_FIELD_HEALTH: health, uf.UNIT_FIELD_MAXHEALTH: max_health}
    if unit_flags is not None:
        fields[uf.UNIT_FIELD_FLAGS] = unit_flags
    if power_type is not None or power is not None:
        bytes0 = (power_type or 0) << 24
        fields[uf.UNIT_FIELD_BYTES_0] = bytes0
    if power is not None:
        fields[uf.UNIT_FIELD_POWER1] = power
    if max_power is not None:
        fields[uf.UNIT_FIELD_MAXPOWER1] = max_power
    world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid,
                                        object_type=uo.TYPEID_PLAYER,
                                        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION,
                                                  "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0},
                                        fields=fields))
    return world


def set_health(world, guid, health):
    world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=guid,
                                        fields={uf.UNIT_FIELD_HEALTH: health}))


def set_unit_flags(world, guid, flags):
    world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=guid,
                                        fields={uf.UNIT_FIELD_FLAGS: flags}))


class NeedsRestTest(unittest.TestCase):
    def test_false_when_object_unknown(self):
        self.assertFalse(rf.needs_rest(None))

    def test_false_above_thresholds(self):
        world = world_with_self(1, health=90, max_health=100)
        self.assertFalse(rf.needs_rest(world.get_my_object()))

    def test_true_when_hp_low(self):
        world = world_with_self(1, health=40, max_health=100)
        self.assertTrue(rf.needs_rest(world.get_my_object()))

    def test_true_when_mana_low(self):
        world = world_with_self(1, health=100, max_health=100,
                                 power=20, max_power=100, power_type=0)
        self.assertTrue(rf.needs_rest(world.get_my_object()))

    def test_false_when_low_power_but_not_mana_type(self):
        # e.g. a warrior sitting at 0 rage isn't "low on mana" — rage is
        # expected to be near-empty out of combat.
        world = world_with_self(1, health=100, max_health=100,
                                 power=0, max_power=100, power_type=1)
        self.assertFalse(rf.needs_rest(world.get_my_object()))

    def test_false_in_combat_even_if_hurt(self):
        world = world_with_self(1, health=10, max_health=100, unit_flags=rf.UNIT_FLAG_IN_COMBAT)
        self.assertFalse(rf.needs_rest(world.get_my_object()))


class RestReflexTickTest(unittest.TestCase):
    def test_tick_starts_resting_when_needed(self):
        sess = fake_session()
        world = world_with_self(sess.player_guid, health=30, max_health=100)
        reflex = rf.RestReflex()
        reflex.tick(sess, world)
        self.assertTrue(reflex.active)
        self.assertEqual(reflex.method, "sit")
        self.assertEqual(sess._sent, [(rf.CMSG_STANDSTATECHANGE, struct.pack('<I', rf.UNIT_STAND_STATE_SIT))])
        self.assertEqual(sess.events[-1]["kind"], "resting_started")

    def test_tick_is_noop_when_not_needed(self):
        sess = fake_session()
        world = world_with_self(sess.player_guid, health=100, max_health=100)
        reflex = rf.RestReflex()
        reflex.tick(sess, world)
        self.assertFalse(reflex.active)
        self.assertEqual(sess._sent, [])

    def test_tick_stops_once_recovered(self):
        sess = fake_session()
        world = world_with_self(sess.player_guid, health=30, max_health=100)
        reflex = rf.RestReflex()
        reflex.tick(sess, world)  # starts resting (sits)
        set_health(world, sess.player_guid, 95)
        reflex.tick(sess, world)  # notices recovery, stands back up
        self.assertFalse(reflex.active)
        self.assertEqual(sess._sent[-1], (rf.CMSG_STANDSTATECHANGE, struct.pack('<I', rf.UNIT_STAND_STATE_STAND)))
        self.assertEqual(sess.events[-1]["kind"], "resting_stopped")
        self.assertEqual(sess.events[-1]["reason"], "recovered")

    def test_tick_stops_on_aggro_even_if_still_hurt(self):
        sess = fake_session()
        world = world_with_self(sess.player_guid, health=30, max_health=100)
        reflex = rf.RestReflex()
        reflex.tick(sess, world)
        set_unit_flags(world, sess.player_guid, rf.UNIT_FLAG_IN_COMBAT)
        reflex.tick(sess, world)
        self.assertFalse(reflex.active)
        self.assertEqual(sess.events[-1]["reason"], "aggro")

    def test_tick_uses_food_when_inventory_available(self):
        sess = fake_session()
        item_guid = 0xF120000000000042

        @ac.register
        class _FakeUseItem(ac.Action):
            name = "use_item"
            description = "test double"

            def execute(self, session, world, **_):
                return ac.ActionResult(ok=True)

        try:
            world = world_with_self(sess.player_guid, health=30, max_health=100)
            # Put an item guid in inventory slot 23 (first backpack slot) on
            # the self player, and a matching item object so
            # build_equipment_and_inventory() resolves its name.
            world.update_object(uo.UpdateBlock(
                update_type=uo.UPDATETYPE_VALUES, guid=sess.player_guid,
                fields={uf.PLAYER_FIELD_PACK_SLOT_1: item_guid & 0xFFFFFFFF,
                        uf.PLAYER_FIELD_PACK_SLOT_1 + 1: item_guid >> 32}))
            world.update_object(uo.UpdateBlock(
                update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=item_guid, object_type=uo.TYPEID_ITEM,
                movement={"update_flags": 0},
                fields={uf.OBJECT_FIELD_ENTRY: 159, uf.ITEM_FIELD_STACK_COUNT: 1}))
            world.apply_item_query_response({"entry": 159, "found": True, "name": "Fresh Bread"})

            reflex = rf.RestReflex()
            reflex.tick(sess, world)
            self.assertTrue(reflex.active)
            self.assertEqual(reflex.method, "food")
            self.assertEqual(sess._sent, [])  # no stand-state packet — ate instead
        finally:
            del ac.REGISTRY["use_item"]

    def test_no_inventory_falls_back_to_sitting(self):
        sess = fake_session()  # self object has no inventory item slots set
        world = world_with_self(sess.player_guid, health=30, max_health=100)
        reflex = rf.RestReflex()
        reflex.tick(sess, world)
        self.assertEqual(reflex.method, "sit")


class RestActionTest(unittest.TestCase):
    def test_check_rejects_in_combat(self):
        sess = fake_session()
        world = world_with_self(sess.player_guid, health=100, max_health=100, unit_flags=rf.UNIT_FLAG_IN_COMBAT)
        action = ac.REGISTRY["rest"]
        self.assertEqual(action.check(sess, world), "cannot rest while in combat")

    def test_execute_starts_resting(self):
        sess = fake_session()
        world = world_with_self(sess.player_guid, health=100, max_health=100)
        action = ac.REGISTRY["rest"]
        result = action.execute(sess, world)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail["method"], "sit")
        self.assertFalse(result.detail["already_resting"])

    def test_execute_reports_already_resting(self):
        sess = fake_session()
        world = world_with_self(sess.player_guid, health=100, max_health=100)
        action = ac.REGISTRY["rest"]
        action.execute(sess, world)
        result = action.execute(sess, world)
        self.assertTrue(result.detail["already_resting"])


if __name__ == "__main__":
    unittest.main()
