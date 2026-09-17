#!/usr/bin/env python3
"""Combat: spellbook response parsers, a hardcoded spell metadata table,
the CMSG_CAST_SPELL builder, and combat/XP event parsers. Pure, no I/O —
agent/session.py wires these into session state (spellbook, spell_cooldowns,
events) and owns the opcode constants, same split as agent/names.py.

Every wire layout cited below is verified against the live
`TrinityCore/TrinityCore` GitHub repo at `ref=3.3.5` (`gh api repos/
TrinityCore/TrinityCore/contents/... ?ref=3.3.5`):
  src/server/game/Server/Packets/SpellPackets.h / .cpp
    (InitialSpells, LearnedSpell, UnlearnedSpell, CastSpell/SpellCastRequest/
    SpellTargetData, CastFailed, SpellStart/SpellGo/SpellCastData)
  src/server/game/Server/Packets/CombatPackets.h / .cpp (AttackStart, SAttackStop)
  src/server/game/Server/Packets/CombatLogPackets.h / .cpp
    (AttackerStateUpdate, SpellNonMeleeDamageLog)
  src/server/game/Server/Packets/MiscPackets.h / .cpp (LevelUpInfo)
  src/server/game/Entities/Unit/Unit.cpp (SMSG_PARTYKILLLOG, raw WorldPacket)
  src/server/game/Entities/Player/Player.cpp (SendLogXPGain, SMSG_LOG_XPGAIN)
  src/server/game/Entities/Unit/UnitDefines.h (HitInfo, VictimState)
  src/server/game/Spells/SpellDefines.h (TARGET_FLAG_*)
  src/server/shared/SharedDefines.h (SpellCastResult, MAX_POWERS, MAX_STATS)
"""

import struct
from dataclasses import dataclass

from . import packets as pk

# ── Spellbook (SMSG_INITIAL_SPELLS / LEARNED_SPELL / REMOVED_SPELL) ───────


def parse_initial_spells(payload: bytes) -> dict:
    """SMSG_INITIAL_SPELLS (InitialSpells::Write, SpellPackets.cpp):
    uint8 initial_login, uint16 spell_count, spell_count * (uint32 spell_id,
    uint16 action_bar_slot — unused client-side, not needed by callers),
    uint16 cooldown_count, cooldown_count * (uint32 spell_id, uint16 item_id,
    uint16 category, int32 recovery_time, int32 category_recovery_time).
    An "on hold" (infinite) cooldown is special-cased server-side as
    recovery_time=1, category_recovery_time=0x80000000 (as a signed int32,
    negative) rather than a separate flag on the wire — not distinguished
    here, just returned as those raw values.
    """
    off = 0
    initial_login = payload[off]; off += 1
    spell_count = pk.u16(payload, off); off += 2
    spell_ids = []
    for _ in range(spell_count):
        spell_ids.append(pk.u32(payload, off)); off += 4
        off += 2  # action_bar_slot
    cooldown_count = pk.u16(payload, off); off += 2
    cooldowns = []
    for _ in range(cooldown_count):
        spell_id = pk.u32(payload, off); off += 4
        item_id = pk.u16(payload, off); off += 2
        category = pk.u16(payload, off); off += 2
        recovery_time = struct.unpack_from('<i', payload, off)[0]; off += 4
        category_recovery_time = struct.unpack_from('<i', payload, off)[0]; off += 4
        cooldowns.append({"spell_id": spell_id, "item_id": item_id, "category": category,
                           "recovery_time": recovery_time,
                           "category_recovery_time": category_recovery_time})
    if off != len(payload):
        raise ValueError(f"parsed {off} of {len(payload)} bytes ({len(payload) - off} leftover)")
    return {"initial_login": bool(initial_login), "spell_ids": spell_ids, "cooldowns": cooldowns}


def parse_learned_spell(payload: bytes) -> int:
    """SMSG_LEARNED_SPELL: int32 spell_id, uint16 action_bar_slot (unused,
    not consumed — self-delimited packet, no sibling data follows)."""
    return struct.unpack_from('<i', payload, 0)[0]


def parse_removed_spell(payload: bytes) -> int:
    """SMSG_REMOVED_SPELL: uint32 spell_id."""
    return pk.u32(payload, 0)


# ── Spell metadata ─────────────────────────────────────────────────────────
# v1: a small hardcoded table for the 5 agent classes' (UM-63) starter
# spells, levels 1-10. get_spell_info() is the interface a Spell.dbc loader
# can replace later (tools/wowmap/transform.py-style) without touching
# callers — see UM-39's card. Cross-checked against `world.playercreateinfo_
# spell_custom` for these characters' starting spells where possible; verify
# against Spell.dbc directly before trusting anything past ~level 5.

POWER_MANA = 0
POWER_RAGE = 1
POWER_ENERGY = 3


@dataclass(frozen=True)
class SpellInfo:
    spell_id: int
    name: str
    class_: str  # "paladin" | "hunter" | "rogue" | "priest" | "mage"
    min_level: int
    power_cost: int
    power_type: int  # POWER_* above
    cast_time_ms: int  # 0 = instant
    range_yd: float
    description: str


SPELL_TABLE: dict[int, SpellInfo] = {
    # Paladin
    635: SpellInfo(635, "Holy Light", "paladin", 4, 25, POWER_MANA, 2500, 40.0,
                   "Heals a friendly target."),
    21084: SpellInfo(21084, "Seal of Righteousness", "paladin", 4, 0, POWER_MANA, 0, 0.0,
                      "Self buff: melee attacks deal additional holy damage."),
    20271: SpellInfo(20271, "Judgement", "paladin", 4, 25, POWER_MANA, 0, 10.0,
                      "Judges the target's Seal, causing an effect."),
    2812: SpellInfo(2812, "Holy Wrath", "paladin", 6, 26, POWER_MANA, 0, 10.0,
                     "Undead/demon area damage."),
    # Hunter
    75: SpellInfo(75, "Auto Shot", "hunter", 1, 0, POWER_MANA, 0, 35.0,
                   "Ranged weapon auto-attack."),
    2973: SpellInfo(2973, "Raptor Strike", "hunter", 1, 0, POWER_MANA, 0, 0.0,
                     "Melee strike that deals extra damage."),
    1978: SpellInfo(1978, "Serpent Sting", "hunter", 4, 15, POWER_MANA, 0, 35.0,
                     "Poisons the target for damage over time."),
    136: SpellInfo(136, "Mend Pet", "hunter", 4, 20, POWER_MANA, 0, 0.0,
                    "Heals the hunter's pet over time."),
    # Rogue
    1752: SpellInfo(1752, "Sinister Strike", "rogue", 1, 45, POWER_ENERGY, 0, 0.0,
                     "Melee strike that generates a combo point."),
    2098: SpellInfo(2098, "Eviscerate", "rogue", 8, 35, POWER_ENERGY, 0, 0.0,
                     "Finishing move that deals damage per combo point."),
    1784: SpellInfo(1784, "Stealth", "rogue", 1, 0, POWER_ENERGY, 0, 0.0,
                     "Self buff: become stealthed."),
    # Priest
    585: SpellInfo(585, "Smite", "priest", 1, 20, POWER_MANA, 2500, 30.0,
                    "Holy damage to an enemy."),
    2050: SpellInfo(2050, "Lesser Heal", "priest", 1, 20, POWER_MANA, 2500, 40.0,
                     "Heals a friendly target."),
    589: SpellInfo(589, "Shadow Word: Pain", "priest", 4, 25, POWER_MANA, 0, 30.0,
                    "Shadow damage over time."),
    # Mage
    133: SpellInfo(133, "Fireball", "mage", 1, 30, POWER_MANA, 2500, 30.0,
                    "Fire damage to an enemy."),
    116: SpellInfo(116, "Frostbolt", "mage", 4, 25, POWER_MANA, 2000, 30.0,
                    "Frost damage and a slow to an enemy."),
    143: SpellInfo(143, "Fireball Rank 2", "mage", 4, 45, POWER_MANA, 2500, 30.0,
                    "Fire damage to an enemy (upgraded rank)."),
}


def get_spell_info(spell_id: int) -> SpellInfo | None:
    return SPELL_TABLE.get(spell_id)


# ── CMSG_CAST_SPELL ─────────────────────────────────────────────────────────
# TARGET_FLAG_* (SpellDefines.h) — only the two v1 actually sends.
TARGET_FLAG_NONE = 0x00000000
TARGET_FLAG_UNIT = 0x00000002


def build_cast_spell(spell_id: int, target_guid: int | None = None, cast_count: int = 0) -> bytes:
    """CMSG_CAST_SPELL (SpellCastRequest::operator>>, SpellPackets.cpp):
    uint8 cast_count, int32 spell_id, uint8 send_cast_flags (always 0 — v1
    never sends a missile trajectory update), then SpellTargetData: uint32
    target_flags, packed guid if TARGET_FLAG_UNIT. No item/location/string
    targets in v1 — ground-targeted and item spells aren't supported yet.
    """
    body = struct.pack('<Bi', cast_count, spell_id) + bytes([0])
    if target_guid:
        body += struct.pack('<I', TARGET_FLAG_UNIT) + pk.pack_packed_guid(target_guid)
    else:
        body += struct.pack('<I', TARGET_FLAG_NONE)
    return body


# ── SMSG_CAST_FAILED ─────────────────────────────────────────────────────────
# SpellCastResult (SharedDefines.h) — the full enum (0-187), by name.
SPELL_CAST_RESULT_NAMES = {
    0: 'success', 1: 'affecting_combat', 2: 'already_at_full_health',
    3: 'already_at_full_mana', 4: 'already_at_full_power', 5: 'already_being_tamed',
    6: 'already_have_charm', 7: 'already_have_summon', 8: 'already_open',
    9: 'aura_bounced', 10: 'autotrack_interrupted', 11: 'bad_implicit_targets',
    12: 'bad_targets', 13: 'cant_be_charmed', 14: 'cant_be_disenchanted',
    15: 'cant_be_disenchanted_skill', 16: 'cant_be_milled', 17: 'cant_be_prospected',
    18: 'cant_cast_on_tapped', 19: 'cant_duel_while_invisible',
    20: 'cant_duel_while_stealthed', 21: 'cant_stealth', 22: 'caster_aurastate',
    23: 'caster_dead', 24: 'charmed', 25: 'chest_in_use', 26: 'confused',
    27: 'dont_report', 28: 'equipped_item', 29: 'equipped_item_class',
    30: 'equipped_item_class_mainhand', 31: 'equipped_item_class_offhand',
    32: 'error', 33: 'fizzle', 34: 'fleeing', 35: 'food_lowlevel', 36: 'highlevel',
    37: 'hunger_satiated', 38: 'immune', 39: 'incorrect_area', 40: 'interrupted',
    41: 'interrupted_combat', 42: 'item_already_enchanted', 43: 'item_gone',
    44: 'item_not_found', 45: 'item_not_ready', 46: 'level_requirement',
    47: 'line_of_sight', 48: 'lowlevel', 49: 'low_castlevel', 50: 'mainhand_empty',
    51: 'moving', 52: 'need_ammo', 53: 'need_ammo_pouch', 54: 'need_exotic_ammo',
    55: 'need_more_items', 56: 'nopath', 57: 'not_behind', 58: 'not_fishable',
    59: 'not_flying', 60: 'not_here', 61: 'not_infront', 62: 'not_in_control',
    63: 'not_known', 64: 'not_mounted', 65: 'not_on_taxi', 66: 'not_on_transport',
    67: 'not_ready', 68: 'not_shapeshift', 69: 'not_standing', 70: 'not_tradeable',
    71: 'not_trading', 72: 'not_unsheathed', 73: 'not_while_ghost',
    74: 'not_while_looting', 75: 'no_ammo', 76: 'no_charges_remain',
    77: 'no_champion', 78: 'no_combo_points', 79: 'no_dueling', 80: 'no_endurance',
    81: 'no_fish', 82: 'no_items_while_shapeshifted', 83: 'no_mounts_allowed',
    84: 'no_pet', 85: 'no_power', 86: 'nothing_to_dispel', 87: 'nothing_to_steal',
    88: 'only_abovewater', 89: 'only_daytime', 90: 'only_indoors', 91: 'only_mounted',
    92: 'only_nighttime', 93: 'only_outdoors', 94: 'only_shapeshift',
    95: 'only_stealthed', 96: 'only_underwater', 97: 'out_of_range', 98: 'pacified',
    99: 'possessed', 100: 'reagents', 101: 'requires_area',
    102: 'requires_spell_focus', 103: 'rooted', 104: 'silenced',
    105: 'spell_in_progress', 106: 'spell_learned', 107: 'spell_unavailable',
    108: 'stunned', 109: 'targets_dead', 110: 'target_affecting_combat',
    111: 'target_aurastate', 112: 'target_dueling', 113: 'target_enemy',
    114: 'target_enraged', 115: 'target_friendly', 116: 'target_in_combat',
    117: 'target_is_player', 118: 'target_is_player_controlled',
    119: 'target_not_dead', 120: 'target_not_in_party', 121: 'target_not_looted',
    122: 'target_not_player', 123: 'target_no_pockets', 124: 'target_no_weapons',
    125: 'target_no_ranged_weapons', 126: 'target_unskinnable',
    127: 'thirst_satiated', 128: 'too_close', 129: 'too_many_of_item',
    130: 'totem_category', 131: 'totems', 132: 'try_again', 133: 'unit_not_behind',
    134: 'unit_not_infront', 135: 'wrong_pet_food', 136: 'not_while_fatigued',
    137: 'target_not_in_instance', 138: 'not_while_trading',
    139: 'target_not_in_raid', 140: 'target_freeforall', 141: 'no_edible_corpses',
    142: 'only_battlegrounds', 143: 'target_not_ghost', 144: 'transform_unusable',
    145: 'wrong_weather', 146: 'damage_immune', 147: 'prevented_by_mechanic',
    148: 'play_time', 149: 'reputation', 150: 'min_skill', 151: 'not_in_arena',
    152: 'not_on_shapeshift', 153: 'not_on_stealthed', 154: 'not_on_damage_immune',
    155: 'not_on_mounted', 156: 'too_shallow', 157: 'target_not_in_sanctuary',
    158: 'target_is_trivial', 159: 'bm_or_invisgod', 160: 'expert_riding_requirement',
    161: 'artisan_riding_requirement', 162: 'not_idle', 163: 'not_inactive',
    164: 'partial_playtime', 165: 'no_playtime', 166: 'not_in_battleground',
    167: 'not_in_raid_instance', 168: 'only_in_arena',
    169: 'target_locked_to_raid_instance', 170: 'on_use_enchant',
    171: 'not_on_ground', 172: 'custom_error', 173: 'cant_do_that_right_now',
    174: 'too_many_sockets', 175: 'invalid_glyph', 176: 'unique_glyph',
    177: 'glyph_socket_locked', 178: 'no_valid_targets', 179: 'item_at_max_charges',
    180: 'not_in_barbershop', 181: 'fishing_too_low',
    182: 'item_enchant_trade_window', 183: 'summon_pending', 184: 'max_sockets',
    185: 'pet_can_rename', 186: 'target_cannot_be_resurrected', 187: 'unknown',
}


def parse_cast_failed(payload: bytes) -> dict:
    """SMSG_CAST_FAILED (CastFailed::Write, SpellPackets.cpp): uint8 cast_id,
    uint32 spell_id, uint8 reason, then [int32 failed_arg1] if the packet is
    long enough (>= 10 B) and [int32 failed_arg2] if longer still (>= 14 B)
    — presence isn't flag-encoded on the wire, only implied by the packet's
    total length (known here from the transport framing)."""
    cast_id = payload[0]
    spell_id = pk.u32(payload, 1)
    reason = payload[5]
    info = {"cast_id": cast_id, "spell_id": spell_id, "reason": reason,
            "reason_name": SPELL_CAST_RESULT_NAMES.get(reason, f"reason_{reason}")}
    if len(payload) >= 10:
        info["failed_arg1"] = struct.unpack_from('<i', payload, 6)[0]
    if len(payload) >= 14:
        info["failed_arg2"] = struct.unpack_from('<i', payload, 10)[0]
    return info


# ── SMSG_SPELL_START / SMSG_SPELL_GO (partial parse) ─────────────────────
# SpellCastData (SpellPackets.cpp operator<<) writes CasterGUID, CasterUnit,
# CastID, SpellID, CastFlags, CastTime unconditionally first, THEN a long
# tail of hit-target lists, miss statuses, target data, runes, ammo etc.
# gated by CastFlags bits we don't need to decode. Both opcodes are
# self-delimited packets (nothing follows them on the wire), so stopping
# after the fixed prefix is safe — it just means the tail is left unread.
def parse_spell_cast_prefix(payload: bytes) -> dict:
    caster_guid, off = pk.unpack_packed_guid(payload, 0)
    caster_unit_guid, off = pk.unpack_packed_guid(payload, off)
    cast_id = payload[off]; off += 1
    spell_id = pk.u32(payload, off); off += 4
    cast_flags = pk.u32(payload, off); off += 4
    cast_time = pk.u32(payload, off); off += 4
    return {"caster_guid": caster_guid, "caster_unit_guid": caster_unit_guid,
            "cast_id": cast_id, "spell_id": spell_id, "cast_flags": cast_flags,
            "cast_time": cast_time}


# ── Melee combat events ───────────────────────────────────────────────────
VICTIM_STATE_NAMES = {
    0: "intact", 1: "hit", 2: "dodge", 3: "parry", 4: "interrupt",
    5: "blocks", 6: "evades", 7: "immune", 8: "deflects",
}

HITINFO_UNK1 = 0x00000001
HITINFO_FULL_ABSORB = 0x00000020
HITINFO_PARTIAL_ABSORB = 0x00000040
HITINFO_FULL_RESIST = 0x00000080
HITINFO_PARTIAL_RESIST = 0x00000100
HITINFO_BLOCK = 0x00002000
HITINFO_RAGE_GAIN = 0x00800000


def parse_attack_start(payload: bytes) -> dict:
    """SMSG_ATTACK_START (AttackStart::Write, CombatPackets.cpp): two raw
    (not packed) uint64 guids, attacker then victim."""
    return {"attacker_guid": pk.u64(payload, 0), "victim_guid": pk.u64(payload, 8)}


def parse_attack_stop(payload: bytes) -> dict:
    """SMSG_ATTACK_STOP (SAttackStop::Write): packed attacker guid, packed
    victim guid, uint32 now_dead (bool)."""
    attacker_guid, off = pk.unpack_packed_guid(payload, 0)
    victim_guid, off = pk.unpack_packed_guid(payload, off)
    now_dead = pk.u32(payload, off)
    return {"attacker_guid": attacker_guid, "victim_guid": victim_guid, "now_dead": bool(now_dead)}


def parse_attacker_state_update(payload: bytes) -> dict:
    """SMSG_ATTACKERSTATEUPDATE (AttackerStateUpdate::Write, CombatLogPackets.cpp)."""
    off = 0
    flags = pk.u32(payload, off); off += 4
    attacker_guid, off = pk.unpack_packed_guid(payload, off)
    victim_guid, off = pk.unpack_packed_guid(payload, off)
    damage = pk.u32(payload, off); off += 4
    over_damage = struct.unpack_from('<i', payload, off)[0]; off += 4
    sub_count = payload[off]; off += 1

    sub_school = []
    sub_damage = []
    for _ in range(sub_count):
        sub_school.append(pk.u32(payload, off)); off += 4
        off += 4  # FDamage (float) — same value as Damage below in practice, not needed by callers
        sub_damage.append(pk.u32(payload, off)); off += 4

    if flags & (HITINFO_FULL_ABSORB | HITINFO_PARTIAL_ABSORB):
        off += 4 * sub_count
    if flags & (HITINFO_FULL_RESIST | HITINFO_PARTIAL_RESIST):
        off += 4 * sub_count

    victim_state = payload[off]; off += 1
    attacker_state = pk.u32(payload, off); off += 4
    melee_spell_id = pk.u32(payload, off); off += 4

    if flags & HITINFO_BLOCK:
        off += 4
    if flags & HITINFO_RAGE_GAIN:
        off += 4
    if flags & HITINFO_UNK1:
        off += 4 * 14  # ArmorReduction + 9 floats + Min/MaxDamage[0,1] + SinceLastSwing

    if off != len(payload):
        raise ValueError(f"parsed {off} of {len(payload)} bytes ({len(payload) - off} leftover)")

    return {"flags": flags, "attacker_guid": attacker_guid, "victim_guid": victim_guid,
            "damage": damage, "over_damage": over_damage, "sub_school_mask": sub_school,
            "sub_damage": sub_damage, "victim_state": victim_state,
            "victim_state_name": VICTIM_STATE_NAMES.get(victim_state, f"state_{victim_state}"),
            "attacker_state": attacker_state, "melee_spell_id": melee_spell_id}


def parse_spell_non_melee_damage_log(payload: bytes) -> dict:
    """SMSG_SPELLNONMELEEDAMAGELOG (SpellNonMeleeDamageLog::Write,
    CombatLogPackets.cpp)."""
    off = 0
    target_guid, off = pk.unpack_packed_guid(payload, off)
    caster_guid, off = pk.unpack_packed_guid(payload, off)
    spell_id = struct.unpack_from('<i', payload, off)[0]; off += 4
    damage = struct.unpack_from('<i', payload, off)[0]; off += 4
    overkill = struct.unpack_from('<i', payload, off)[0]; off += 4
    school_mask = payload[off]; off += 1
    absorbed = pk.u32(payload, off); off += 4
    resisted = pk.u32(payload, off); off += 4
    periodic = payload[off]; off += 1
    off += 1  # unused
    shield_block = pk.u32(payload, off); off += 4
    flags = pk.u32(payload, off); off += 4
    # trailing uint8 debug-info flag + optional debug floats not decoded —
    # this server build never sets it (Write()'s DebugInfo branch is
    # commented out upstream), so the packet always ends right here.
    return {"target_guid": target_guid, "caster_guid": caster_guid, "spell_id": spell_id,
            "damage": damage, "overkill": overkill, "school_mask": school_mask,
            "absorbed": absorbed, "resisted": resisted, "periodic": bool(periodic),
            "shield_block": shield_block, "flags": flags}


def parse_party_kill_log(payload: bytes) -> dict:
    """SMSG_PARTYKILLLOG (Unit::Kill, Unit.cpp): two raw uint64 guids,
    killer then victim."""
    return {"killer_guid": pk.u64(payload, 0), "victim_guid": pk.u64(payload, 8)}


def parse_log_xp_gain(payload: bytes) -> dict:
    """SMSG_LOG_XPGAIN (Player::SendLogXPGain, Player.cpp): uint64
    victim_guid (0 for non-kill XP, e.g. quests), uint32 total_xp
    (given + bonus), uint8 type (0 kill, 1 non-kill), [uint32
    xp_without_bonus, float group_bonus_rate — only if victim_guid != 0],
    uint8 recruit_a_friend."""
    off = 0
    victim_guid = pk.u64(payload, off); off += 8
    total_xp = pk.u32(payload, off); off += 4
    xp_type = payload[off]; off += 1
    info = {"victim_guid": victim_guid, "total_xp": total_xp,
            "is_kill": xp_type == 0}
    if victim_guid:
        info["xp_without_bonus"] = pk.u32(payload, off); off += 4
        info["group_bonus_rate"] = pk.f32(payload, off); off += 4
    info["recruit_a_friend"] = bool(payload[off]); off += 1
    return info


def parse_levelup_info(payload: bytes) -> dict:
    """SMSG_LEVELUP_INFO (LevelUpInfo::Write, MiscPackets.cpp): uint32
    level, uint32 health_delta, 7x uint32 power_delta (MAX_POWERS), 5x
    uint32 stat_delta (MAX_STATS: str/agi/sta/int/spirit)."""
    off = 0
    level = pk.u32(payload, off); off += 4
    health_delta = pk.u32(payload, off); off += 4
    power_delta = list(struct.unpack_from('<7I', payload, off)); off += 4 * 7
    stat_delta = list(struct.unpack_from('<5I', payload, off)); off += 4 * 5
    return {"level": level, "health_delta": health_delta, "power_delta": power_delta,
            "stat_delta": stat_delta}
