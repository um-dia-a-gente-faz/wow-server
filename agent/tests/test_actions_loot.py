"""Tests for agent.actions.loot (split out of test_actions.py, issue #248).
"""

import struct
import unittest

from agent import actions as ac
from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo
from agent.tests.actions_helpers import fake_session, object_at, append_event_after, item_object, self_player_with_slots


def lootable_object_at(guid, x, y, z):
    block = object_at(guid, x, y, z)
    ws_block = uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=guid,
                               fields={uf.UNIT_DYNAMIC_FLAGS: per.UNIT_DYNFLAG_LOOTABLE})
    return block, ws_block


WARRIOR_SWORD_TEMPLATE = {
    "entry": 1001, "found": True, "name": "Warrior Sword", "class_": 2, "subclass": 7,
    "inventory_type": 13, "item_level": 20, "allowable_class": -1,
    "stats": [{"type": 4, "value": 10}], "armor": 0, "damage": [{"min": 5.0, "max": 10.0, "type": 0}],
    "spells": [],
}


MAGE_STAFF_TEMPLATE = {
    "entry": 1002, "found": True, "name": "Mage Staff", "class_": 2, "subclass": 10,
    "inventory_type": 17, "item_level": 25, "allowable_class": -1,
    "stats": [{"type": 5, "value": 15}], "armor": 0, "damage": [{"min": 3.0, "max": 6.0, "type": 0}],
    "spells": [],
}


PLATE_CHEST_TEMPLATE = {
    "entry": 1003, "found": True, "name": "Plate Chest", "class_": 4, "subclass": 4,
    "inventory_type": 5, "item_level": 30, "allowable_class": -1,
    "stats": [{"type": 4, "value": 8}], "armor": 200, "damage": [], "spells": [],
}


CLOTH_ROBE_ONLY_MAGE_TEMPLATE = {
    "entry": 1004, "found": True, "name": "Mage Robe", "class_": 4, "subclass": 1,
    "inventory_type": 5, "item_level": 18, "allowable_class": 1 << (ac.item_compare.CLASS_MAGE - 1),
    "stats": [{"type": 5, "value": 6}], "armor": 40, "damage": [], "spells": [],
}


class LootActionTest(unittest.TestCase):
    def test_check_not_lootable(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 1.0, 0.0, 0.0))
        self.assertIsNotNone(ac.LootAction().check(sess, world, guid=5))

    def test_check_out_of_range(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        block, ws_block = lootable_object_at(5, 100.0, 0.0, 0.0)
        world.update_object(block)
        world.update_object(ws_block)
        err = ac.LootAction().check(sess, world, guid=5)
        self.assertIsNotNone(err)
        self.assertIn("loot range", err)

    def test_check_passes_in_range_and_lootable(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        block, ws_block = lootable_object_at(5, 2.0, 0.0, 0.0)
        world.update_object(block)
        world.update_object(ws_block)
        self.assertIsNone(ac.LootAction().check(sess, world, guid=5))

    def test_execute_full_sequence_on_success(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        action = ac.LootAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01

        response = {"kind": "loot_response", "guid": 5, "success": True, "coins": 12,
                    "items": [{"slot": 0, "entry": 159, "count": 4}]}
        append_event_after(sess, 0.02, response)
        result = action.execute(sess, world, guid=5)

        opcodes = [op for op, _ in sess._sent]
        self.assertIn(ac.lootmod.CMSG_LOOT, opcodes)
        self.assertIn(ac.lootmod.CMSG_LOOT_MONEY, opcodes)
        self.assertIn(ac.lootmod.CMSG_AUTOSTORE_LOOT_ITEM, opcodes)
        self.assertIn(ac.lootmod.CMSG_LOOT_RELEASE, opcodes)
        # release must be the very last packet sent
        self.assertEqual(opcodes[-1], ac.lootmod.CMSG_LOOT_RELEASE)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail["coins"], 12)
        self.assertEqual(result.detail["items"], response["items"])

    def test_execute_no_response_times_out(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        action = ac.LootAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, guid=5)
        self.assertFalse(result.ok)
        # never got a response, so no follow-up loot packets were sent
        opcodes = [op for op, _ in sess._sent]
        self.assertEqual(opcodes, [ac.lootmod.CMSG_LOOT])

    def test_execute_failure_response_stops_early(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        action = ac.LootAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "loot_response", "guid": 5, "success": False,
                                          "failure_reason_name": "too_far"})
        result = action.execute(sess, world, guid=5)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "too_far")
        opcodes = [op for op, _ in sess._sent]
        self.assertEqual(opcodes, [ac.lootmod.CMSG_LOOT])


class UseItemActionTest(unittest.TestCase):
    def test_check_no_item_at_slot(self):
        sess = fake_session()
        world = per.WorldState()
        world.set_my_guid(0x1)
        world.update_object(self_player_with_slots(0x1, {}))
        self.assertIsNotNone(ac.UseItemAction().check(sess, world, bag=255, slot=23))

    def test_check_blocked_in_combat(self):
        sess = fake_session()
        world = per.WorldState()
        world.set_my_guid(0x1)
        item_guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: item_guid}))
        world.update_object(item_object(item_guid, entry=159))
        world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=0x1,
                                            fields={uf.UNIT_FIELD_FLAGS: ac.UNIT_FLAG_IN_COMBAT}))
        err = ac.UseItemAction().check(sess, world, bag=255, slot=23)
        self.assertIsNotNone(err)
        self.assertIn("combat", err)

    def test_execute_sends_use_item_with_looked_up_spell(self):
        sess = fake_session()
        world = per.WorldState()
        world.set_my_guid(0x1)
        item_guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: item_guid}))
        world.update_object(item_object(item_guid, entry=159))
        world.items.items[159] = {"entry": 159, "found": True, "name": "Tough Jerky",
                                   "spells": [{"spell_id": 433, "trigger": ac.lootmod.ITEM_SPELLTRIGGER_ON_USE}]}

        result = ac.UseItemAction().execute(sess, world, bag=255, slot=23)
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.lootmod.CMSG_USE_ITEM)
        bag, slot, cast_count, spell_id = struct.unpack_from('<BBBI', payload, 0)
        item_guid_on_wire = struct.unpack_from('<Q', payload, 7)[0]
        self.assertEqual((bag, slot, spell_id), (255, 23, 433))
        self.assertEqual(item_guid_on_wire, item_guid)
        self.assertEqual(result.detail["spell_id"], 433)


class DestroyItemActionTest(unittest.TestCase):
    def test_requires_confirm(self):
        sess = fake_session()
        world = per.WorldState()
        err = ac.DestroyItemAction().check(sess, world, bag=255, slot=23, count=1, confirm=False)
        self.assertIsNotNone(err)
        self.assertIn("confirm", err)

    def test_execute_sends_destroyitem(self):
        sess = fake_session()
        world = per.WorldState()
        result = ac.DestroyItemAction().run(sess, world, bag=255, slot=23, count=2, confirm=True)
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.lootmod.CMSG_DESTROYITEM)
        self.assertEqual(payload, struct.pack('<BBI', 255, 23, 2))


class CompareItemsActionTest(unittest.TestCase):
    def _world_with(self, slot_a, entry_a, template_a, slot_b, entry_b, template_b):
        world = per.WorldState()
        world.set_my_guid(0x1)
        guid_a = 0xF120000000000001
        guid_b = 0xF120000000000002
        world.update_object(self_player_with_slots(0x1, {slot_a: guid_a, slot_b: guid_b}))
        world.update_object(item_object(guid_a, entry=entry_a))
        world.update_object(item_object(guid_b, entry=entry_b))
        world.items.items[entry_a] = template_a
        world.items.items[entry_b] = template_b
        return world

    def test_check_fails_when_template_not_cached_yet(self):
        world = per.WorldState()
        world.set_my_guid(0x1)
        guid_a = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: guid_a}))
        world.update_object(item_object(guid_a, entry=1001))
        err = ac.CompareItemsAction().check(fake_session(), world, bag_a=255, slot_a=23,
                                             bag_b=255, slot_b=24)
        self.assertIsNotNone(err)
        self.assertIn("not queried yet", err)

    def test_check_fails_for_different_equip_slots(self):
        world = self._world_with(23, 1001, WARRIOR_SWORD_TEMPLATE, 24, 1003, PLATE_CHEST_TEMPLATE)
        err = ac.CompareItemsAction().check(fake_session(), world, bag_a=255, slot_a=23,
                                             bag_b=255, slot_b=24)
        self.assertIsNotNone(err)
        self.assertIn("different equip slots", err)

    def test_execute_picks_higher_primary_stat_for_class(self):
        # Same inventory_type (weapon slot 13/two-hand not required here, both use
        # inventory_type=13/17 in fixtures — re-key both to the same slot for this test).
        template_b = dict(MAGE_STAFF_TEMPLATE, inventory_type=WARRIOR_SWORD_TEMPLATE["inventory_type"])
        world = self._world_with(23, 1001, WARRIOR_SWORD_TEMPLATE, 24, 1002, template_b)
        sess = fake_session(class_=ac.item_compare.CLASS_WARRIOR)
        result = ac.CompareItemsAction().execute(sess, world, bag_a=255, slot_a=23, bag_b=255, slot_b=24)
        self.assertTrue(result.ok)
        # Warrior's primary stat (Strength) only appears on item A -> A wins.
        self.assertEqual(result.detail["winner"], "a")
        self.assertIn("Warrior Sword", result.detail["reason"])

    def test_execute_flags_unusable_item_without_crashing(self):
        template_b = dict(PLATE_CHEST_TEMPLATE, inventory_type=WARRIOR_SWORD_TEMPLATE["inventory_type"])
        world = self._world_with(23, 1001, WARRIOR_SWORD_TEMPLATE, 24, 1003, template_b)
        sess = fake_session(class_=ac.item_compare.CLASS_MAGE)
        result = ac.CompareItemsAction().execute(sess, world, bag_a=255, slot_a=23, bag_b=255, slot_b=24)
        self.assertTrue(result.ok)  # comparing is always safe; unusability is just reported
        self.assertIsNotNone(result.detail["usability_error_b"])
        self.assertIn("proficiency", result.detail["usability_error_b"])


class EquipItemActionTest(unittest.TestCase):
    def _world_with_item(self, slot, entry, template):
        world = per.WorldState()
        world.set_my_guid(0x1)
        guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {slot: guid}))
        world.update_object(item_object(guid, entry=entry))
        if template is not None:
            world.items.items[entry] = template
        return world, guid

    def test_check_fails_for_empty_slot(self):
        world = per.WorldState()
        world.set_my_guid(0x1)
        world.update_object(self_player_with_slots(0x1, {}))
        err = ac.EquipItemAction().check(fake_session(), world, bag=255, slot=23)
        self.assertIsNotNone(err)
        self.assertIn("no known item", err)

    def test_check_rejects_item_character_cannot_use(self):
        world, _ = self._world_with_item(23, 1004, CLOTH_ROBE_ONLY_MAGE_TEMPLATE)
        sess = fake_session(class_=ac.item_compare.CLASS_WARRIOR)
        err = ac.EquipItemAction().check(sess, world, bag=255, slot=23)
        self.assertIsNotNone(err)
        self.assertIn("not usable by class", err)

    def test_check_passes_when_class_matches(self):
        world, _ = self._world_with_item(23, 1004, CLOTH_ROBE_ONLY_MAGE_TEMPLATE)
        sess = fake_session(class_=ac.item_compare.CLASS_MAGE)
        self.assertIsNone(ac.EquipItemAction().check(sess, world, bag=255, slot=23))

    def test_check_allows_unqueried_item_through_to_the_server(self):
        # Template not cached yet -> can't pre-validate, so check() doesn't block it
        # (the server's own inventory_change_failure is the backstop).
        world, _ = self._world_with_item(23, 1001, None)
        self.assertIsNone(ac.EquipItemAction().check(fake_session(), world, bag=255, slot=23))

    def test_execute_sends_autoequip_and_succeeds_when_no_failure_arrives(self):
        world, guid = self._world_with_item(23, 1001, WARRIOR_SWORD_TEMPLATE)
        sess = fake_session(class_=ac.item_compare.CLASS_WARRIOR)
        action = ac.EquipItemAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, bag=255, slot=23)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (ac.lootmod.CMSG_AUTOEQUIP_ITEM,
                                          ac.lootmod.build_autoequip_item(255, 23)))
        self.assertEqual(sess._sent[0][0], 0x10A)
        self.assertEqual(result.detail["item_guid"], guid)

    def test_execute_fails_on_inventory_change_failure(self):
        world, _ = self._world_with_item(23, 1001, WARRIOR_SWORD_TEMPLATE)
        sess = fake_session(class_=ac.item_compare.CLASS_WARRIOR)
        action = ac.EquipItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "inventory_change_failure", "result": 50,
                                         "reason_name": "inventory_full"})
        result = action.execute(sess, world, bag=255, slot=23)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "inventory_full")
