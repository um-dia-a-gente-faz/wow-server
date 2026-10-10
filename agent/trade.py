#!/usr/bin/env python3
"""Player-to-player trade (UM-59): opcode builders + response parsers (pure,
no I/O — same split as agent/npc.py), for agent/session.py's dispatch and
agent/perception/trade_state.py's `world.trade` state.

Every wire layout below is copied from TrinityCore branch `3.3.5`:
  src/server/game/Server/Protocol/Opcodes.h (opcode values)
  src/server/game/Handlers/TradeHandler.cpp (every CMSG_* handler,
    WorldSession::SendTradeStatus, WorldSession::SendUpdateTrade)
  src/server/game/Server/Packets/TradePackets.h (CancelTrade::Read — empty)
  src/server/game/Entities/Player/TradeData.h/.cpp (TradeSlots enum,
    TradeData::SetItem/SetMoney/SetAccepted — which status goes to which side)
  src/server/game/Entities/Player/Player.cpp (Player::TradeCancel)
  src/server/game/Entities/Object/ObjectDefines.h (TRADE_DISTANCE)
  src/server/shared/SharedDefines.h (enum TradeStatus)
  src/server/game/Entities/Item/ItemTemplate.h (ITEM_FIELD_FLAG_SOULBOUND,
    MAX_ITEM_PROTO_SOCKETS)

All GUIDs in this file are the *raw* 8-byte form (struct.pack('<Q', ...)) —
`ByteBuffer::operator<</operator>>(ObjectGuid&)` (ObjectGuid.cpp) is a plain
uint64 read/write, not the variable-length PackedGuid movement/update-object
use (that's a distinct type in this branch, only ever used explicitly).

**Non-obvious protocol trap, found reading (not yet live-verified) the
handler source: my own offer is never echoed back to me.** TradeData::
SetItem/SetMoney only ever call Update(forTrader=true) — which sends
SMSG_TRADE_STATUS_EXTENDED to *the trade partner*, informing them of MY new
offer. Nothing equivalent is sent back to the player who just changed their
own offer (a real client already knows what it put there — it updated its
own window optimistically). So `world.trade["my_items"]`/`"my_gold"` has to
be tracked client-side the moment we send CMSG_SET_TRADE_ITEM/
CMSG_SET_TRADE_GOLD, the same way a real client's UI would; only
`their_items`/`their_gold` ever arrives from the server (SMSG_
TRADE_STATUS_EXTENDED with trader_data=1). What DOES reliably come back to
the sender is SMSG_TRADE_STATUS: TRADE_STATUS_BACK_TO_TRADE (any offer
change, either side, un-accepts both — TradeData::SetAccepted(false) is
unconditional) or a failure status (TRADE_STATUS_TRADE_CANCELED/
NOT_ON_TAPLIST) if the change was rejected — agent/actions/trade.py's offer
actions wait for one of those instead.
"""

import struct

from . import packets as pk

# Opcodes (Opcodes.h)
from .opcodes import (  # noqa: F401  (re-exported: callers use trade.CMSG_*/SMSG_*)
    CMSG_INITIATE_TRADE,
    CMSG_BEGIN_TRADE,
    CMSG_BUSY_TRADE,
    CMSG_IGNORE_TRADE,
    CMSG_ACCEPT_TRADE,
    CMSG_UNACCEPT_TRADE,
    CMSG_CANCEL_TRADE,
    CMSG_SET_TRADE_ITEM,
    CMSG_CLEAR_TRADE_ITEM,
    CMSG_SET_TRADE_GOLD,
    SMSG_TRADE_STATUS,
    SMSG_TRADE_STATUS_EXTENDED,
)

# TradeData.h: TradeSlots
TRADE_SLOT_COUNT = 7          # slots 0-6 total, one packet entry each
TRADE_SLOT_TRADED_COUNT = 6   # slots 0-5 actually exchanged
TRADE_SLOT_NONTRADED = 6      # the 7th ("enchant reagent") slot — not offered, out of scope here

MAX_GEM_SOCKETS = 3  # ItemTemplate.h MAX_ITEM_PROTO_SOCKETS

TRADE_DISTANCE_YD = 11.11  # ObjectDefines.h TRADE_DISTANCE

ITEM_FIELD_FLAG_SOULBOUND = 0x00000001  # ItemTemplate.h — Item::IsSoulBound()

# SharedDefines.h enum TradeStatus (13 is unused/reserved in this branch)
TRADE_STATUS_NAMES = {
    0: "busy", 1: "begin_trade", 2: "open_window", 3: "trade_canceled",
    4: "trade_accept", 5: "busy_2", 6: "no_target", 7: "back_to_trade",
    8: "trade_complete", 9: "trade_rejected", 10: "target_to_far",
    11: "wrong_faction", 12: "close_window", 14: "ignore_you",
    15: "you_stunned", 16: "target_stunned", 17: "you_dead", 18: "target_dead",
    19: "you_logout", 20: "target_logout", 21: "trial_account",
    22: "wrong_realm", 23: "not_on_taplist",
}
TRADE_STATUS_BUSY = 0
TRADE_STATUS_BEGIN_TRADE = 1
TRADE_STATUS_OPEN_WINDOW = 2
TRADE_STATUS_TRADE_CANCELED = 3
TRADE_STATUS_TRADE_ACCEPT = 4
TRADE_STATUS_BACK_TO_TRADE = 7
TRADE_STATUS_TRADE_COMPLETE = 8
TRADE_STATUS_CLOSE_WINDOW = 12
TRADE_STATUS_WRONG_REALM = 22
TRADE_STATUS_NOT_ON_TAPLIST = 23

# ItemDefines.h enum InventoryResult — only the handful reachable from a
# trade failure (HandleAcceptTradeOpcode/TradeData::SetMoney); anything else
# falls back to result_<n>.
EQUIP_RESULT_NAMES = {
    0: "ok", 4: "bag_full", 29: "not_enough_money", 77: "too_much_gold",
    79: "trade_bound_item",
}


# ── Request builders ──────────────────────────────────────────────────────

def build_initiate_trade(target_guid: int) -> bytes:
    """CMSG_INITIATE_TRADE (0x116): HandleInitiateTradeOpcode — raw uint64
    guid of the player to trade with."""
    return struct.pack('<Q', target_guid)


def build_begin_trade() -> bytes:
    """CMSG_BEGIN_TRADE (0x117): empty payload — sent by the player who
    received an incoming trade request, to accept opening the window."""
    return b''


def build_busy_trade() -> bytes:
    """CMSG_BUSY_TRADE (0x118): empty payload."""
    return b''


def build_ignore_trade() -> bytes:
    """CMSG_IGNORE_TRADE (0x119): empty payload."""
    return b''


def build_accept_trade() -> bytes:
    """CMSG_ACCEPT_TRADE (0x11A): empty payload."""
    return b''


def build_unaccept_trade() -> bytes:
    """CMSG_UNACCEPT_TRADE (0x11B): empty payload."""
    return b''


def build_cancel_trade() -> bytes:
    """CMSG_CANCEL_TRADE (0x11C): empty payload (WorldPackets::Trade::
    CancelTrade::Read is a no-op)."""
    return b''


def build_set_trade_item(trade_slot: int, bag: int, slot: int) -> bytes:
    """CMSG_SET_TRADE_ITEM (0x11D): HandleSetTradeItemOpcode — uint8
    tradeSlot (0..5 tradeable, per TRADE_SLOT_TRADED_COUNT), uint8 bag,
    uint8 slot (the item's own bag/slot position, same addressing as
    CMSG_USE_ITEM/CMSG_SELL_ITEM)."""
    return struct.pack('<BBB', trade_slot, bag, slot)


def build_clear_trade_item(trade_slot: int) -> bytes:
    """CMSG_CLEAR_TRADE_ITEM (0x11E): HandleClearTradeItemOpcode — uint8
    tradeSlot."""
    return struct.pack('<B', trade_slot)


def build_set_trade_gold(copper: int) -> bytes:
    """CMSG_SET_TRADE_GOLD (0x11F): HandleSetTradeGoldOpcode — uint32
    copper amount."""
    return struct.pack('<I', copper)


# ── Response parsers ──────────────────────────────────────────────────────

def parse_trade_status(payload: bytes) -> dict:
    """SMSG_TRADE_STATUS (0x120): WorldSession::SendTradeStatus. uint32
    status always; then a status-specific tail, per the switch in
    SendTradeStatus — everything else has no tail at all."""
    status = pk.u32(payload, 0)
    off = 4
    info = {"status": status, "status_name": TRADE_STATUS_NAMES.get(status, f"status_{status}")}
    if status == TRADE_STATUS_BEGIN_TRADE:
        info["trader_guid"] = pk.u64(payload, off)
    elif status == TRADE_STATUS_OPEN_WINDOW:
        pass  # trailing uint32, always 0 ("trade ID") — nothing worth decoding
    elif status == TRADE_STATUS_CLOSE_WINDOW:
        result = pk.u32(payload, off); off += 4
        is_target_result = bool(payload[off]); off += 1
        item_limit_category_id = pk.u32(payload, off)
        info["result"] = result
        info["result_name"] = EQUIP_RESULT_NAMES.get(result, f"result_{result}")
        info["is_target_result"] = is_target_result
        info["item_limit_category_id"] = item_limit_category_id
    elif status in (TRADE_STATUS_WRONG_REALM, TRADE_STATUS_NOT_ON_TAPLIST):
        info["slot"] = payload[off]
    return info


def _parse_trade_slot(payload: bytes, off: int) -> tuple[dict | None, int]:
    """One TRADE_SLOT_COUNT entry from SMSG_TRADE_STATUS_EXTENDED
    (WorldSession::SendUpdateTrade): uint8 slot index, then either a real
    item's 17 fields or 18 zero uint32s for an empty slot (see this
    function's field count below, which matches either shape byte-for-byte
    since a GUID field takes 2 words) — an entry of all zeros (entry == 0)
    means the slot is empty; returns (None, new_off) for that case."""
    slot = payload[off]; off += 1
    entry = pk.u32(payload, off); off += 4
    display_id = pk.u32(payload, off); off += 4
    stack_count = pk.u32(payload, off); off += 4
    wrapped = pk.u32(payload, off); off += 4
    gift_creator = pk.u64(payload, off); off += 8
    enchant_id = pk.u32(payload, off); off += 4
    socket_enchants = list(struct.unpack_from(f'<{MAX_GEM_SOCKETS}I', payload, off))
    off += 4 * MAX_GEM_SOCKETS
    creator = pk.u64(payload, off); off += 8
    charges = pk.u32(payload, off); off += 4
    suffix_factor = pk.u32(payload, off); off += 4
    random_property_id = pk.u32(payload, off); off += 4
    lock_id = pk.u32(payload, off); off += 4
    max_durability = pk.u32(payload, off); off += 4
    durability = pk.u32(payload, off); off += 4
    if entry == 0:
        return None, off
    item = {
        "slot": slot, "entry": entry, "display_id": display_id, "count": stack_count,
        "wrapped": bool(wrapped), "gift_creator_guid": gift_creator or None,
        "enchant_id": enchant_id, "socket_enchants": socket_enchants,
        "creator_guid": creator or None, "charges": charges,
        "suffix_factor": suffix_factor, "random_property_id": random_property_id,
        "lock_id": lock_id, "max_durability": max_durability, "durability": durability,
    }
    return item, off


def parse_trade_status_extended(payload: bytes) -> dict:
    """SMSG_TRADE_STATUS_EXTENDED (0x121): WorldSession::SendUpdateTrade.
    uint8 trader_data (1 = this describes the *other* side's offer, 0 =
    my own — see this module's docstring: 0 only happens for the
    enchant-reagent slot 6 case, not modeled here), uint32 trade_id
    (always 0), uint32 x2 slot counts (both always TRADE_SLOT_COUNT),
    uint32 money, uint32 spell (enchant spell cast on slot 6, 0 = none),
    then TRADE_SLOT_COUNT fixed slot entries (see _parse_trade_slot)."""
    off = 0
    trader_data = bool(payload[off]); off += 1
    trade_id = pk.u32(payload, off); off += 4
    slot_count_a = pk.u32(payload, off); off += 4
    slot_count_b = pk.u32(payload, off); off += 4
    money = pk.u32(payload, off); off += 4
    spell = pk.u32(payload, off); off += 4
    items = {}
    for _ in range(TRADE_SLOT_COUNT):
        item, off = _parse_trade_slot(payload, off)
        if item is not None:
            items[item["slot"]] = item
    return {
        "is_trader_data": trader_data, "trade_id": trade_id,
        "slot_count_a": slot_count_a, "slot_count_b": slot_count_b,
        "money": money, "spell": spell, "items": items,
    }
