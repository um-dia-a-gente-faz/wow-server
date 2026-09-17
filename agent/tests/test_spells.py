"""Unit tests for agent.spells: spellbook parsers, the spell metadata table,
CMSG_CAST_SPELL builder, and combat/XP event parsers — all against hand-built
bytes matching the layout cited in agent/spells.py."""

import struct
import unittest

from agent import packets as pk
from agent import spells as sp


class InitialSpellsTest(unittest.TestCase):
    def test_spells_and_cooldowns(self):
        payload = (bytes([1])  # initial_login
                   + struct.pack('<H', 2)
                   + struct.pack('<IH', 635, 0)
                   + struct.pack('<IH', 21084, 1)
                   + struct.pack('<H', 1)
                   + struct.pack('<IHHii', 20271, 0, 0, 1500, 0))
        info = sp.parse_initial_spells(payload)
        self.assertTrue(info["initial_login"])
        self.assertEqual(info["spell_ids"], [635, 21084])
        self.assertEqual(len(info["cooldowns"]), 1)
        self.assertEqual(info["cooldowns"][0]["spell_id"], 20271)
        self.assertEqual(info["cooldowns"][0]["recovery_time"], 1500)

    def test_empty(self):
        payload = bytes([0]) + struct.pack('<H', 0) + struct.pack('<H', 0)
        info = sp.parse_initial_spells(payload)
        self.assertFalse(info["initial_login"])
        self.assertEqual(info["spell_ids"], [])
        self.assertEqual(info["cooldowns"], [])

    def test_leftover_bytes_raise(self):
        payload = bytes([0]) + struct.pack('<H', 0) + struct.pack('<H', 0) + b'\xFF'
        with self.assertRaises(ValueError):
            sp.parse_initial_spells(payload)


class LearnedRemovedSpellTest(unittest.TestCase):
    def test_learned_spell(self):
        payload = struct.pack('<iH', 635, 0)
        self.assertEqual(sp.parse_learned_spell(payload), 635)

    def test_removed_spell(self):
        payload = struct.pack('<I', 635)
        self.assertEqual(sp.parse_removed_spell(payload), 635)


class SpellTableTest(unittest.TestCase):
    def test_known_spell(self):
        info = sp.get_spell_info(635)
        self.assertIsNotNone(info)
        self.assertEqual(info.name, "Holy Light")
        self.assertEqual(info.class_, "paladin")

    def test_unknown_spell(self):
        self.assertIsNone(sp.get_spell_info(999999))

    def test_every_entry_key_matches_its_spell_id(self):
        for spell_id, info in sp.SPELL_TABLE.items():
            self.assertEqual(spell_id, info.spell_id)


class BuildCastSpellTest(unittest.TestCase):
    def test_self_cast_no_target(self):
        payload = sp.build_cast_spell(21084)
        cast_id, spell_id, flags = struct.unpack_from('<BiB', payload, 0)
        self.assertEqual(cast_id, 0)
        self.assertEqual(spell_id, 21084)
        self.assertEqual(flags, 0)
        target_flags = struct.unpack_from('<I', payload, 6)[0]
        self.assertEqual(target_flags, sp.TARGET_FLAG_NONE)
        self.assertEqual(len(payload), 10)  # no packed guid appended

    def test_unit_target(self):
        payload = sp.build_cast_spell(133, target_guid=0xF130000000000042)
        target_flags = struct.unpack_from('<I', payload, 6)[0]
        self.assertEqual(target_flags, sp.TARGET_FLAG_UNIT)
        guid, off = pk.unpack_packed_guid(payload, 10)
        self.assertEqual(guid, 0xF130000000000042)
        self.assertEqual(off, len(payload))

    def test_cast_count_is_customizable(self):
        payload = sp.build_cast_spell(133, cast_count=3)
        self.assertEqual(payload[0], 3)


class CastFailedTest(unittest.TestCase):
    def test_no_args(self):
        payload = bytes([2]) + struct.pack('<I', 133) + bytes([63])  # not_known
        info = sp.parse_cast_failed(payload)
        self.assertEqual(info["cast_id"], 2)
        self.assertEqual(info["spell_id"], 133)
        self.assertEqual(info["reason"], 63)
        self.assertEqual(info["reason_name"], "not_known")
        self.assertNotIn("failed_arg1", info)

    def test_with_one_arg(self):
        payload = bytes([0]) + struct.pack('<I', 133) + bytes([97]) + struct.pack('<i', 30)  # out_of_range
        info = sp.parse_cast_failed(payload)
        self.assertEqual(info["reason_name"], "out_of_range")
        self.assertEqual(info["failed_arg1"], 30)
        self.assertNotIn("failed_arg2", info)

    def test_with_two_args(self):
        payload = bytes([0]) + struct.pack('<I', 133) + bytes([67]) + struct.pack('<ii', 1, 2)  # not_ready
        info = sp.parse_cast_failed(payload)
        self.assertEqual(info["failed_arg1"], 1)
        self.assertEqual(info["failed_arg2"], 2)

    def test_unmapped_reason_gets_fallback_name(self):
        payload = bytes([0]) + struct.pack('<I', 1) + bytes([250])
        info = sp.parse_cast_failed(payload)
        self.assertEqual(info["reason_name"], "reason_250")

    def test_every_reason_from_0_to_187_is_named(self):
        for reason in range(188):
            self.assertIn(reason, sp.SPELL_CAST_RESULT_NAMES)
        self.assertEqual(len(sp.SPELL_CAST_RESULT_NAMES), 188)


class SpellCastPrefixTest(unittest.TestCase):
    def test_parses_fixed_prefix_and_ignores_tail(self):
        payload = (pk.pack_packed_guid(0xF130000000000001)
                   + pk.pack_packed_guid(0xF130000000000001)
                   + bytes([0])
                   + struct.pack('<III', 133, 0, 1234)
                   + b'\xDE\xAD\xBE\xEF\xFF')  # arbitrary tail, must not raise
        info = sp.parse_spell_cast_prefix(payload)
        self.assertEqual(info["caster_guid"], 0xF130000000000001)
        self.assertEqual(info["spell_id"], 133)
        self.assertEqual(info["cast_time"], 1234)


class AttackStartStopTest(unittest.TestCase):
    def test_attack_start(self):
        payload = struct.pack('<QQ', 0x1, 0x2)
        info = sp.parse_attack_start(payload)
        self.assertEqual(info, {"attacker_guid": 1, "victim_guid": 2})

    def test_attack_stop(self):
        payload = pk.pack_packed_guid(0x1) + pk.pack_packed_guid(0x2) + struct.pack('<I', 1)
        info = sp.parse_attack_stop(payload)
        self.assertEqual(info["attacker_guid"], 1)
        self.assertEqual(info["victim_guid"], 2)
        self.assertTrue(info["now_dead"])


def attacker_state_update_body(flags=0, damage=10, over_damage=-1, sub_damages=((0, 10.0, 10),),
                                victim_state=1, attacker_state=1, melee_spell_id=0,
                                block_amount=None, rage_gained=None, hitinfo=None):
    body = struct.pack('<I', flags)
    body += pk.pack_packed_guid(0x1) + pk.pack_packed_guid(0x2)
    body += struct.pack('<Ii', damage, over_damage)
    body += bytes([len(sub_damages)])
    for school, fdamage, dmg in sub_damages:
        body += struct.pack('<I', school) + struct.pack('<f', fdamage) + struct.pack('<I', dmg)
    if flags & (sp.HITINFO_FULL_ABSORB | sp.HITINFO_PARTIAL_ABSORB):
        body += struct.pack(f'<{len(sub_damages)}I', *([0] * len(sub_damages)))
    if flags & (sp.HITINFO_FULL_RESIST | sp.HITINFO_PARTIAL_RESIST):
        body += struct.pack(f'<{len(sub_damages)}I', *([0] * len(sub_damages)))
    body += bytes([victim_state])
    body += struct.pack('<II', attacker_state, melee_spell_id)
    if flags & sp.HITINFO_BLOCK:
        body += struct.pack('<I', block_amount or 0)
    if flags & sp.HITINFO_RAGE_GAIN:
        body += struct.pack('<I', rage_gained or 0)
    if flags & sp.HITINFO_UNK1:
        body += struct.pack('<14I', *([0] * 14))
    return body


class AttackerStateUpdateTest(unittest.TestCase):
    def test_basic_hit(self):
        payload = attacker_state_update_body(damage=42, victim_state=1)
        info = sp.parse_attacker_state_update(payload)
        self.assertEqual(info["attacker_guid"], 1)
        self.assertEqual(info["victim_guid"], 2)
        self.assertEqual(info["damage"], 42)
        self.assertEqual(info["victim_state_name"], "hit")

    def test_dodge(self):
        payload = attacker_state_update_body(damage=0, victim_state=2)
        info = sp.parse_attacker_state_update(payload)
        self.assertEqual(info["victim_state_name"], "dodge")

    def test_two_sub_damages(self):
        payload = attacker_state_update_body(sub_damages=((0, 5.0, 5), (1, 3.0, 3)))
        info = sp.parse_attacker_state_update(payload)
        self.assertEqual(info["sub_damage"], [5, 3])

    def test_absorb_and_resist_flags(self):
        flags = sp.HITINFO_FULL_ABSORB | sp.HITINFO_PARTIAL_RESIST
        payload = attacker_state_update_body(flags=flags)
        info = sp.parse_attacker_state_update(payload)
        self.assertEqual(info["flags"], flags)

    def test_block_and_rage_and_unk1(self):
        flags = sp.HITINFO_BLOCK | sp.HITINFO_RAGE_GAIN | sp.HITINFO_UNK1
        payload = attacker_state_update_body(flags=flags, block_amount=5, rage_gained=3)
        info = sp.parse_attacker_state_update(payload)
        self.assertEqual(info["flags"], flags)

    def test_leftover_bytes_raise(self):
        payload = attacker_state_update_body() + b'\xFF'
        with self.assertRaises(ValueError):
            sp.parse_attacker_state_update(payload)


class SpellNonMeleeDamageLogTest(unittest.TestCase):
    def test_parses_all_fields(self):
        payload = (pk.pack_packed_guid(0x1) + pk.pack_packed_guid(0x2)
                   + struct.pack('<iii', 133, 50, 0)
                   + bytes([2])  # school_mask
                   + struct.pack('<II', 0, 0)  # absorbed, resisted
                   + bytes([0, 0])  # periodic, unused
                   + struct.pack('<II', 0, 0))  # shield_block, flags
        info = sp.parse_spell_non_melee_damage_log(payload)
        self.assertEqual(info["target_guid"], 1)
        self.assertEqual(info["caster_guid"], 2)
        self.assertEqual(info["spell_id"], 133)
        self.assertEqual(info["damage"], 50)
        self.assertEqual(info["school_mask"], 2)


class PartyKillLogTest(unittest.TestCase):
    def test_parses_killer_and_victim(self):
        payload = struct.pack('<QQ', 0x1, 0x2)
        info = sp.parse_party_kill_log(payload)
        self.assertEqual(info, {"killer_guid": 1, "victim_guid": 2})


class LogXpGainTest(unittest.TestCase):
    def test_kill_xp(self):
        payload = (struct.pack('<Q', 0x2)
                   + struct.pack('<I', 45)
                   + bytes([0])
                   + struct.pack('<I', 40)
                   + struct.pack('<f', 1.0)
                   + bytes([0]))
        info = sp.parse_log_xp_gain(payload)
        self.assertTrue(info["is_kill"])
        self.assertEqual(info["victim_guid"], 2)
        self.assertEqual(info["total_xp"], 45)
        self.assertEqual(info["xp_without_bonus"], 40)

    def test_non_kill_xp_has_no_victim_fields(self):
        payload = struct.pack('<Q', 0) + struct.pack('<I', 100) + bytes([1]) + bytes([0])
        info = sp.parse_log_xp_gain(payload)
        self.assertFalse(info["is_kill"])
        self.assertNotIn("xp_without_bonus", info)


class LevelUpInfoTest(unittest.TestCase):
    def test_parses_level_and_deltas(self):
        payload = (struct.pack('<II', 5, 20)
                   + struct.pack('<7I', *range(7))
                   + struct.pack('<5I', *range(5)))
        info = sp.parse_levelup_info(payload)
        self.assertEqual(info["level"], 5)
        self.assertEqual(info["health_delta"], 20)
        self.assertEqual(info["power_delta"], list(range(7)))
        self.assertEqual(info["stat_delta"], list(range(5)))


if __name__ == '__main__':
    unittest.main()
