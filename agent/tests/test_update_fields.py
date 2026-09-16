"""Unit tests for agent.update_fields.decode_fields against synthetic
{field_index: uint32} dicts (the shape agent.update_object._parse_values_update
returns)."""

import struct
import unittest

from agent import update_fields as uf
from agent import update_object as uo


def pack_float(value: float) -> int:
    return struct.unpack('<I', struct.pack('<f', value))[0]


class UnitFieldsTest(unittest.TestCase):
    def test_core_stats(self):
        raw = {
            uf.OBJECT_FIELD_ENTRY: 6368,
            uf.UNIT_FIELD_HEALTH: 42,
            uf.UNIT_FIELD_MAXHEALTH: 100,
            uf.UNIT_FIELD_LEVEL: 5,
            uf.UNIT_FIELD_FACTIONTEMPLATE: 14,
            uf.UNIT_FIELD_FLAGS: 0x8,
            uf.UNIT_DYNAMIC_FLAGS: 0x1,
            uf.UNIT_NPC_FLAGS: 0x2,
            uf.UNIT_FIELD_DISPLAYID: 1234,
        }
        decoded = uf.decode_fields(uo.TYPEID_UNIT, raw)
        self.assertEqual(decoded['entry'], 6368)
        self.assertEqual(decoded['health'], 42)
        self.assertEqual(decoded['max_health'], 100)
        self.assertEqual(decoded['level'], 5)
        self.assertEqual(decoded['faction'], 14)
        self.assertEqual(decoded['unit_flags'], 0x8)
        self.assertEqual(decoded['dynamic_flags'], 0x1)
        self.assertEqual(decoded['npc_flags'], 0x2)
        self.assertEqual(decoded['display_id'], 1234)
        self.assertEqual(decoded['raw_fields'], raw)

    def test_target_guid_spans_two_slots(self):
        raw = {uf.UNIT_FIELD_TARGET: 0xAABBCCDD, uf.UNIT_FIELD_TARGET + 1: 0xF130}
        decoded = uf.decode_fields(uo.TYPEID_UNIT, raw)
        self.assertEqual(decoded['target_guid'], 0xAABBCCDD | (0xF130 << 32))

    def test_target_guid_high_word_defaults_zero_when_absent(self):
        raw = {uf.UNIT_FIELD_TARGET: 5}
        decoded = uf.decode_fields(uo.TYPEID_UNIT, raw)
        self.assertEqual(decoded['target_guid'], 5)

    def test_no_target_guid_when_low_word_absent(self):
        decoded = uf.decode_fields(uo.TYPEID_UNIT, {})
        self.assertNotIn('target_guid', decoded)

    def test_power_and_max_power_lists_only_include_set_slots(self):
        raw = {uf.UNIT_FIELD_POWER1: 10, uf.UNIT_FIELD_POWER1 + 2: 30,
               uf.UNIT_FIELD_MAXPOWER1: 100}
        decoded = uf.decode_fields(uo.TYPEID_UNIT, raw)
        self.assertEqual(decoded['power'], [10, 30])
        self.assertEqual(decoded['max_power'], [100])

    def test_bytes_0_splits_race_class_gender_powertype(self):
        # race=10 (blood elf), class=2 (paladin), gender=0, power_type=0
        raw = {uf.UNIT_FIELD_BYTES_0: 10 | (2 << 8) | (0 << 16) | (0 << 24)}
        decoded = uf.decode_fields(uo.TYPEID_UNIT, raw)
        self.assertEqual(decoded['race'], 10)
        self.assertEqual(decoded['class_'], 2)
        self.assertEqual(decoded['gender'], 0)
        self.assertEqual(decoded['power_type'], 0)

    def test_unmapped_field_stays_in_raw_fields_only(self):
        raw = {9999: 42}
        decoded = uf.decode_fields(uo.TYPEID_UNIT, raw)
        self.assertEqual(decoded['raw_fields'], raw)
        self.assertNotIn(9999, {k for k in decoded if k != 'raw_fields'})

    def test_scale_is_reinterpreted_as_float(self):
        raw = {uf.OBJECT_FIELD_SCALE_X: pack_float(1.5)}
        decoded = uf.decode_fields(uo.TYPEID_UNIT, raw)
        self.assertAlmostEqual(decoded['scale'], 1.5, places=5)


class PlayerFieldsTest(unittest.TestCase):
    def test_player_only_fields(self):
        raw = {
            uf.PLAYER_FLAGS: 1,
            uf.PLAYER_XP: 8400,
            uf.PLAYER_NEXT_LEVEL_XP: 11400,
            uf.PLAYER_FIELD_COINAGE: 23450,
            uf.UNIT_FIELD_LEVEL: 12,
        }
        decoded = uf.decode_fields(uo.TYPEID_PLAYER, raw)
        self.assertEqual(decoded['player_flags'], 1)
        self.assertEqual(decoded['xp'], 8400)
        self.assertEqual(decoded['next_level_xp'], 11400)
        self.assertEqual(decoded['coinage'], 23450)
        self.assertEqual(decoded['level'], 12)  # inherited from UNIT mapping

    def test_player_fields_not_mapped_for_plain_unit(self):
        raw = {uf.PLAYER_XP: 999}
        decoded = uf.decode_fields(uo.TYPEID_UNIT, raw)
        self.assertNotIn('xp', decoded)


class GameObjectFieldsTest(unittest.TestCase):
    def test_gameobject_fields(self):
        raw = {
            uf.OBJECT_FIELD_ENTRY: 181646,
            uf.GAMEOBJECT_DISPLAYID: 500,
            uf.GAMEOBJECT_FLAGS: 0x10,
            uf.GAMEOBJECT_FACTION: 0,
            uf.GAMEOBJECT_LEVEL: 0,
        }
        decoded = uf.decode_fields(uo.TYPEID_GAMEOBJECT, raw)
        self.assertEqual(decoded['entry'], 181646)
        self.assertEqual(decoded['display_id'], 500)
        self.assertEqual(decoded['gameobject_flags'], 0x10)

    def test_unit_fields_not_mapped_for_gameobject(self):
        raw = {uf.UNIT_FIELD_HEALTH: 100}
        decoded = uf.decode_fields(uo.TYPEID_GAMEOBJECT, raw)
        self.assertNotIn('health', decoded)


if __name__ == '__main__':
    unittest.main()
