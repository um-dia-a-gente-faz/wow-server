#!/usr/bin/env python3
"""Mailbox (UM-60): opcode builders + response parsers (pure, no I/O — same
split as agent/npc.py and agent/trade.py), for agent/session.py's dispatch
and agent/perception.py's `world.mailbox` state.

Every wire layout below is copied from TrinityCore branch `3.3.5`:
  src/server/game/Server/Protocol/Opcodes.h (opcode values)
  src/server/game/Handlers/MailHandler.cpp (every CMSG_* handler,
    WorldSession::CanOpenMailBox — the mailbox-guid/range check every mail
    opcode shares)
  src/server/game/Server/Packets/MailPackets.h/.cpp (the structured
    WorldPackets::Mail::* classes this branch's mail system actually uses —
    despite the "3.3.5" branch name, this handler was retrofitted onto the
    modern typed-packet framework; verified as the byte-for-byte format this
    branch's build actually sends/expects, same as CancelTrade in
    agent/trade.py's TradeHandler.cpp)
  src/server/game/Server/Packets/PacketUtilities.h (`String<N, ...>` reads
    via the same `ReadCString` as a plain `std::string` — still a plain
    null-terminated cstring on the wire, not length-prefixed)
  src/server/game/Mails/Mail.h (`MAX_MAIL_ITEMS`, `MailMessageType`,
    `MailCheckMask`)
  src/server/shared/SharedDefines.h (`enum MailResponseType`,
    `enum MailResponseResult`, `GAMEOBJECT_TYPE_MAILBOX`)
  src/server/game/Entities/Unit/UnitDefines.h (`UNIT_NPC_FLAG_MAILBOX`)
  src/server/game/Entities/Item/ItemDefines.h (`MAX_INSPECTED_ENCHANTMENT_SLOT`)

All GUIDs in this file are the *raw* 8-byte form (struct.pack('<Q', ...)) —
same as agent/npc.py and agent/trade.py, not the packed-guid movement/
update-object format.

A mailbox is found in perception the same way a vendor/trainer NPC is: a
gameobject whose queried template `type == GAMEOBJECT_TYPE_MAILBOX` (19), or
(rarer, some custom mailbox NPCs) a unit/player with `UNIT_NPC_FLAG_MAILBOX`
(`0x04000000`) set — `agent/perception.py::ObjectInfo.is_mailbox()`.
CMSG_GET_MAIL_LIST/CMSG_SEND_MAIL/etc. all validate range and mailbox-ness
server-side via `CanOpenMailBox` — there's no separate "use" opcode to open
the mailbox window first, unlike an NPC's gossip/vendor window; sending
CMSG_GET_MAIL_LIST *is* "opening the mailbox".
"""

import struct

from . import packets as pk

# Opcodes (Opcodes.h)
from .opcodes import (
    CMSG_SEND_MAIL,
    SMSG_SEND_MAIL_RESULT,
    CMSG_GET_MAIL_LIST,
    SMSG_MAIL_LIST_RESULT,
    CMSG_MAIL_TAKE_MONEY,
    CMSG_MAIL_TAKE_ITEM,
    CMSG_MAIL_MARK_AS_READ,
    CMSG_MAIL_RETURN_TO_SENDER,
    CMSG_MAIL_DELETE,
    SMSG_RECEIVED_MAIL,
)

MAX_MAIL_ITEMS = 12                    # Mail.h
MAX_INSPECTED_ENCHANTMENT_SLOT = 7     # ItemDefines.h
GAMEOBJECT_TYPE_MAILBOX = 19           # SharedDefines.h
UNIT_NPC_FLAG_MAILBOX = 0x04000000     # UnitDefines.h
MAILBOX_INTERACT_RANGE_YD = 5.0        # same GetGameObjectIfCanInteractWith-style range as npc.INTERACT_RANGE_YD
MAIL_POSTAGE_COPPER = 30               # HandleSendMail: cost = 30 * attachments.size() if any, else 30 flat
ITEM_FIELD_FLAG_SOULBOUND = 0x00000001  # ItemTemplate.h — Item::CanBeTraded() rejects a soulbound attachment the same as a trade offer

# Mail.h enum MailMessageType — who sent it
MAIL_NORMAL = 0
MAIL_AUCTION = 2
MAIL_CREATURE = 3
MAIL_GAMEOBJECT = 4
MAIL_CALENDAR = 5

# Mail.h enum MailCheckMask (bit flags on each mail's `Flags`)
MAIL_CHECK_MASK_READ = 0x0001
MAIL_CHECK_MASK_COD_PAYMENT = 0x0008

# SharedDefines.h enum MailResponseType — which CMSG_MAIL_* this result answers
MAIL_RESPONSE_NAMES = {
    0: "send", 1: "money_taken", 2: "item_taken", 3: "returned_to_sender",
    4: "deleted", 5: "made_permanent",
}
MAIL_SEND = 0
MAIL_MONEY_TAKEN = 1
MAIL_ITEM_TAKEN = 2
MAIL_DELETED = 4

# SharedDefines.h enum MailResponseResult
MAIL_RESULT_NAMES = {
    0: "ok", 1: "equip_error", 2: "cannot_send_to_self", 3: "not_enough_money",
    4: "recipient_not_found", 5: "not_your_team", 6: "internal_error",
    14: "disabled_for_trial_acc", 15: "recipient_cap_reached",
    16: "cant_send_wrapped_cod", 17: "mail_and_chat_suspended",
    18: "too_many_attachments", 19: "mail_attachment_invalid",
    21: "item_has_expired",
}
MAIL_OK = 0
MAIL_ERR_EQUIP_ERROR = 1


# ── Request builders ──────────────────────────────────────────────────────

def build_get_mail_list(mailbox_guid: int) -> bytes:
    """CMSG_GET_MAIL_LIST (0x23A): MailGetList::Read — a single raw uint64
    guid. Sending this *is* "opening the mailbox" (see this module's
    docstring) — there's no separate use/hello opcode."""
    return struct.pack('<Q', mailbox_guid)


def build_send_mail(mailbox_guid: int, target_name: str, subject: str, body: str,
                     money: int = 0, cod: int = 0, item_guid: int | None = None,
                     stationery_id: int = 41, package_id: int = 0) -> bytes:
    """CMSG_SEND_MAIL (0x238): SendMail::Read (MailPackets.cpp). Raw uint64
    mailbox guid, cstring target (player name — resolved server-side, so the
    agent doesn't need the recipient's guid or even for them to be online),
    cstring subject, cstring body, int32 stationeryId (41 =
    MAIL_STATIONERY_DEFAULT), int32 packageId (always 0 for a normal player-
    sent letter), uint8 attachmentCount, then per attachment: uint8
    position, uint64 itemGuid (raw) — v1 supports at most one attached item
    (`item_guid`), not the wire's full `MAX_INSPECTED_ENCHANTMENT_SLOT`-style
    up-to-12; int32 money, int32 cod, then two trailing fields the handler
    reads and discards (`read_skip<uint64>()`, `read_skip<uint8>()`) —
    present on the wire but unused server-side, sent as 0."""
    attachments = b'' if item_guid is None else struct.pack('<BQ', 0, item_guid)
    count = 0 if item_guid is None else 1
    return (struct.pack('<Q', mailbox_guid)
            + target_name.encode('utf-8') + b'\x00'
            + subject.encode('utf-8') + b'\x00'
            + body.encode('utf-8') + b'\x00'
            + struct.pack('<ii', stationery_id, package_id)
            + struct.pack('<B', count) + attachments
            + struct.pack('<ii', money, cod)
            + struct.pack('<QB', 0, 0))


def build_mail_take_money(mailbox_guid: int, mail_id: int) -> bytes:
    """CMSG_MAIL_TAKE_MONEY (0x245): MailTakeMoney::Read — raw uint64
    mailbox guid, int32 mailId."""
    return struct.pack('<Qi', mailbox_guid, mail_id)


def build_mail_take_item(mailbox_guid: int, mail_id: int, attach_id: int) -> bytes:
    """CMSG_MAIL_TAKE_ITEM (0x246): MailTakeItem::Read — raw uint64 mailbox
    guid, int32 mailId, int32 attachId (the attachment's own `AttachID` from
    the mail list, NOT a 0-based slot index)."""
    return struct.pack('<Qii', mailbox_guid, mail_id, attach_id)


def build_mail_mark_as_read(mailbox_guid: int, mail_id: int) -> bytes:
    """CMSG_MAIL_MARK_AS_READ (0x247): MailMarkAsRead::Read — raw uint64
    mailbox guid, int32 mailId."""
    return struct.pack('<Qi', mailbox_guid, mail_id)


def build_mail_delete(mailbox_guid: int, mail_id: int, delete_reason: int = 0) -> bytes:
    """CMSG_MAIL_DELETE (0x249): MailDelete::Read — raw uint64 mailbox guid,
    int32 mailId, int32 deleteReason (always 0 from a real client)."""
    return struct.pack('<Qii', mailbox_guid, mail_id, delete_reason)


# ── Response parsers ──────────────────────────────────────────────────────

def parse_send_mail_result(payload: bytes) -> dict:
    """SMSG_SEND_MAIL_RESULT (0x239): MailCommandResult::Write. uint32
    mailId, uint32 command (MailResponseType — which request this answers),
    uint32 errorCode (MailResponseResult); if errorCode == MAIL_ERR_EQUIP_ERROR
    (1): uint32 bagResult; if command == MAIL_ITEM_TAKEN (2) and errorCode in
    (MAIL_OK, MAIL_ERR_ITEM_HAS_EXPIRED): uint32 attachId, uint32
    qtyInInventory."""
    off = 0
    mail_id = pk.u32(payload, off); off += 4
    command = pk.u32(payload, off); off += 4
    error_code = pk.u32(payload, off); off += 4
    info = {
        "mail_id": mail_id, "command": command,
        "command_name": MAIL_RESPONSE_NAMES.get(command, f"command_{command}"),
        "error_code": error_code,
        "error_name": MAIL_RESULT_NAMES.get(error_code, f"error_{error_code}"),
    }
    if error_code == MAIL_ERR_EQUIP_ERROR:
        info["bag_result"] = pk.u32(payload, off); off += 4
    if command == MAIL_ITEM_TAKEN and error_code in (MAIL_OK, 21):
        info["attach_id"] = pk.u32(payload, off); off += 4
        info["qty_in_inventory"] = pk.u32(payload, off); off += 4
    return info


def _parse_mail_attached_item(payload: bytes, off: int) -> tuple[dict, int]:
    position = payload[off]; off += 1
    attach_id = struct.unpack_from('<i', payload, off)[0]; off += 4
    item_entry = struct.unpack_from('<i', payload, off)[0]; off += 4
    enchant_ids = []
    for _ in range(MAX_INSPECTED_ENCHANTMENT_SLOT):
        enchant_id = struct.unpack_from('<i', payload, off)[0]; off += 4
        off += 8  # duration, charges — not surfaced, rarely meaningful on a mailed item
        enchant_ids.append(enchant_id)
    random_property_id = struct.unpack_from('<i', payload, off)[0]; off += 4
    random_property_seed = struct.unpack_from('<i', payload, off)[0]; off += 4
    count = struct.unpack_from('<i', payload, off)[0]; off += 4
    charges = struct.unpack_from('<i', payload, off)[0]; off += 4
    max_durability = pk.u32(payload, off); off += 4
    durability = struct.unpack_from('<i', payload, off)[0]; off += 4
    unlocked = bool(payload[off]); off += 1
    item = {
        "position": position, "attach_id": attach_id, "entry": item_entry,
        "enchant_ids": enchant_ids, "random_property_id": random_property_id,
        "random_property_seed": random_property_seed, "count": count,
        "charges": charges, "max_durability": max_durability,
        "durability": durability, "unlocked": unlocked,
    }
    return item, off


def _parse_mail_list_entry(payload: bytes, off: int) -> tuple[dict, int]:
    entry_size = pk.u16(payload, off); off += 2  # byte size of everything below, not needed to parse it
    mail_id = struct.unpack_from('<i', payload, off)[0]; off += 4
    sender_type = payload[off]; off += 1
    sender_guid = None
    alt_sender_id = None
    if sender_type == MAIL_NORMAL:
        sender_guid = pk.u64(payload, off); off += 8
    else:
        alt_sender_id = struct.unpack_from('<i', payload, off)[0]; off += 4
    cod = pk.u32(payload, off); off += 4
    package_id = struct.unpack_from('<i', payload, off)[0]; off += 4
    stationery_id = struct.unpack_from('<i', payload, off)[0]; off += 4
    money = pk.u32(payload, off); off += 4
    flags = struct.unpack_from('<i', payload, off)[0]; off += 4
    days_left = pk.f32(payload, off); off += 4
    mail_template_id = struct.unpack_from('<i', payload, off)[0]; off += 4
    subject, off = pk.cstring(payload, off)
    body, off = pk.cstring(payload, off)
    attachment_count = payload[off]; off += 1
    attachments = []
    for _ in range(attachment_count):
        item, off = _parse_mail_attached_item(payload, off)
        attachments.append(item)
    entry = {
        "mail_id": mail_id, "sender_type": sender_type, "sender_guid": sender_guid,
        "alt_sender_id": alt_sender_id, "cod": cod, "package_id": package_id,
        "stationery_id": stationery_id, "money": money, "flags": flags,
        "is_read": bool(flags & MAIL_CHECK_MASK_READ),
        "days_left": days_left, "mail_template_id": mail_template_id,
        "subject": subject, "body": body, "attachments": attachments,
    }
    return entry, off


def parse_mail_list_result(payload: bytes) -> dict:
    """SMSG_MAIL_LIST_RESULT (0x23B): MailListResult::Write. int32
    totalNumRecords (may exceed the 50-entry page below — same v1 "no
    pagination" scope as this project's other list windows), uint8
    mails.size(), then that many MailListEntry records (see
    _parse_mail_list_entry — each starts with its own uint16 byte-size
    header, which this parser doesn't need since every field is fixed or
    null-terminated)."""
    off = 0
    total = struct.unpack_from('<i', payload, off)[0]; off += 4
    count = payload[off]; off += 1
    mails = []
    for _ in range(count):
        entry, off = _parse_mail_list_entry(payload, off)
        mails.append(entry)
    return {"total_records": total, "mails": mails}


def parse_received_mail(payload: bytes) -> dict:
    """SMSG_RECEIVED_MAIL (0x285): NotifyReceivedMail::Write — a single
    float `Delay` (seconds until the client should re-check, always 0 from
    a live delivery notification; not otherwise meaningful here)."""
    return {"delay": pk.f32(payload, 0)}
