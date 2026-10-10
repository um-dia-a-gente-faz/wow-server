"""Tests for the equipment/inventory snapshot builder (perception/inventory.py)."""

import unittest

from agent import perception as per
from agent import update_fields as uo_fields
from agent import update_object as uo
from agent.tests.test_perception import create_block


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
