#!/usr/bin/env python3
"""Quest system (UM-41): quest text query, questgiver flow (status/hello/
accept/complete/turn-in) request builders + response parsers (pure, no I/O —
see agent/session.py for opcodes' dispatch, agent/perception.py for
quest_giver_status on nearby NPCs and quest_log in snapshot()), plus a small
QuestCache mirroring agent.names.NameCache's shape for static per-quest-id
text (SMSG_QUEST_QUERY_RESPONSE), keyed by quest id.

Opcode values follow the same numbering scheme as the other Query opcodes
already in this codebase (agent/session.py: CMSG_GAMEOBJECT_QUERY = 0x05E,
CMSG_CREATURE_QUERY = 0x060 — CMSG_QUEST_QUERY / SMSG_QUEST_QUERY_RESPONSE
slot in right before those, 0x05C/0x05D) and the well-documented 3.3.5a
(build 12340) `Opcodes.h` questgiver block (0x182-0x19D). The exact
questgiver/quest-query *payload* layouts below are this module's best-effort
reconstruction of TrinityCore 3.3.5's `Player::SendQuestQueryResponse` /
`PlayerMenu::Send*` (GossipDef.cpp, QuestDef.cpp) from documented structure —
this repo had no network access to a live TrinityCore checkout or a live
server while UM-41 was implemented, so treat every parser here as **not
diffed against source or live-verified** (unlike e.g. agent/npc.py's
citations) until someone can cross-check it against a live packet capture or
the actual TrinityCore branch `3.3.5` source. Every parser is still wrapped
by the caller (agent/session.py) in the same try/except-and-drop-the-packet
pattern as everything else, so a wrong guess here degrades to "quest
feature misses this update" rather than crashing the session.
"""

import struct
import time

from . import packets as pk

# ── Opcodes ───────────────────────────────────────────────────────────────
CMSG_QUEST_QUERY               = 0x05C
SMSG_QUEST_QUERY_RESPONSE      = 0x05D

CMSG_QUESTGIVER_STATUS_QUERY   = 0x182
SMSG_QUESTGIVER_STATUS         = 0x183
CMSG_QUESTGIVER_HELLO          = 0x184
SMSG_QUESTGIVER_QUEST_LIST     = 0x185
CMSG_QUESTGIVER_QUERY_QUEST    = 0x186
SMSG_QUESTGIVER_QUEST_DETAILS  = 0x188
CMSG_QUESTGIVER_ACCEPT_QUEST   = 0x189
CMSG_QUESTGIVER_COMPLETE_QUEST = 0x18A
SMSG_QUESTGIVER_REQUEST_ITEMS  = 0x18B
CMSG_QUESTGIVER_REQUEST_REWARD = 0x18C
SMSG_QUESTGIVER_OFFER_REWARD   = 0x18D
CMSG_QUESTGIVER_CHOOSE_REWARD  = 0x18E
CMSG_QUESTGIVER_CANCEL         = 0x190
SMSG_QUESTGIVER_QUEST_COMPLETE = 0x191
SMSG_QUESTGIVER_QUEST_FAILED   = 0x192
CMSG_QUESTLOG_REMOVE_QUEST     = 0x194
SMSG_QUESTUPDATE_COMPLETE      = 0x198
SMSG_QUESTUPDATE_ADD_KILL      = 0x199
SMSG_QUESTUPDATE_ADD_ITEM      = 0x19A

MAX_QUEST_REQ = 4          # required creature/GO/item slots per quest (QUEST_OBJECTIVES_COUNT)
MAX_QUEST_REWARDS = 4      # reward item / reward choice item slots (QUEST_REWARD_CHOICES_COUNT)

INTERACT_RANGE_YD = 5.0    # matches agent.npc.INTERACT_RANGE_YD

# QuestGiverStatus (best-effort — SharedDefines.h `enum QuestGiverStatus` in
# real TrinityCore mixes plain states and "reward" bit-flags; only the
# handful of values worth surfacing to the LLM are named, everything else
# falls back to `status_<n>` the same way agent/npc.py's BUY_RESULT_NAMES
# does for unmapped values).
QUEST_GIVER_STATUS_NAMES = {
    0: "none",
    1: "unavailable",
    2: "low_level_available",
    4: "available",
    8: "incomplete",
    9: "reward_rep",
    10: "reward",
}

# QuestStatus (Player.h) — used for quest log slot `state` low byte.
QUEST_STATUS_NAMES = {
    0: "none", 1: "complete", 2: "unavailable", 3: "incomplete", 4: "failed",
}


# ── Request builders ────────────────────────────────────────────────────

def build_quest_query(quest_id: int, guid: int = 0) -> bytes:
    """CMSG_QUEST_QUERY (0x05C): uint32 quest id, raw uint64 guid (the
    questgiver this quest was queried through, if any — 0 when just
    re-querying an already-known quest id, e.g. from the quest log)."""
    return struct.pack('<IQ', quest_id, guid)


def build_questgiver_status_query(guid: int) -> bytes:
    """CMSG_QUESTGIVER_STATUS_QUERY (0x182): a single raw uint64 guid."""
    return struct.pack('<Q', guid)


def build_questgiver_hello(guid: int) -> bytes:
    """CMSG_QUESTGIVER_HELLO (0x184): a single raw uint64 guid — same
    "Hello" struct shape as agent.npc.build_gossip_hello."""
    return struct.pack('<Q', guid)


def build_questgiver_query_quest(guid: int, quest_id: int) -> bytes:
    """CMSG_QUESTGIVER_QUERY_QUEST (0x186): raw uint64 guid, uint32 quest id."""
    return struct.pack('<QI', guid, quest_id)


def build_questgiver_accept_quest(guid: int, quest_id: int) -> bytes:
    """CMSG_QUESTGIVER_ACCEPT_QUEST (0x189): raw uint64 guid, uint32 quest
    id, uint32 unk (start-cheat flag on a real client's debug build; always
    0 from a normal client)."""
    return struct.pack('<QII', guid, quest_id, 0)


def build_questgiver_complete_quest(guid: int, quest_id: int) -> bytes:
    """CMSG_QUESTGIVER_COMPLETE_QUEST (0x18A): raw uint64 guid, uint32 quest id."""
    return struct.pack('<QI', guid, quest_id)


def build_questgiver_request_reward(guid: int, quest_id: int) -> bytes:
    """CMSG_QUESTGIVER_REQUEST_REWARD (0x18C): raw uint64 guid, uint32 quest id."""
    return struct.pack('<QI', guid, quest_id)


def build_questgiver_choose_reward(guid: int, quest_id: int, reward_choice: int = 0) -> bytes:
    """CMSG_QUESTGIVER_CHOOSE_REWARD (0x18E): raw uint64 guid, uint32 quest
    id, uint32 reward item-choice slot (0 for a quest with no item choice,
    or with only one reward item)."""
    return struct.pack('<QII', guid, quest_id, reward_choice)


def build_questlog_remove_quest(slot: int) -> bytes:
    """CMSG_QUESTLOG_REMOVE_QUEST (0x194): a single uint8 quest-log slot
    (0-24) — the client removes by slot position, not quest id."""
    return struct.pack('<B', slot)


# ── Response parsers ────────────────────────────────────────────────────

def _read_req_pairs(payload: bytes, off: int, count: int) -> tuple[list, int]:
    """count (entry, required_count) uint32 pairs — the shape used for both
    required creature/GO credit and required item slots."""
    out = []
    for _ in range(count):
        entry = struct.unpack_from('<i', payload, off)[0]; off += 4
        need = pk.u32(payload, off); off += 4
        out.append({"entry": entry, "count": need})
    return out, off


def parse_quest_query_response(payload: bytes) -> dict:
    """SMSG_QUEST_QUERY_RESPONSE (0x05D). uint32 quest id, int32 quest
    method/type, uint32 quest level, uint32 flags, cstring title, cstring
    details, cstring objectives, cstring end_text (offer-reward text),
    uint32 reward_money, uint32 reward_xp, then MAX_QUEST_REQ
    (entry, required_count) pairs for required creature/GO credit, then
    MAX_QUEST_REQ (entry, count) pairs for required items, then
    MAX_QUEST_REWARDS (entry, count) pairs for reward items, then uint32
    next_quest_in_chain."""
    off = 0
    quest_id = pk.u32(payload, off); off += 4
    method = struct.unpack_from('<i', payload, off)[0]; off += 4
    level = pk.u32(payload, off); off += 4
    flags = pk.u32(payload, off); off += 4
    title, off = pk.cstring(payload, off)
    details, off = pk.cstring(payload, off)
    objectives, off = pk.cstring(payload, off)
    end_text, off = pk.cstring(payload, off)
    reward_money = pk.u32(payload, off); off += 4
    reward_xp = pk.u32(payload, off); off += 4
    required_credit, off = _read_req_pairs(payload, off, MAX_QUEST_REQ)
    required_items, off = _read_req_pairs(payload, off, MAX_QUEST_REQ)
    reward_items, off = _read_req_pairs(payload, off, MAX_QUEST_REWARDS)
    next_quest_in_chain = pk.u32(payload, off); off += 4

    return {
        "quest_id": quest_id, "method": method, "level": level, "flags": flags,
        "title": title, "details": details, "objectives": objectives, "end_text": end_text,
        "reward_money": reward_money, "reward_xp": reward_xp,
        "required_credit": required_credit,  # [{"entry": creature/GO entry (GO entries are negative), "count": required}]
        "required_items": required_items,     # [{"entry": item entry, "count": required}]
        "reward_items": reward_items,         # [{"entry": item entry, "count": reward}]
        "next_quest_in_chain": next_quest_in_chain,
    }


def parse_questgiver_status(payload: bytes) -> dict:
    """SMSG_QUESTGIVER_STATUS (0x183): raw uint64 guid, uint8 status
    (QuestGiverStatus) — matches TrinityCore 3.3.5's
    QuestGiverStatus::Write() in QuestPackets.cpp (guid + single status
    byte, 9 bytes total)."""
    guid = pk.u64(payload, 0)
    status = pk.u8(payload, 8)
    return {"guid": guid, "status": status,
            "status_name": QUEST_GIVER_STATUS_NAMES.get(status, f"status_{status}")}


def parse_questgiver_quest_list(payload: bytes) -> dict:
    """SMSG_QUESTGIVER_QUEST_LIST (0x185): raw uint64 npc guid, cstring
    greeting title, uint32 emote_delay, uint32 emote_id, uint8 quest_count,
    then per quest: int32 quest_id, uint32 quest_icon (QuestGiverStatus-ish
    icon selector), int32 quest_level, cstring title."""
    off = 0
    npc_guid = pk.u64(payload, off); off += 8
    title, off = pk.cstring(payload, off)
    emote_delay = pk.u32(payload, off); off += 4
    emote_id = pk.u32(payload, off); off += 4
    count = payload[off]; off += 1
    quests = []
    for _ in range(count):
        quest_id = struct.unpack_from('<i', payload, off)[0]; off += 4
        icon = pk.u32(payload, off); off += 4
        level = struct.unpack_from('<i', payload, off)[0]; off += 4
        quest_title, off = pk.cstring(payload, off)
        quests.append({"quest_id": quest_id, "icon": icon, "level": level, "title": quest_title})
    return {"npc_guid": npc_guid, "title": title, "emote_delay": emote_delay,
            "emote_id": emote_id, "quests": quests}


def parse_questgiver_quest_details(payload: bytes) -> dict:
    """SMSG_QUESTGIVER_QUEST_DETAILS (0x188): raw uint64 npc guid, uint32
    quest_id, cstring title, cstring details, cstring objectives, uint8
    auto_finish, uint32 suggested_players, uint32 reward_money, uint32
    reward_xp, then MAX_QUEST_REQ (entry, count) required creature/GO
    credit pairs, then MAX_QUEST_REQ (entry, count) required item pairs."""
    off = 0
    npc_guid = pk.u64(payload, off); off += 8
    quest_id = pk.u32(payload, off); off += 4
    title, off = pk.cstring(payload, off)
    details, off = pk.cstring(payload, off)
    objectives, off = pk.cstring(payload, off)
    auto_finish = bool(payload[off]); off += 1
    suggested_players = pk.u32(payload, off); off += 4
    reward_money = pk.u32(payload, off); off += 4
    reward_xp = pk.u32(payload, off); off += 4
    required_credit, off = _read_req_pairs(payload, off, MAX_QUEST_REQ)
    required_items, off = _read_req_pairs(payload, off, MAX_QUEST_REQ)
    return {
        "npc_guid": npc_guid, "quest_id": quest_id, "title": title, "details": details,
        "objectives": objectives, "auto_finish": auto_finish,
        "suggested_players": suggested_players, "reward_money": reward_money,
        "reward_xp": reward_xp, "required_credit": required_credit,
        "required_items": required_items,
    }


def parse_questgiver_request_items(payload: bytes) -> dict:
    """SMSG_QUESTGIVER_REQUEST_ITEMS (0x18B): raw uint64 npc guid, uint32
    quest_id, cstring title, cstring request_items_text, uint32
    required_money, uint8 auto_finish, then MAX_QUEST_REQ (entry, count)
    required item pairs."""
    off = 0
    npc_guid = pk.u64(payload, off); off += 8
    quest_id = pk.u32(payload, off); off += 4
    title, off = pk.cstring(payload, off)
    request_items_text, off = pk.cstring(payload, off)
    required_money = pk.u32(payload, off); off += 4
    auto_finish = bool(payload[off]); off += 1
    required_items, off = _read_req_pairs(payload, off, MAX_QUEST_REQ)
    return {
        "npc_guid": npc_guid, "quest_id": quest_id, "title": title,
        "request_items_text": request_items_text, "required_money": required_money,
        "auto_finish": auto_finish, "required_items": required_items,
    }


def parse_questgiver_offer_reward(payload: bytes) -> dict:
    """SMSG_QUESTGIVER_OFFER_REWARD (0x18D): raw uint64 npc guid, uint32
    quest_id, cstring title, cstring offer_reward_text, uint32 reward_money,
    uint32 reward_xp, then MAX_QUEST_REWARDS (entry, count) reward item
    pairs, then MAX_QUEST_REWARDS (entry, count) reward *choice* item
    pairs (the ones choose_reward's reward_choice slot picks between)."""
    off = 0
    npc_guid = pk.u64(payload, off); off += 8
    quest_id = pk.u32(payload, off); off += 4
    title, off = pk.cstring(payload, off)
    offer_reward_text, off = pk.cstring(payload, off)
    reward_money = pk.u32(payload, off); off += 4
    reward_xp = pk.u32(payload, off); off += 4
    reward_items, off = _read_req_pairs(payload, off, MAX_QUEST_REWARDS)
    reward_choice_items, off = _read_req_pairs(payload, off, MAX_QUEST_REWARDS)
    return {
        "npc_guid": npc_guid, "quest_id": quest_id, "title": title,
        "offer_reward_text": offer_reward_text, "reward_money": reward_money,
        "reward_xp": reward_xp, "reward_items": reward_items,
        "reward_choice_items": reward_choice_items,
    }


def parse_questgiver_quest_complete(payload: bytes) -> dict:
    """SMSG_QUESTGIVER_QUEST_COMPLETE (0x191): uint32 quest_id, uint32
    xp_reward, uint32 money_reward — sent right after CMSG_QUESTGIVER_
    CHOOSE_REWARD lands successfully; presence of this opcode at all is the
    turn-in success signal (mirrors SMSG_TRAINER_BUY_SUCCEEDED's
    "no separate reason field" shape in agent/npc.py)."""
    quest_id = pk.u32(payload, 0)
    xp_reward = pk.u32(payload, 4)
    money_reward = pk.u32(payload, 8)
    return {"quest_id": quest_id, "xp_reward": xp_reward, "money_reward": money_reward}


def parse_questgiver_quest_failed(payload: bytes) -> dict:
    """SMSG_QUESTGIVER_QUEST_FAILED (0x192): uint32 quest_id, uint32 reason."""
    quest_id = pk.u32(payload, 0)
    reason = pk.u32(payload, 4) if len(payload) >= 8 else 0
    return {"quest_id": quest_id, "reason": reason}


def parse_questupdate_add_kill(payload: bytes) -> dict:
    """SMSG_QUESTUPDATE_ADD_KILL (0x199): uint32 quest_id, int32
    creature_entry (the kill-credit target), uint32 count (new progress),
    uint32 required (objective's target count), raw uint64 victim guid."""
    quest_id = pk.u32(payload, 0)
    entry = struct.unpack_from('<i', payload, 4)[0]
    count = pk.u32(payload, 8)
    required = pk.u32(payload, 12)
    victim_guid = pk.u64(payload, 16) if len(payload) >= 24 else 0
    return {"quest_id": quest_id, "entry": entry, "count": count,
            "required": required, "victim_guid": victim_guid}


def parse_questupdate_add_item(payload: bytes) -> dict:
    """SMSG_QUESTUPDATE_ADD_ITEM (0x19A): uint32 item_entry, uint32 count
    (new progress toward that item's objective)."""
    item_entry = pk.u32(payload, 0)
    count = pk.u32(payload, 4)
    return {"item_entry": item_entry, "count": count}


def parse_questupdate_complete(payload: bytes) -> dict:
    """SMSG_QUESTUPDATE_COMPLETE (0x198): uint32 quest_id — every objective
    is now satisfied, ready to turn in (still requires
    CMSG_QUESTGIVER_COMPLETE_QUEST at the questgiver to actually hand it
    in)."""
    return {"quest_id": pk.u32(payload, 0)}


class QuestCache:
    """Per-quest-id static text/reward data (SMSG_QUEST_QUERY_RESPONSE),
    cached like agent.names.NameCache: in-flight dedupe + a send budget, no
    on-disk persistence (unlike creature/gameobject templates — quest text
    is comparatively rare to re-query and this repo has no live server to
    validate a cache format against yet)."""

    def __init__(self, budget_per_second: int = 10, clock=time.monotonic):
        self.quests: dict[int, dict] = {}
        self._in_flight: set[int] = set()
        self._pending: list = []
        self._sent_times: list = []
        self._budget_per_second = budget_per_second
        self._clock = clock

    def want(self, quest_id: int, guid: int = 0):
        if quest_id in self.quests or quest_id in self._in_flight:
            return
        self._in_flight.add(quest_id)
        self._pending.append((quest_id, guid))

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
        self._in_flight.discard(data["quest_id"])
        self.quests[data["quest_id"]] = data
