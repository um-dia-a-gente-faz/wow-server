"""Unit tests for agent.loot: request builders (golden bytes), response
parsers, and the item-query parser — all against hand-built bytes matching
the layout cited in agent/loot.py."""

import struct
import unittest

from agent import loot as lo
from agent import packets as pk


class BuildRequestsTest(unittest.TestCase):
    def test_build_loot(self):
        self.assertEqual(lo.build_loot(0x1234), struct.pack('<Q', 0x1234))

    def test_build_loot_money(self):
        self.assertEqual(lo.build_loot_money(), b'')

    def test_build_autostore_loot_item(self):
        self.assertEqual(lo.build_autostore_loot_item(3), bytes([3]))

    def test_build_loot_release(self):
        self.assertEqual(lo.build_loot_release(0xDEAD), struct.pack('<Q', 0xDEAD))

    def test_build_destroy_item(self):
        self.assertEqual(lo.build_destroy_item(255, 23, 5), struct.pack('<BBI', 255, 23, 5))

    def test_build_item_query(self):
        self.assertEqual(lo.build_item_query(159), struct.pack('<I', 159))

    def test_build_use_item_self(self):
        payload = lo.build_use_item(255, 23, 0xABCD, spell_id=433)
        expected = (struct.pack('<BBBI', 255, 23, 0, 433)
                    + struct.pack('<Q', 0xABCD)
                    + struct.pack('<IB', 0, 0)
                    + struct.pack('<I', 0))
        self.assertEqual(payload, expected)

    def test_build_use_item_with_target(self):
        payload = lo.build_use_item(255, 24, 0x1, spell_id=746, target_guid=0x99)
        expected = (struct.pack('<BBBI', 255, 24, 0, 746)
                    + struct.pack('<Q', 0x1)
                    + struct.pack('<IB', 0, 0)
                    + struct.pack('<I', 0x2)
                    + pk.pack_packed_guid(0x99))
        self.assertEqual(payload, expected)


class ParseLootResponseTest(unittest.TestCase):
    def test_success_with_items(self):
        guid = 0xF130000000000042
        item = struct.pack('<BIIIii', 0, 2589, 1, 12345, 0, -1) + bytes([0])
        payload = struct.pack('<Q', guid) + bytes([lo.LOOT_CORPSE]) + struct.pack('<IB', 55, 1) + item
        info = lo.parse_loot_response(payload)
        self.assertTrue(info["success"])
        self.assertEqual(info["guid"], guid)
        self.assertEqual(info["coins"], 55)
        self.assertEqual(len(info["items"]), 1)
        it = info["items"][0]
        self.assertEqual(it, {"slot": 0, "entry": 2589, "count": 1, "display_id": 12345,
                               "random_suffix": 0, "random_property": -1, "ui_type": 0})

    def test_success_no_items(self):
        guid = 0x1
        payload = struct.pack('<Q', guid) + bytes([lo.LOOT_CORPSE]) + struct.pack('<IB', 0, 0)
        info = lo.parse_loot_response(payload)
        self.assertTrue(info["success"])
        self.assertEqual(info["items"], [])
        self.assertEqual(info["coins"], 0)

    def test_failure(self):
        guid = 0x2
        payload = struct.pack('<Q', guid) + bytes([0, 0])  # acquire_reason=0, failure_reason=0
        info = lo.parse_loot_response(payload)
        self.assertFalse(info["success"])
        self.assertEqual(info["failure_reason"], 0)
        self.assertEqual(info["failure_reason_name"], "didnt_kill")

    def test_leftover_bytes_raise(self):
        guid = 0x3
        payload = struct.pack('<Q', guid) + bytes([0, 0, 0xFF])
        with self.assertRaises(ValueError):
            lo.parse_loot_response(payload)


class ParseSimpleLootPacketsTest(unittest.TestCase):
    def test_loot_release_response(self):
        payload = struct.pack('<Q', 0x777) + bytes([1])
        self.assertEqual(lo.parse_loot_release_response(payload), {"guid": 0x777})

    def test_loot_removed(self):
        self.assertEqual(lo.parse_loot_removed(bytes([4])), {"slot": 4})

    def test_loot_money_notify(self):
        payload = struct.pack('<I', 1234) + bytes([1])
        self.assertEqual(lo.parse_loot_money_notify(payload), {"money": 1234, "sole_looter": True})


class ParseItemPushResultTest(unittest.TestCase):
    def test_new_stack(self):
        payload = (struct.pack('<Q', 0x11) + struct.pack('<III', 1, 0, 1)
                   + bytes([0]) + struct.pack('<I', 0xFFFFFFFF)
                   + struct.pack('<I', 159) + struct.pack('<I', 0)
                   + struct.pack('<i', -1) + struct.pack('<II', 4, 8))
        info = lo.parse_item_push_result(payload)
        self.assertEqual(info["player_guid"], 0x11)
        self.assertTrue(info["received"])
        self.assertFalse(info["created"])
        self.assertEqual(info["bag_slot"], 0)
        self.assertIsNone(info["slot"])  # merged into existing stack
        self.assertEqual(info["entry"], 159)
        self.assertEqual(info["random_property_id"], -1)
        self.assertEqual(info["count"], 4)
        self.assertEqual(info["total_count"], 8)

    def test_fresh_slot(self):
        payload = (struct.pack('<Q', 0x11) + struct.pack('<III', 1, 0, 1)
                   + bytes([0]) + struct.pack('<I', 23)
                   + struct.pack('<I', 2589) + struct.pack('<I', 0)
                   + struct.pack('<i', 0) + struct.pack('<II', 1, 1))
        info = lo.parse_item_push_result(payload)
        self.assertEqual(info["slot"], 23)


class ParseInventoryChangeFailureTest(unittest.TestCase):
    def test_ok(self):
        info = lo.parse_inventory_change_failure(bytes([0]))
        self.assertTrue(info["ok"])
        self.assertEqual(info["result"], 0)

    def test_inv_full(self):
        payload = (bytes([lo.EQUIP_ERR_INV_FULL]) + struct.pack('<Q', 0) + struct.pack('<Q', 0)
                   + bytes([0]) + struct.pack('<i', 0))
        info = lo.parse_inventory_change_failure(payload)
        self.assertFalse(info["ok"])
        self.assertEqual(info["result"], lo.EQUIP_ERR_INV_FULL)
        self.assertEqual(info["item_guids"], [])


def _cstring(s: str) -> bytes:
    return s.encode('utf-8') + b'\x00'


class ParseItemQueryResponseTest(unittest.TestCase):
    def _build(self, entry=159, name="Tough Jerky", stackable=20, max_durability=0,
               item_level=1, allowable_class=-1, item_class=0, subclass=5,
               stats=(), armor=0, damage=(),
               spells=((3222, lo.ITEM_SPELLTRIGGER_ON_USE),)) -> bytes:
        p = struct.pack('<I', entry)
        p += struct.pack('<III', item_class, subclass, 0)  # class, subclass, sound_override
        p += _cstring(name) + bytes([0, 0, 0])
        p += struct.pack('<II', 100, 1)  # display_info_id, quality
        p += struct.pack('<II', 0, 0)  # Flags[2]
        p += struct.pack('<iI', 500, 500)  # buy_price(i32), sell_price
        p += struct.pack('<I', 0)  # inventory_type
        p += struct.pack('<iI', allowable_class, 0)  # allowable_class(i32), allowable_race
        p += struct.pack('<I', item_level)  # item_level
        p += struct.pack('<I', 0)  # required_level
        p += struct.pack('<IIII', 0, 0, 0, 0)  # required_skill/rank/spell/honor_rank
        p += struct.pack('<II', 0, 0)  # required_city_rank, required_reputation_faction
        p += struct.pack('<I', 0)  # required_reputation_rank
        p += struct.pack('<i', -1)  # max_count
        p += struct.pack('<i', stackable)  # stackable
        p += struct.pack('<I', 0)  # container_slots
        stat_slots = list(stats) + [(0, 0)] * (lo.MAX_ITEM_PROTO_STATS - len(stats))
        for stat_type, stat_value in stat_slots[:lo.MAX_ITEM_PROTO_STATS]:
            p += struct.pack('<Ii', stat_type, stat_value)
        p += struct.pack('<II', 0, 0)  # scaling_stat_distribution, scaling_stat_value
        damage_slots = list(damage) + [(0.0, 0.0, 0)] * (lo.MAX_ITEM_PROTO_DAMAGES - len(damage))
        for dmin, dmax, dtype in damage_slots[:lo.MAX_ITEM_PROTO_DAMAGES]:
            p += struct.pack('<ffI', dmin, dmax, dtype)
        p += struct.pack('<I', armor)  # armor
        p += struct.pack('<IIIIII', 0, 0, 0, 0, 0, 0)  # holy/fire/nature/frost/shadow/arcane res
        p += struct.pack('<I', 0)  # delay
        p += struct.pack('<I', 0)  # ammo_type
        p += struct.pack('<f', 0.0)  # ranged_mod_range
        spell_slots = list(spells) + [(0, 0)] * (lo.MAX_ITEM_PROTO_SPELLS - len(spells))
        for spell_id, trigger in spell_slots[:lo.MAX_ITEM_PROTO_SPELLS]:
            if spell_id > 0:
                p += struct.pack('<iIIIII', spell_id, trigger, 0, 0, 0, 0xFFFFFFFF)
            else:
                p += struct.pack('<IIIIII', 0, 0, 0, 0xFFFFFFFF, 0, 0xFFFFFFFF)
        p += struct.pack('<I', 1)  # bonding
        p += _cstring("")  # description
        p += struct.pack('<IIIII', 0, 0, 0, 0, 0)  # page_text, language_id, page_material, start_quest, lock_id
        p += struct.pack('<i', -1)  # material
        p += struct.pack('<I', 0)  # sheath
        p += struct.pack('<ii', -1, -1)  # random_property, random_suffix
        p += struct.pack('<I', 0)  # block
        p += struct.pack('<I', 0)  # item_set
        p += struct.pack('<I', max_durability)  # max_durability
        p += struct.pack('<II', 0, 0)  # area, map
        p += struct.pack('<II', 0, 0)  # bag_family, totem_category
        p += struct.pack('<II', 0, 0) * 3  # Socket[3]
        p += struct.pack('<I', 0)  # socket_bonus
        p += struct.pack('<I', 0)  # gem_properties
        p += struct.pack('<I', 0)  # required_disenchant_skill
        p += struct.pack('<f', 0.0)  # armor_damage_modifier
        p += struct.pack('<I', 0)  # duration
        p += struct.pack('<I', 0)  # item_limit_category
        p += struct.pack('<I', 0)  # holiday_id
        return p

    def test_found(self):
        payload = self._build()
        info = lo.parse_item_query_response(payload)
        self.assertTrue(info["found"])
        self.assertEqual(info["entry"], 159)
        self.assertEqual(info["name"], "Tough Jerky")
        self.assertEqual(info["stackable"], 20)
        self.assertEqual(info["item_level"], 1)
        self.assertEqual(info["stats"], [])
        self.assertEqual(info["armor"], 0)
        self.assertEqual(info["damage"], [])
        self.assertEqual(info["spells"], [{"spell_id": 3222, "trigger": lo.ITEM_SPELLTRIGGER_ON_USE}])

    def test_stats_armor_and_damage(self):
        payload = self._build(
            name="Sword of Testing", item_class=2, subclass=7, item_level=60,
            allowable_class=-1, armor=0,
            stats=((4, 12), (7, 20)),  # STRENGTH +12, STAMINA +20
            damage=((10.0, 20.0, 0),),
        )
        info = lo.parse_item_query_response(payload)
        self.assertEqual(info["stats"], [{"type": 4, "value": 12}, {"type": 7, "value": 20}])
        self.assertEqual(info["damage"], [{"min": 10.0, "max": 20.0, "type": 0}])
        self.assertEqual(info["item_level"], 60)
        self.assertEqual(info["allowable_class"], -1)

    def test_armor_piece(self):
        payload = self._build(name="Plate Chest", item_class=4, subclass=4, armor=500)
        info = lo.parse_item_query_response(payload)
        self.assertEqual(info["class_"], 4)
        self.assertEqual(info["subclass"], 4)
        self.assertEqual(info["armor"], 500)

    def test_not_found(self):
        payload = struct.pack('<I', 999999 | 0x80000000)
        info = lo.parse_item_query_response(payload)
        self.assertFalse(info["found"])
        self.assertEqual(info["entry"], 999999)

    def test_leftover_bytes_raise(self):
        payload = self._build() + b'\xFF'
        with self.assertRaises(ValueError):
            lo.parse_item_query_response(payload)


if __name__ == '__main__':
    unittest.main()
