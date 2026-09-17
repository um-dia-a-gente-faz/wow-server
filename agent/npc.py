#!/usr/bin/env python3
"""NPC interaction (UM-40): gossip/vendor/trainer/gameobject request
builders + response parsers (pure, no I/O — see agent/session.py for the
opcodes' dispatch, agent/perception.py for the ui_state ('window') this
feeds), plus a small NpcTextCache mirroring agent.names.NameCache's shape
for gossip body text (SMSG_NPC_TEXT_UPDATE), keyed by text id.

Every wire layout below is copied from TrinityCore branch `3.3.5`:
  src/server/game/Server/Protocol/Opcodes.h (opcode values)
  src/server/game/Entities/Creature/GossipDef.cpp (PlayerMenu::SendGossipMenu)
  src/server/game/Server/Packets/NPCPackets.h/.cpp (Hello, GossipMessage,
    TrainerList request/response structs — CMSG_GOSSIP_HELLO,
    CMSG_LIST_INVENTORY and CMSG_TRAINER_LIST all share the `Hello` struct:
    a single raw uint64 guid)
  src/server/game/Handlers/MiscHandler.cpp (HandleGossipSelectOptionOpcode)
  src/server/game/Handlers/QueryHandler.cpp (QueryNPCText request/response)
  src/server/game/Handlers/ItemHandler.cpp (HandleListInventoryOpcode via
    Hello, HandleBuyItemOpcode/HandleSellItemOpcode — buy is still a raw
    WorldPacket in this branch, not a packet struct)
  src/server/game/Server/Packets/ItemPackets.cpp (SellItem::Read)
  src/server/game/Server/Packets/GameObjectPackets.cpp (GameObjUse::Read)

All GUIDs in this file are the *raw* 8-byte form (struct.pack('<Q', ...)),
not the variable-length packed-guid format movement/update-object use —
verified against each struct's operator<</operator>> above.
"""

import struct
import time

from . import packets as pk

# Opcodes (Opcodes.h)
CMSG_GOSSIP_HELLO          = 0x17B
CMSG_GOSSIP_SELECT_OPTION  = 0x17C
SMSG_GOSSIP_MESSAGE        = 0x17D
SMSG_GOSSIP_COMPLETE       = 0x17E
CMSG_NPC_TEXT_QUERY        = 0x17F
SMSG_NPC_TEXT_UPDATE       = 0x180
CMSG_LIST_INVENTORY        = 0x19E
SMSG_LIST_INVENTORY        = 0x19F
CMSG_SELL_ITEM             = 0x1A0
SMSG_SELL_ITEM             = 0x1A1
CMSG_BUY_ITEM              = 0x1A2
CMSG_BUY_ITEM_IN_SLOT      = 0x1A3
SMSG_BUY_ITEM              = 0x1A4
SMSG_BUY_FAILED            = 0x1A5
CMSG_TRAINER_LIST          = 0x1B0
SMSG_TRAINER_LIST          = 0x1B1
CMSG_TRAINER_BUY_SPELL     = 0x1B2
SMSG_TRAINER_BUY_SUCCEEDED = 0x1B3
SMSG_TRAINER_BUY_FAILED    = 0x1B4
CMSG_GAMEOBJ_USE           = 0x0B1

MAX_NPC_TEXT_OPTIONS = 8  # MAX_GOSSIP_TEXT_OPTIONS, GossipDef.h
MAX_NPC_TEXT_EMOTES = 3   # MAX_GOSSIP_TEXT_EMOTES, GossipDef.h

INTERACT_RANGE_YD = 5.0  # matches actions.MELEE_RANGE_YD — "in range" for interact()


# ── Request builders ──────────────────────────────────────────────────────

def build_gossip_hello(guid: int) -> bytes:
    """CMSG_GOSSIP_HELLO (0x17B): WorldPackets::NPC::Hello::Read — a single
    raw (not packed) uint64 guid."""
    return struct.pack('<Q', guid)


def build_list_inventory(guid: int) -> bytes:
    """CMSG_LIST_INVENTORY (0x19E): same `Hello` struct as gossip hello."""
    return struct.pack('<Q', guid)


def build_trainer_list(guid: int) -> bytes:
    """CMSG_TRAINER_LIST (0x1B0): same `Hello` struct as gossip hello."""
    return struct.pack('<Q', guid)


def build_gameobj_use(guid: int) -> bytes:
    """CMSG_GAMEOBJ_USE (0x0B1): GameObjUse::Read — a single raw uint64 guid."""
    return struct.pack('<Q', guid)


def build_npc_text_query(text_id: int, guid: int) -> bytes:
    """CMSG_NPC_TEXT_QUERY (0x17F): QueryNPCText::Read — uint32 TextID, then
    a raw uint64 Guid (TextID first)."""
    return struct.pack('<IQ', text_id, guid)


def build_gossip_select_option(guid: int, menu_id: int, gossip_list_id: int,
                                code: str | None = None) -> bytes:
    """CMSG_GOSSIP_SELECT_OPTION (0x17C): HandleGossipSelectOptionOpcode
    (MiscHandler.cpp) reads `guid >> menuId >> gossipListId` (raw guid, then
    two uint32s), then a cstring `code` only for options the cached menu
    marked "coded" (a password/promo-code text box) — `code` is omitted
    entirely for a normal option, not sent as an empty string."""
    payload = struct.pack('<QII', guid, menu_id, gossip_list_id)
    if code is not None:
        payload += code.encode('utf-8') + b'\x00'
    return payload


def build_buy_item(vendor_guid: int, item_entry: int, slot: int, count: int) -> bytes:
    """CMSG_BUY_ITEM (0x1A2): HandleBuyItemOpcode (ItemHandler.cpp), still a
    raw WorldPacket in this branch (no packet struct): ObjectGuid vendorguid,
    uint32 item (entry), uint32 slot (1-based vendor slot — the client sends
    slot+1, the server does --slot; callers of this builder pass the
    already-1-based slot they got from SMSG_LIST_INVENTORY's `slot` field,
    which is itself MuID = vendor slot + 1), uint32 count, uint8 unk1 (always
    1 in the retail client; not consumed for anything meaningful server-side)."""
    return struct.pack('<QIIIB', vendor_guid, item_entry, slot, count, 1)


def build_sell_item(vendor_guid: int, item_guid: int, amount: int) -> bytes:
    """CMSG_SELL_ITEM (0x1A0): SellItem::Read (ItemPackets.cpp) — ObjectGuid
    VendorGUID, ObjectGuid ItemGUID (the *item's own* GUID, looked up
    server-side via GetItemByGuid — NOT a bag/slot pair), uint32 Amount
    (0 = sell the whole stack, per HandleSellItemOpcode)."""
    return struct.pack('<QQI', vendor_guid, item_guid, amount)


def build_train_spell(trainer_guid: int, spell_id: int) -> bytes:
    """CMSG_TRAINER_BUY_SPELL (0x1B2): TrainerBuySpell::Read — ObjectGuid
    TrainerGUID, int32 SpellID."""
    return struct.pack('<Qi', trainer_guid, spell_id)


# ── Response parsers ──────────────────────────────────────────────────────

def parse_gossip_message(payload: bytes) -> dict:
    """SMSG_GOSSIP_MESSAGE (0x17D): GossipMessage::Write, called from
    PlayerMenu::SendGossipMenu (GossipDef.cpp). Raw uint64 GossipGUID, int32
    GossipID (menu id), int32 RandomTextID (npc text id), uint32
    GossipOptionsCount, then per option: int32 GossipOptionID (sequential
    index), uint8 OptionNPC (icon), int8 OptionFlags (IsCoded — nonzero
    means this option needs a text-entry box), uint32 OptionCost (BoxMoney),
    cstring Text, cstring Confirm (BoxMessage); then uint32 GossipTextCount
    (quest items shown in this menu), then per quest: int32 QuestID, int32
    QuestType, int32 QuestLevel, int32 QuestFlags, uint8 Repeatable, cstring
    QuestTitle — this field order (Type before Level) is what the actual
    `operator<<` writes, not the struct's declaration order."""
    off = 0
    guid = pk.u64(payload, off); off += 8
    menu_id = struct.unpack_from('<i', payload, off)[0]; off += 4
    text_id = struct.unpack_from('<i', payload, off)[0]; off += 4
    option_count = pk.u32(payload, off); off += 4

    options = []
    for _ in range(option_count):
        index = struct.unpack_from('<i', payload, off)[0]; off += 4
        icon = payload[off]; off += 1
        coded = bool(struct.unpack_from('<b', payload, off)[0]); off += 1
        box_money = pk.u32(payload, off); off += 4
        text, off = pk.cstring(payload, off)
        box_text, off = pk.cstring(payload, off)
        options.append({
            "index": index, "icon": icon, "coded": coded,
            "box_money": box_money, "text": text, "box_text": box_text,
        })

    quest_count = pk.u32(payload, off); off += 4
    quests = []
    for _ in range(quest_count):
        quest_id = struct.unpack_from('<i', payload, off)[0]; off += 4
        quest_type = struct.unpack_from('<i', payload, off)[0]; off += 4
        level = struct.unpack_from('<i', payload, off)[0]; off += 4
        flags = struct.unpack_from('<i', payload, off)[0]; off += 4
        repeatable = bool(payload[off]); off += 1
        title, off = pk.cstring(payload, off)
        quests.append({
            "quest_id": quest_id, "quest_type": quest_type, "level": level,
            "flags": flags, "repeatable": repeatable, "title": title,
        })

    return {
        "npc_guid": guid, "menu_id": menu_id, "text_id": text_id,
        "options": options, "quests": quests,
    }


def parse_list_inventory(payload: bytes) -> dict:
    """SMSG_LIST_INVENTORY (0x19F): VendorInventory::Write (NPCPackets.cpp).
    Raw uint64 Vendor guid, uint8 ItemCount (not uint32), then per item:
    int32 MuID (vendor slot, 1-based — pass straight through to
    build_buy_item's `slot`), uint32 Item (entry), uint32 ItemDisplayInfoID,
    int32 Quantity (stock left; -1 = unlimited), int32 Price (unit price,
    already reputation-discounted server-side), int32 Durability (item
    template's MaxDurability), int32 StackCount (buy count per purchase),
    int32 ExtendedCostID. If ItemCount == 0, one trailing int8 Reason (e.g.
    vendor has nothing to sell) — otherwise nothing follows the last item."""
    off = 0
    vendor_guid = pk.u64(payload, off); off += 8
    item_count = payload[off]; off += 1

    items = []
    for _ in range(item_count):
        slot = struct.unpack_from('<i', payload, off)[0]; off += 4
        entry = pk.u32(payload, off); off += 4
        display_id = pk.u32(payload, off); off += 4
        quantity = struct.unpack_from('<i', payload, off)[0]; off += 4
        price = struct.unpack_from('<i', payload, off)[0]; off += 4
        durability = struct.unpack_from('<i', payload, off)[0]; off += 4
        stack_count = struct.unpack_from('<i', payload, off)[0]; off += 4
        extended_cost = struct.unpack_from('<i', payload, off)[0]; off += 4
        items.append({
            "slot": slot, "entry": entry, "display_id": display_id,
            "quantity": quantity, "price": price, "durability": durability,
            "stack_count": stack_count, "extended_cost": extended_cost,
        })

    reason = None
    if item_count == 0 and off < len(payload):
        reason = struct.unpack_from('<b', payload, off)[0]; off += 1

    return {"vendor_guid": vendor_guid, "items": items, "reason": reason}


def parse_trainer_list(payload: bytes) -> dict:
    """SMSG_TRAINER_LIST (0x1B1): TrainerList::Write (NPCPackets.cpp). Raw
    uint64 TrainerGUID, int32 TrainerType, int32 SpellCount, then per spell:
    int32 SpellID, uint8 Usable ("state" — client-usable check result),
    int32 MoneyCost, int32 PointCost[2] (talent-point-style costs, rare on
    normal trainers), uint8 ReqLevel, int32 ReqSkillLine, int32
    ReqSkillRank, int32 ReqAbility[3] (prerequisite spell ids, 0 = none);
    finally cstring Greeting."""
    off = 0
    trainer_guid = pk.u64(payload, off); off += 8
    trainer_type = struct.unpack_from('<i', payload, off)[0]; off += 4
    spell_count = struct.unpack_from('<i', payload, off)[0]; off += 4

    spells = []
    for _ in range(spell_count):
        spell_id = struct.unpack_from('<i', payload, off)[0]; off += 4
        state = payload[off]; off += 1
        cost = struct.unpack_from('<i', payload, off)[0]; off += 4
        point_cost = list(struct.unpack_from('<2i', payload, off)); off += 8
        req_level = payload[off]; off += 1
        req_skill_line = struct.unpack_from('<i', payload, off)[0]; off += 4
        req_skill_rank = struct.unpack_from('<i', payload, off)[0]; off += 4
        req_abilities = list(struct.unpack_from('<3i', payload, off)); off += 12
        spells.append({
            "spell_id": spell_id, "state": state, "cost": cost,
            "point_cost": point_cost, "required_level": req_level,
            "required_skill_line": req_skill_line,
            "required_skill_rank": req_skill_rank,
            "required_abilities": req_abilities,
        })

    greeting, off = pk.cstring(payload, off)

    return {
        "trainer_guid": trainer_guid, "trainer_type": trainer_type,
        "spells": spells, "greeting": greeting,
    }


def parse_npc_text_update(payload: bytes) -> dict:
    """SMSG_NPC_TEXT_UPDATE (0x180): QueryNPCTextResponse::Write
    (QueryPackets.cpp). uint32 (TextID | 0x80000000 if not found); if found,
    a fixed (not length-prefixed) 8 entries (MAX_GOSSIP_TEXT_OPTIONS) of:
    float Probability, cstring Text (text0), cstring Text1, int32
    LanguageID, then a fixed 3 entries (MAX_GOSSIP_TEXT_EMOTES) of: uint32
    EmoteDelay, uint32 EmoteID (delay before id — opposite of the struct's
    field-declaration order)."""
    raw = pk.u32(payload, 0); off = 4
    found = not (raw & 0x80000000)
    text_id = raw & 0x7FFFFFFF
    info = {"text_id": text_id, "found": found}
    if not found:
        return info

    options = []
    for _ in range(MAX_NPC_TEXT_OPTIONS):
        probability = pk.f32(payload, off); off += 4
        text0, off = pk.cstring(payload, off)
        text1, off = pk.cstring(payload, off)
        language = struct.unpack_from('<i', payload, off)[0]; off += 4
        emotes = []
        for _ in range(MAX_NPC_TEXT_EMOTES):
            delay = pk.u32(payload, off); off += 4
            emote_id = pk.u32(payload, off); off += 4
            emotes.append({"delay": delay, "id": emote_id})
        options.append({
            "probability": probability, "text0": text0, "text1": text1,
            "language": language, "emotes": emotes,
        })
    info["options"] = options
    return info


# ── BuyResult / SellResult / trainer FailReason enums ─────────────────────
# src/server/game/Entities/Item/ItemDefines.h (BuyResult, SellResult),
# src/server/game/Entities/Creature/Trainer.h (Trainer::FailReason)
BUY_RESULT_NAMES = {
    0: 'cant_find_item', 1: 'item_already_sold', 2: 'not_enough_money',
    4: 'seller_dont_like_you', 5: 'distance_too_far', 7: 'item_sold_out',
    8: 'cant_carry_more', 11: 'rank_require', 12: 'reputation_require',
}
SELL_RESULT_NAMES = {
    1: 'cant_find_item', 2: 'cant_sell_item', 3: 'cant_find_vendor',
    4: 'you_dont_own_that_item', 5: 'unk', 6: 'only_empty_bag',
    7: 'cant_sell_to_this_merchant',
}
TRAINER_FAIL_REASON_NAMES = {
    0: 'unavailable', 1: 'not_enough_money', 2: 'not_enough_skill',
}


def parse_buy_item(payload: bytes) -> dict:
    """SMSG_BUY_ITEM (0x1A4): sent by Player::BuyItemFromVendorSlot
    (Player.cpp) directly to the buyer (SendDirectMessage — a personal ack,
    not a nearby broadcast) on a successful purchase: ObjectGuid VendorGUID,
    uint32 vendor slot (1-based, matches SMSG_LIST_INVENTORY's MuID/
    build_buy_item's `slot`), int32 new stock count (-1/0xFFFFFFFF if the
    vendor slot has unlimited stock), uint32 stacks bought."""
    off = 0
    vendor_guid = pk.u64(payload, off); off += 8
    slot = pk.u32(payload, off); off += 4
    new_count = struct.unpack_from('<i', payload, off)[0]; off += 4
    stacks = pk.u32(payload, off); off += 4
    return {"vendor_guid": vendor_guid, "slot": slot, "new_count": new_count, "stacks": stacks}


def parse_buy_failed(payload: bytes) -> dict:
    """SMSG_BUY_FAILED (0x1A5): Player::SendBuyError (Player.cpp) —
    ObjectGuid vendor guid (Empty if creature was null), uint32 item entry,
    optional uint32 param (only present if param > 0 — not flag-encoded,
    only inferable from packet length: absent leaves the packet 13 B,
    present makes it 17 B), uint8 BuyResult reason."""
    off = 0
    vendor_guid = pk.u64(payload, off); off += 8
    item_entry = pk.u32(payload, off); off += 4
    param = None
    if len(payload) - off > 1:
        param = pk.u32(payload, off); off += 4
    reason = payload[off]
    return {"vendor_guid": vendor_guid, "item_entry": item_entry, "param": param,
            "reason": reason, "reason_name": BUY_RESULT_NAMES.get(reason, f"reason_{reason}")}


def parse_sell_item(payload: bytes) -> dict:
    """SMSG_SELL_ITEM (0x1A1): Player::SendSellError (Player.cpp) — this
    opcode is ONLY ever sent on failure (HandleSellItemOpcode never sends
    anything on success; a successful sell is silent). ObjectGuid vendor
    guid (Empty if creature was null), ObjectGuid item guid, optional
    uint32 param (only present if param > 0 — same length-inferred
    presence as SMSG_BUY_FAILED), uint8 SellResult reason."""
    off = 0
    vendor_guid = pk.u64(payload, off); off += 8
    item_guid = pk.u64(payload, off); off += 8
    param = None
    if len(payload) - off > 1:
        param = pk.u32(payload, off); off += 4
    reason = payload[off]
    return {"vendor_guid": vendor_guid, "item_guid": item_guid, "param": param,
            "reason": reason, "reason_name": SELL_RESULT_NAMES.get(reason, f"reason_{reason}")}


def parse_trainer_buy_succeeded(payload: bytes) -> dict:
    """SMSG_TRAINER_BUY_SUCCEEDED (0x1B3): TrainerBuySucceeded::Write
    (NPCPackets.cpp) — raw uint64 TrainerGUID, int32 SpellID. No reason
    field; presence of this opcode at all is the success signal."""
    trainer_guid = pk.u64(payload, 0)
    spell_id = struct.unpack_from('<i', payload, 8)[0]
    return {"trainer_guid": trainer_guid, "spell_id": spell_id}


def parse_trainer_buy_failed(payload: bytes) -> dict:
    """SMSG_TRAINER_BUY_FAILED (0x1B4): TrainerBuyFailed::Write
    (NPCPackets.cpp) — raw uint64 TrainerGUID, int32 SpellID, int32
    TrainerFailedReason (Trainer::FailReason)."""
    trainer_guid = pk.u64(payload, 0)
    spell_id = struct.unpack_from('<i', payload, 8)[0]
    reason = struct.unpack_from('<i', payload, 12)[0]
    return {"trainer_guid": trainer_guid, "spell_id": spell_id, "reason": reason,
            "reason_name": TRAINER_FAIL_REASON_NAMES.get(reason, f"reason_{reason}")}


class NpcTextCache:
    """Per-text-id gossip body text (SMSG_NPC_TEXT_UPDATE), cached the same
    shape as agent.names.NameCache: in-flight dedupe + a send budget, no
    on-disk persistence (unlike creature/gameobject templates, gossip text
    isn't currently reused enough across sessions to bother — revisit if
    that changes)."""

    def __init__(self, budget_per_second: int = 10, clock=time.monotonic):
        self.texts: dict[int, dict] = {}
        self._in_flight: set[int] = set()
        self._pending: list = []
        self._sent_times: list = []
        self._budget_per_second = budget_per_second
        self._clock = clock

    def want(self, text_id: int, guid: int):
        if text_id in self.texts or text_id in self._in_flight:
            return
        self._in_flight.add(text_id)
        self._pending.append((text_id, guid))

    def drain(self, max_items: int | None = None) -> list:
        now = self._clock()
        self._sent_times = [t for t in self._sent_times if now - t < 1.0]
        available = self._budget_per_second - len(self._sent_times)
        if max_items is not None:
            available = min(available, max_items)
        out = []
        while available > 0 and self._pending:
            item = self._pending.pop(0)
            out.append(item)
            self._sent_times.append(now)
            available -= 1
        return out

    def on_response(self, data: dict):
        self._in_flight.discard(data["text_id"])
        self.texts[data["text_id"]] = data if data["found"] else None
