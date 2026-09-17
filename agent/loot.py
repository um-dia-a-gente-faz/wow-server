#!/usr/bin/env python3
"""Loot request builders + response parsers (UM-42), pure functions, no I/O —
see agent/session.py for the opcodes' dispatch and agent/actions.py for the
loot() action that drives the loot/loot_money/autostore/loot_release
sequence.

Every wire layout below is copied from TrinityCore branch `3.3.5` and cited
by file:
  src/server/game/Handlers/LootHandler.cpp (WorldSession::Handle*Opcode)
  src/server/game/Server/Packets/LootPackets.h/.cpp (WorldPackets::Loot::*)
  src/server/game/Loot/Loot.h (LootType, LootError enums)
  src/server/game/Entities/Player/Player.cpp (SendLoot, SendLootRelease,
    SendLootError, SendNewItem — the last three build their packets by hand,
    not through a WorldPackets::Loot class)
  src/server/game/Entities/Object/ObjectGuid.cpp (operator<<(ByteBuffer&,
    ObjectGuid const&) — writes a *raw*, not packed, uint64; every loot-owner
    guid on the wire below is raw for that reason, unlike the packed guids
    used elsewhere in update-object parsing)
  src/server/game/Server/Protocol/Opcodes.h
"""

import struct

from . import packets as pk

# Opcodes (Opcodes.h)
CMSG_USE_ITEM                   = 0x0AB
CMSG_DESTROYITEM                = 0x111
SMSG_INVENTORY_CHANGE_FAILURE   = 0x112
CMSG_AUTOSTORE_LOOT_ITEM        = 0x108
CMSG_LOOT                       = 0x15D
CMSG_LOOT_MONEY                 = 0x15E
CMSG_LOOT_RELEASE                = 0x15F
SMSG_LOOT_RESPONSE               = 0x160
SMSG_LOOT_RELEASE_RESPONSE       = 0x161
SMSG_LOOT_REMOVED                = 0x162
SMSG_LOOT_MONEY_NOTIFY           = 0x163
SMSG_LOOT_CLEAR_MONEY            = 0x165
SMSG_ITEM_PUSH_RESULT            = 0x166
CMSG_ITEM_QUERY_SINGLE           = 0x056
SMSG_ITEM_QUERY_SINGLE_RESPONSE  = 0x058

# LootType (Loot.h) — the "AcquireReason" byte in SMSG_LOOT_RESPONSE. 0
# (LOOT_NONE) never appears on a successful response (Player::SendLoot always
# passes a nonzero LootType in); it's what the *failure* branch implies by
# leaving AcquireReason at its 0 default (Player::SendLootError only sets
# FailureReason).
LOOT_NONE = 0
LOOT_CORPSE = 1
LOOT_PICKPOCKETING = 2
LOOT_FISHING = 3
LOOT_DISENCHANTING = 4
LOOT_SKINNING = 6
LOOT_PROSPECTING = 7
LOOT_MILLING = 8

# LootError (Loot.h) — FailureReason on a failed SMSG_LOOT_RESPONSE.
LOOT_ERROR_NAMES = {
    0: "didnt_kill", 4: "too_far", 5: "bad_facing", 6: "locked",
    8: "not_standing", 9: "stunned", 10: "player_not_found",
    11: "play_time_exceeded", 12: "master_inv_full", 13: "master_unique_item",
    14: "master_other", 15: "already_pickpocketed", 16: "not_while_shapeshifted",
}

# EquipError / InventoryResult (ItemDefines.h) — only the couple of values
# callers (agent/actions.py) need to recognize by name; everything else stays
# reachable as the raw int.
EQUIP_ERR_OK = 0
EQUIP_ERR_INV_FULL = 50


def build_loot(guid: int) -> bytes:
    """CMSG_LOOT (0x15D): LootUnit::Read (LootPackets.cpp) — a single raw
    (not packed) uint64 guid of the corpse/creature/gameobject/item to loot."""
    return struct.pack('<Q', guid)


def build_loot_money() -> bytes:
    """CMSG_LOOT_MONEY (0x15E): LootMoney::Read is empty — the server acts on
    the player's currently-open loot (Player::GetLootGUID()), no payload."""
    return b''


def build_autostore_loot_item(loot_list_id: int) -> bytes:
    """CMSG_AUTOSTORE_LOOT_ITEM (0x108): LootItem::Read (LootPackets.cpp) — a
    single uint8 LootListID (the slot index from SMSG_LOOT_RESPONSE's Items,
    not an inventory slot)."""
    return struct.pack('<B', loot_list_id)


def build_loot_release(guid: int) -> bytes:
    """CMSG_LOOT_RELEASE (0x15F): LootRelease::Read — a single raw uint64 guid."""
    return struct.pack('<Q', guid)


def parse_loot_response(payload: bytes) -> dict:
    """SMSG_LOOT_RESPONSE (0x160): WorldPackets::Loot::LootResponse::Write
    (LootPackets.cpp). Layout: uint64 owner (raw guid), uint8 acquire_reason
    (LootType; nonzero on success), then:
      success (acquire_reason != 0): uint32 coins, uint8 item_count, then
        item_count * LootItemData (LootPackets.cpp's operator<<): uint8
        loot_list_id, uint32 item_id, uint32 quantity, uint32
        item_display_info_id, int32 random_properties_seed, int32
        random_properties_id, uint8 ui_type (LootSlotType).
      failure (acquire_reason == 0): uint8 failure_reason (LootError).
    """
    off = 0
    owner = pk.u64(payload, off); off += 8
    acquire_reason = payload[off]; off += 1
    info = {"guid": owner, "loot_type": acquire_reason, "success": acquire_reason != 0}

    if acquire_reason:
        coins = pk.u32(payload, off); off += 4
        item_count = payload[off]; off += 1
        items = []
        for _ in range(item_count):
            slot = payload[off]; off += 1
            item_id = pk.u32(payload, off); off += 4
            quantity = pk.u32(payload, off); off += 4
            display_info_id = pk.u32(payload, off); off += 4
            random_suffix = struct.unpack_from('<i', payload, off)[0]; off += 4
            random_property = struct.unpack_from('<i', payload, off)[0]; off += 4
            ui_type = payload[off]; off += 1
            items.append({
                "slot": slot, "entry": item_id, "count": quantity,
                "display_id": display_info_id, "random_suffix": random_suffix,
                "random_property": random_property, "ui_type": ui_type,
            })
        info["coins"] = coins
        info["items"] = items
    else:
        failure_reason = payload[off]; off += 1
        info["failure_reason"] = failure_reason
        info["failure_reason_name"] = LOOT_ERROR_NAMES.get(failure_reason, f"error_{failure_reason}")

    if off != len(payload):
        raise ValueError(f"parsed {off} of {len(payload)} bytes ({len(payload) - off} leftover)")
    return info


def parse_loot_release_response(payload: bytes) -> dict:
    """SMSG_LOOT_RELEASE_RESPONSE (0x161): Player::SendLootRelease
    (Player.cpp) — uint64 guid (raw), uint8 (always 1 in this build; not
    otherwise meaningful to callers)."""
    guid = pk.u64(payload, 0)
    return {"guid": guid}


def parse_loot_removed(payload: bytes) -> dict:
    """SMSG_LOOT_REMOVED (0x162): WorldPackets::Loot::LootRemoved::Write — a
    single uint8 LootListID (the slot that was just looted/rolled away)."""
    return {"slot": payload[0]}


def parse_loot_money_notify(payload: bytes) -> dict:
    """SMSG_LOOT_MONEY_NOTIFY (0x163): WorldPackets::Loot::LootMoneyNotify::Write
    — uint32 money, uint8 sole_looter (bool)."""
    money = pk.u32(payload, 0)
    sole_looter = bool(payload[4])
    return {"money": money, "sole_looter": sole_looter}


def parse_item_push_result(payload: bytes) -> dict:
    """SMSG_ITEM_PUSH_RESULT (0x166): Player::SendNewItem (Player.cpp) — a
    hand-built WorldPacket, not a WorldPackets::Item class:
      uint64 player_guid (raw), uint32 received (0=looted, 1=from npc/other),
      uint32 created (0=received an existing item, 1=a brand-new one), uint32
      send_chat_message (bool), uint8 bag_slot, uint32 slot (0xFFFFFFFF if
      merged into an existing stack instead of a fresh slot), uint32 entry,
      uint32 suffix_factor, int32 random_property_id, uint32 count, uint32
      total_count_in_inventory.
    """
    off = 0
    player_guid = pk.u64(payload, off); off += 8
    received = pk.u32(payload, off); off += 4
    created = pk.u32(payload, off); off += 4
    send_chat_message = pk.u32(payload, off); off += 4
    bag_slot = payload[off]; off += 1
    slot = pk.u32(payload, off); off += 4
    entry = pk.u32(payload, off); off += 4
    suffix_factor = pk.u32(payload, off); off += 4
    random_property_id = struct.unpack_from('<i', payload, off)[0]; off += 4
    count = pk.u32(payload, off); off += 4
    total_count = pk.u32(payload, off); off += 4
    return {
        "player_guid": player_guid,
        "received": bool(received),
        "created": bool(created),
        "bag_slot": bag_slot,
        "slot": None if slot == 0xFFFFFFFF else slot,
        "entry": entry,
        "suffix_factor": suffix_factor,
        "random_property_id": random_property_id,
        "count": count,
        "total_count": total_count,
    }


def parse_inventory_change_failure(payload: bytes) -> dict:
    """SMSG_INVENTORY_CHANGE_FAILURE (0x112): WorldPackets::Item::
    InventoryChangeFailure::Write (ItemPackets.cpp) — uint8 bag_result
    (InventoryResult/EquipError; EQUIP_ERR_OK=0 never actually sent as this
    opcode). If nonzero: two raw uint64 item guids (Item[0]/Item[1] — which
    of the two is populated depends on the specific error, e.g. INV_FULL
    leaves both empty), uint8 container_bag_slot, then a further
    error-specific tail (int32 level, or a rebind confirmation, or a limit
    category id) this parser does not decode — the three fields above are
    enough to recognize "inventory full" and similar loot-blocking errors,
    and nothing follows them in the packet that callers need yet."""
    result = payload[0]
    info = {"result": result, "ok": result == EQUIP_ERR_OK}
    if result != EQUIP_ERR_OK and len(payload) >= 1 + 8 + 8 + 1:
        item0 = pk.u64(payload, 1)
        item1 = pk.u64(payload, 9)
        container_bag_slot = payload[17]
        info["item_guids"] = [g for g in (item0, item1) if g]
        info["container_bag_slot"] = container_bag_slot
    return info


def build_use_item(bag: int, slot: int, item_guid: int, spell_id: int = 0,
                    target_guid: int | None = None, cast_count: int = 0,
                    glyph_index: int = 0) -> bytes:
    """CMSG_USE_ITEM (0x0AB): WorldSession::HandleUseItemOpcode
    (SpellHandler.cpp) reads: uint8 bagIndex, uint8 slot, uint8 castCount,
    uint32 spellId, uint64 itemGUID (raw), uint32 glyphIndex, uint8
    castFlags, then a SpellCastTargets (Spell.cpp SpellCastTargets::Read):
    uint32 target_mask, and — only if target_mask is nonzero — one packed
    guid per set bit category. For a self-targeted consumable (food/water),
    target_mask 0 (TARGET_FLAG_NONE) means nothing else follows; passing
    target_guid sends TARGET_FLAG_UNIT (0x2) + that guid instead (e.g. a
    bandage used on an ally). castFlags is left at 0 so
    HandleClientCastFlags reads no extra movement/trajectory data."""
    payload = struct.pack('<BBBI', bag, slot, cast_count, spell_id)
    payload += struct.pack('<Q', item_guid)
    payload += struct.pack('<IB', glyph_index, 0)  # glyph_index, cast_flags=0
    if target_guid is None:
        payload += struct.pack('<I', 0)  # TARGET_FLAG_NONE
    else:
        payload += struct.pack('<I', 0x2)  # TARGET_FLAG_UNIT
        payload += pk.pack_packed_guid(target_guid)
    return payload


def build_destroy_item(bag: int, slot: int, count: int) -> bytes:
    """CMSG_DESTROYITEM (0x111): WorldPackets::Item::DestroyItem::Read
    (ItemPackets.cpp) — uint8 ContainerId, uint8 SlotNum, uint32 Count."""
    return struct.pack('<BBI', bag, slot, count)


def build_item_query(item_id: int) -> bytes:
    """CMSG_ITEM_QUERY_SINGLE (0x056): WorldPackets::Query::QueryItemSingle::Read
    (QueryPackets.cpp) — a single uint32 item entry (no guid, unlike the
    creature/gameobject queries)."""
    return struct.pack('<I', item_id)


# ItemSpellTriggerType (ItemTemplate.h) — the trigger byte agent.actions'
# use_item cares about: only an ITEM_SPELLTRIGGER_ON_USE spell is what a
# right-click/CMSG_USE_ITEM actually invokes.
ITEM_SPELLTRIGGER_ON_USE = 0

# MAX_ITEM_PROTO_SPELLS (ItemTemplate.h) — SMSG_ITEM_QUERY_SINGLE_RESPONSE
# always writes exactly this many (spell_id<=0) placeholder slots.
MAX_ITEM_PROTO_SPELLS = 5


def parse_item_query_response(payload: bytes) -> dict:
    """SMSG_ITEM_QUERY_SINGLE_RESPONSE (0x058): WorldPackets::Query::
    QueryItemSingleResponse::Write (QueryPackets.cpp). Only the fields
    agent.actions/agent.perception need are named below; the rest of the
    (large, mostly stat-block) payload is walked past by byte count so the
    trailing-leftover check still catches a parser bug.
    """
    off = 0
    raw_entry = pk.u32(payload, off); off += 4
    found = not (raw_entry & 0x80000000)
    entry = raw_entry & 0x7FFFFFFF
    info = {"entry": entry, "found": found}
    if not found:
        return info

    item_class = pk.u32(payload, off); off += 4
    subclass = pk.u32(payload, off); off += 4
    off += 4  # sound_override_subclass (int32) — not needed by callers yet
    name, off = pk.cstring(payload, off)
    off += 3  # name2, name3, name4 — always empty
    display_info_id = pk.u32(payload, off); off += 4
    quality = pk.u32(payload, off); off += 4
    off += 4 * 2  # Flags[MAX_ITEM_PROTO_FLAGS=2]
    off += 4  # buy_price (int32)
    off += 4  # sell_price
    inventory_type = pk.u32(payload, off); off += 4
    off += 4 * 2  # allowable_class, allowable_race
    off += 4  # item_level
    required_level = pk.u32(payload, off); off += 4
    off += 4 * 4  # required_skill, required_skill_rank, required_spell, required_honor_rank
    off += 4 * 2  # required_city_rank, required_reputation_faction
    off += 4  # required_reputation_rank
    off += 4  # max_count (int32)
    stackable = struct.unpack_from('<i', payload, off)[0]; off += 4
    off += 4  # container_slots
    stats_count = pk.u32(payload, off); off += 4
    off += 8 * stats_count  # ItemStat[i]: uint32 type, int32 value
    off += 4 * 2  # scaling_stat_distribution, scaling_stat_value
    off += 12 * 2  # Damage[MAX_ITEM_PROTO_DAMAGES=2]: float min, float max, uint32 type
    off += 4 * 7  # Resistance[MAX_SPELL_SCHOOL=7]
    off += 4  # delay
    off += 4  # ammo_type
    off += 4  # ranged_mod_range (float)

    spells = []
    for _ in range(MAX_ITEM_PROTO_SPELLS):
        spell_id = struct.unpack_from('<i', payload, off)[0]; off += 4
        trigger = pk.u32(payload, off); off += 4
        off += 4  # spell_charges (int32)
        off += 4  # spell_cooldown (uint32, per Write()'s explicit cast)
        off += 4  # spell_category
        off += 4  # spell_category_cooldown
        if spell_id > 0:
            spells.append({"spell_id": spell_id, "trigger": trigger})

    off += 4  # bonding
    _, off = pk.cstring(payload, off)  # description — not needed by callers yet
    off += 4 * 6  # page_text, language_id, page_material, start_quest, lock_id, material(int32)
    off += 4  # sheath
    off += 4 * 2  # random_property, random_suffix
    off += 4  # block
    off += 4  # item_set
    max_durability = pk.u32(payload, off); off += 4
    off += 4 * 2  # area, map
    off += 4 * 2  # bag_family, totem_category
    off += 8 * 3  # Socket[MAX_ITEM_PROTO_SOCKETS=3]
    off += 4  # socket_bonus
    off += 4  # gem_properties
    off += 4  # required_disenchant_skill
    off += 4  # armor_damage_modifier (float)
    off += 4  # duration
    off += 4  # item_limit_category
    off += 4  # holiday_id

    if off != len(payload):
        raise ValueError(f"parsed {off} of {len(payload)} bytes ({len(payload) - off} leftover)")

    info.update({
        "class_": item_class, "subclass": subclass, "name": name,
        "display_id": display_info_id, "quality": quality,
        "inventory_type": inventory_type, "required_level": required_level,
        "stackable": stackable, "max_durability": max_durability,
        "spells": spells,
    })
    return info
