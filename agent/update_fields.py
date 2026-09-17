#!/usr/bin/env python3
"""VALUES_UPDATE field indices and decoding (WoW 3.3.5a, build 12340).

Every index below is copied from TrinityCore branch `3.3.5`,
src/server/game/Entities/Object/Updates/UpdateFields.h (commit
ed939325374216d6a049e778d6ad864c65283ae6), whose own header comment reads
"Auto generated for version 3, 3, 5, 12340" — the exact build this repo's
client speaks. docs/NEXT-AGENT-HANDOFF.md's field table (ENTRY=0x02,
HEALTH=0x21, ...) does NOT match this source; don't use it.

Field values in EObjectFields (OBJECT_FIELD_GUID..OBJECT_FIELD_PADDING) are
absolute indices. Every other enum's members are written as `OBJECT_END +
n` / `UNIT_END + n` in the header; this module keeps that same relationship
so a future field is one line to add and stays obviously correct.
"""

import struct

from . import update_object as uo

# ── OBJECT (every object type starts here) ──────────────────────────────
OBJECT_FIELD_GUID = 0x00       # Size 2 (guid)
OBJECT_FIELD_TYPE = 0x02
OBJECT_FIELD_ENTRY = 0x03
OBJECT_FIELD_SCALE_X = 0x04    # float
OBJECT_END = 0x06

# ── UNIT (also used by PLAYER, which extends it) ────────────────────────
UNIT_FIELD_TARGET = OBJECT_END + 0x0C          # Size 2 (guid)
UNIT_FIELD_BYTES_0 = OBJECT_END + 0x11         # bytes: race, class, gender, power_type
UNIT_FIELD_HEALTH = OBJECT_END + 0x12
UNIT_FIELD_POWER1 = OBJECT_END + 0x13          # ..POWER7 = +0x19
UNIT_FIELD_MAXHEALTH = OBJECT_END + 0x1A
UNIT_FIELD_MAXPOWER1 = OBJECT_END + 0x1B       # ..MAXPOWER7 = +0x21
UNIT_FIELD_LEVEL = OBJECT_END + 0x30
UNIT_FIELD_FACTIONTEMPLATE = OBJECT_END + 0x31
UNIT_FIELD_FLAGS = OBJECT_END + 0x35
UNIT_FIELD_DISPLAYID = OBJECT_END + 0x3D
UNIT_DYNAMIC_FLAGS = OBJECT_END + 0x49
UNIT_NPC_FLAGS = OBJECT_END + 0x4C
UNIT_END = OBJECT_END + 0x8E

# ── PLAYER (extends UNIT) ────────────────────────────────────────────────
PLAYER_FLAGS = UNIT_END + 0x02
PLAYER_XP = UNIT_END + 0x1E6
PLAYER_NEXT_LEVEL_XP = UNIT_END + 0x1E7
PLAYER_FIELD_COINAGE = UNIT_END + 0x3FE

# Inventory/equipment item-GUID slots (UM-42): each slot is a guid (Size 2:
# low, high uint32). Equipment (0-18) and the 4 equipped-bag-container slots
# (19-22) share one contiguous 23-slot array starting at INV_SLOT_HEAD;
# backpack contents (23-38, 16 slots) are a separate array right after it.
# src/server/game/Entities/Object/Updates/UpdateFields.h,
# src/server/game/Entities/Player/Player.h (EquipmentSlots/InventorySlots/
# InventoryPackSlots enums — the bag/slot numbers CMSG_LOOT's
# CMSG_AUTOSTORE_LOOT_ITEM, CMSG_USE_ITEM, CMSG_DESTROYITEM address).
PLAYER_FIELD_INV_SLOT_HEAD = UNIT_END + 0xB0   # Size 46 (23 guids): equip 0-18 + bag-container 19-22
PLAYER_FIELD_PACK_SLOT_1 = UNIT_END + 0xDE     # Size 32 (16 guids): backpack contents, slot 23-38
EQUIPMENT_SLOT_COUNT = 19
BAG_SLOT_COUNT = 4          # equipped bag containers, slots 19-22
BACKPACK_SLOT_COUNT = 16    # slots 23-38
INVENTORY_SLOT_BAG_0 = 255  # pseudo-bag id meaning "equipped / main backpack" (Player.h)

# ── GAMEOBJECT (extends OBJECT directly, not UNIT) ───────────────────────
GAMEOBJECT_DISPLAYID = OBJECT_END + 0x02
GAMEOBJECT_FLAGS = OBJECT_END + 0x03
GAMEOBJECT_FACTION = OBJECT_END + 0x09
GAMEOBJECT_LEVEL = OBJECT_END + 0x0A

# ── ITEM (extends OBJECT directly, not UNIT) — UM-42 ─────────────────────
# src/server/game/Entities/Object/Updates/UpdateFields.h
ITEM_FIELD_OWNER = OBJECT_END + 0x00           # Size 2 (guid)
ITEM_FIELD_CONTAINED = OBJECT_END + 0x02       # Size 2 (guid) — the bag this item sits in, if any
ITEM_FIELD_CREATOR = OBJECT_END + 0x04         # Size 2 (guid)
ITEM_FIELD_STACK_COUNT = OBJECT_END + 0x08
ITEM_FIELD_FLAGS = OBJECT_END + 0x0F
ITEM_FIELD_PROPERTY_SEED = OBJECT_END + 0x34
ITEM_FIELD_RANDOM_PROPERTIES_ID = OBJECT_END + 0x35
ITEM_FIELD_DURABILITY = OBJECT_END + 0x36
ITEM_FIELD_MAXDURABILITY = OBJECT_END + 0x37

# ── Type hints for the handful of fields decode_fields() reinterprets ────
FIELD_TYPE_FLOAT = "float"
FIELD_TYPE_GUID = "guid"      # occupies this slot and the next (low, high)
FIELD_TYPES = {
    OBJECT_FIELD_SCALE_X: FIELD_TYPE_FLOAT,
    UNIT_FIELD_TARGET: FIELD_TYPE_GUID,
}


def reinterpret_float(raw_u32: int) -> float:
    """A VALUES_UPDATE slot is always transmitted as a raw uint32; float
    fields need the bit pattern reinterpreted, not converted."""
    return struct.unpack('<f', struct.pack('<I', raw_u32 & 0xFFFFFFFF))[0]


def _guid_from_slots(raw: dict, index: int):
    if index not in raw:
        return None
    return raw[index] | (raw.get(index + 1, 0) << 32)


def decode_fields(object_type: int, raw: dict) -> dict:
    """Named, typed values from `raw` (agent.update_object's VALUES_UPDATE
    field dict: {field_index: uint32}). Only maps the fields listed in the
    UM-33 card; anything else stays reachable via `raw_fields` rather than
    being silently dropped — cheaper to keep than to re-add slots every time
    a new field turns out to matter."""
    out = {"raw_fields": dict(raw)}

    if OBJECT_FIELD_ENTRY in raw:
        out["entry"] = raw[OBJECT_FIELD_ENTRY]
    if OBJECT_FIELD_SCALE_X in raw:
        out["scale"] = reinterpret_float(raw[OBJECT_FIELD_SCALE_X])

    if object_type in (uo.TYPEID_UNIT, uo.TYPEID_PLAYER):
        if UNIT_FIELD_HEALTH in raw:
            out["health"] = raw[UNIT_FIELD_HEALTH]
        if UNIT_FIELD_MAXHEALTH in raw:
            out["max_health"] = raw[UNIT_FIELD_MAXHEALTH]
        if UNIT_FIELD_LEVEL in raw:
            out["level"] = raw[UNIT_FIELD_LEVEL]
        if UNIT_FIELD_FACTIONTEMPLATE in raw:
            out["faction"] = raw[UNIT_FIELD_FACTIONTEMPLATE]
        if UNIT_FIELD_FLAGS in raw:
            out["unit_flags"] = raw[UNIT_FIELD_FLAGS]
        if UNIT_DYNAMIC_FLAGS in raw:
            out["dynamic_flags"] = raw[UNIT_DYNAMIC_FLAGS]
        if UNIT_NPC_FLAGS in raw:
            out["npc_flags"] = raw[UNIT_NPC_FLAGS]
        if UNIT_FIELD_DISPLAYID in raw:
            out["display_id"] = raw[UNIT_FIELD_DISPLAYID]

        target_guid = _guid_from_slots(raw, UNIT_FIELD_TARGET)
        if target_guid is not None:
            out["target_guid"] = target_guid

        power = [raw[UNIT_FIELD_POWER1 + i] for i in range(7) if (UNIT_FIELD_POWER1 + i) in raw]
        if power:
            out["power"] = power
        max_power = [raw[UNIT_FIELD_MAXPOWER1 + i] for i in range(7) if (UNIT_FIELD_MAXPOWER1 + i) in raw]
        if max_power:
            out["max_power"] = max_power

        if UNIT_FIELD_BYTES_0 in raw:
            bytes0 = raw[UNIT_FIELD_BYTES_0]
            out["race"] = bytes0 & 0xFF
            out["class_"] = (bytes0 >> 8) & 0xFF
            out["gender"] = (bytes0 >> 16) & 0xFF
            out["power_type"] = (bytes0 >> 24) & 0xFF

    if object_type == uo.TYPEID_PLAYER:
        if PLAYER_FLAGS in raw:
            out["player_flags"] = raw[PLAYER_FLAGS]
        if PLAYER_XP in raw:
            out["xp"] = raw[PLAYER_XP]
        if PLAYER_NEXT_LEVEL_XP in raw:
            out["next_level_xp"] = raw[PLAYER_NEXT_LEVEL_XP]
        if PLAYER_FIELD_COINAGE in raw:
            out["coinage"] = raw[PLAYER_FIELD_COINAGE]

    if object_type == uo.TYPEID_GAMEOBJECT:
        if GAMEOBJECT_DISPLAYID in raw:
            out["display_id"] = raw[GAMEOBJECT_DISPLAYID]
        if GAMEOBJECT_FLAGS in raw:
            out["gameobject_flags"] = raw[GAMEOBJECT_FLAGS]
        if GAMEOBJECT_FACTION in raw:
            out["faction"] = raw[GAMEOBJECT_FACTION]
        if GAMEOBJECT_LEVEL in raw:
            out["level"] = raw[GAMEOBJECT_LEVEL]

    if object_type in (uo.TYPEID_ITEM, uo.TYPEID_CONTAINER):
        out.update(decode_item_fields(raw))

    return out


def decode_item_fields(raw: dict) -> dict:
    """Named, typed values for an ITEM/CONTAINER object's VALUES_UPDATE
    fields (UM-42) — used both from decode_fields() above (for objects
    perceived generically through update-object) and directly by
    agent.perception.WorldState when building session.inventory from the
    self player's own item objects."""
    out = {}
    if OBJECT_FIELD_ENTRY in raw:
        out["entry"] = raw[OBJECT_FIELD_ENTRY]
    owner = _guid_from_slots(raw, ITEM_FIELD_OWNER)
    if owner is not None:
        out["owner_guid"] = owner
    contained = _guid_from_slots(raw, ITEM_FIELD_CONTAINED)
    if contained is not None:
        out["contained_guid"] = contained
    if ITEM_FIELD_STACK_COUNT in raw:
        out["count"] = raw[ITEM_FIELD_STACK_COUNT]
    if ITEM_FIELD_FLAGS in raw:
        out["item_flags"] = raw[ITEM_FIELD_FLAGS]
    if ITEM_FIELD_RANDOM_PROPERTIES_ID in raw:
        out["random_property_id"] = raw[ITEM_FIELD_RANDOM_PROPERTIES_ID]
    if ITEM_FIELD_PROPERTY_SEED in raw:
        out["property_seed"] = raw[ITEM_FIELD_PROPERTY_SEED]
    if ITEM_FIELD_DURABILITY in raw:
        out["durability"] = raw[ITEM_FIELD_DURABILITY]
    if ITEM_FIELD_MAXDURABILITY in raw:
        out["max_durability"] = raw[ITEM_FIELD_MAXDURABILITY]
    return out


def decode_equipment_and_inventory_guids(raw: dict) -> dict:
    """The self player's equipment (slots 0-18), equipped-bag-container
    (slots 19-22) and backpack (slots 23-38) item GUIDs, keyed by slot
    number 0-38 (matching the bag/slot numbering CMSG_AUTOSTORE_LOOT_ITEM /
    CMSG_USE_ITEM / CMSG_DESTROYITEM use with bag=INVENTORY_SLOT_BAG_0).
    Only nonzero (occupied) slots are included."""
    out = {}
    total = EQUIPMENT_SLOT_COUNT + BAG_SLOT_COUNT
    for i in range(total):
        guid = _guid_from_slots(raw, PLAYER_FIELD_INV_SLOT_HEAD + i * 2)
        if guid:
            out[i] = guid
    for i in range(BACKPACK_SLOT_COUNT):
        guid = _guid_from_slots(raw, PLAYER_FIELD_PACK_SLOT_1 + i * 2)
        if guid:
            out[EQUIPMENT_SLOT_COUNT + BAG_SLOT_COUNT + i] = guid
    return out
