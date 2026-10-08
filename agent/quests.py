#!/usr/bin/env python3
"""Quest system (UM-41): quest text query, questgiver flow (status/hello/
accept/complete/turn-in) request builders + response parsers (pure, no I/O —
see agent/session.py for opcodes' dispatch, agent/perception/windows.py for
quest_giver_status on nearby NPCs and quest_log in snapshot()), plus a small
QuestCache mirroring agent.names.NameCache's shape for static per-quest-id
text (SMSG_QUEST_QUERY_RESPONSE), keyed by quest id.

Payload layouts are verified against TrinityCore branch `3.3.5` (commit
48128f325ac5f1b597ab86b6b410d9eed1024bb1): `Server/Packets/QuestPackets.cpp`,
`Entities/Creature/GossipDef.cpp`, `Entities/Player/Player.cpp`,
`Handlers/QuestHandler.cpp` — each parser cites its writer. UM-41 first
wrote them from memory; UM-91 found SMSG_QUEST_QUERY_RESPONSE (and the
window packets) did not match the server, which is why the quest log showed
the zone id as the title. SMSG_QUEST_QUERY_RESPONSE and SMSG_QUESTUPDATE_*
are checked against live captures in agent/tests/fixtures/quests/.
"""

import struct
import time

from . import packets as pk

# ── Opcodes ───────────────────────────────────────────────────────────────
from .opcodes import (
    CMSG_QUEST_QUERY,
    SMSG_QUEST_QUERY_RESPONSE,
    CMSG_QUESTGIVER_STATUS_QUERY,
    SMSG_QUESTGIVER_STATUS,
    CMSG_QUESTGIVER_HELLO,
    SMSG_QUESTGIVER_QUEST_LIST,
    CMSG_QUESTGIVER_QUERY_QUEST,
    SMSG_QUESTGIVER_QUEST_DETAILS,
    CMSG_QUESTGIVER_ACCEPT_QUEST,
    CMSG_QUESTGIVER_COMPLETE_QUEST,
    SMSG_QUESTGIVER_REQUEST_ITEMS,
    CMSG_QUESTGIVER_REQUEST_REWARD,
    SMSG_QUESTGIVER_OFFER_REWARD,
    CMSG_QUESTGIVER_CHOOSE_REWARD,
    CMSG_QUESTGIVER_CANCEL,
    SMSG_QUESTGIVER_QUEST_COMPLETE,
    SMSG_QUESTGIVER_QUEST_FAILED,
    CMSG_QUESTLOG_REMOVE_QUEST,
    SMSG_QUESTUPDATE_COMPLETE,
    SMSG_QUESTUPDATE_ADD_KILL,
    SMSG_QUESTUPDATE_ADD_ITEM,
)


# Quests/QuestDef.h, SharedDefines.h (PVP_TEAMS_COUNT)
QUEST_OBJECTIVES_COUNT = 4          # creature/GO kill-credit objectives
QUEST_ITEM_OBJECTIVES_COUNT = 6     # required-item objectives
QUEST_REWARD_CHOICES_COUNT = 6
QUEST_REWARD_ITEM_COUNT = 4
QUEST_REWARD_REPUTATIONS_COUNT = 5
PVP_TEAMS_COUNT = 2

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

# PLAYER_QUEST_LOG_x_2 is a QuestSlotStateMask bitmask (Entities/Player/
# Player.h), not the DB's QuestStatus: an accepted, unfinished quest is 0
# (Player::SetQuestSlot zeroes it; SetQuestSlotState sets the bits).
QUEST_STATE_COMPLETE = 0x1
QUEST_STATE_FAIL = 0x2


def quest_slot_state_name(state: int) -> str:
    if state & QUEST_STATE_FAIL:
        return "failed"
    if state & QUEST_STATE_COMPLETE:
        return "complete"
    return "incomplete"


# ── Request builders ────────────────────────────────────────────────────

def build_quest_query(quest_id: int, guid: int = 0) -> bytes:
    """CMSG_QUEST_QUERY (0x05C): uint32 quest id only — QueryQuestInfo::Read
    (QuestPackets.cpp) reads nothing else. `guid` is accepted for the
    caller's (quest_id, guid) queue shape and ignored."""
    return struct.pack('<I', quest_id)


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
#
# Every layout below is the write order of TrinityCore branch `3.3.5` (commit
# 48128f325ac5f1b597ab86b6b410d9eed1024bb1), cited per parser. Parsers that
# have a live capture in agent/tests/fixtures/quests/ are tested against it.

class _Reader:
    """Sequential little-endian reader over one payload; `done()` checks
    the whole payload was consumed (a layout mistake shows up as leftover
    or missing bytes instead of silently shifted fields — UM-91)."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.off = 0

    def u8(self) -> int:
        v = self.payload[self.off]; self.off += 1
        return v

    def u32(self) -> int:
        v = struct.unpack_from('<I', self.payload, self.off)[0]; self.off += 4
        return v

    def i32(self) -> int:
        v = struct.unpack_from('<i', self.payload, self.off)[0]; self.off += 4
        return v

    def f32(self) -> float:
        v = struct.unpack_from('<f', self.payload, self.off)[0]; self.off += 4
        return v

    def u64(self) -> int:
        v = struct.unpack_from('<Q', self.payload, self.off)[0]; self.off += 8
        return v

    def cstring(self) -> str:
        s, self.off = pk.cstring(self.payload, self.off)
        return s

    def done(self):
        if self.off != len(self.payload):
            raise ValueError(f"parsed {self.off} of {len(self.payload)} bytes")


def _npc_or_go(raw: int) -> tuple[int, bool]:
    """A RequiredNpcOrGo entry on the wire: creature entry, or gameobject
    entry | 0x80000000 (QueryQuestInfoResponse::Write,
    Player::SendQuestUpdateAddCreatureOrGo)."""
    if raw & 0x80000000:
        return raw & 0x7FFFFFFF, True
    return raw, False


def _reward_list(r: _Reader) -> list:
    """uint32 count, then count x (uint32 item, uint32 quantity, uint32
    display id) — QuestGiverQuestDetails/QuestGiverOfferRewardMessage::Write.
    Only non-empty slots are sent (Quest::BuildQuestRewards)."""
    out = []
    for _ in range(r.u32()):
        out.append({"entry": r.u32(), "count": r.u32(), "display_id": r.u32()})
    return out


def parse_quest_query_response(payload: bytes) -> dict:
    """SMSG_QUEST_QUERY_RESPONSE (0x05D) — QueryQuestInfoResponse::Write
    (src/server/game/Server/Packets/QuestPackets.cpp), built by
    Quest::BuildQueryData (Quests/QuestDef.cpp). Fixed numeric header, the
    reward/POI block, 5 cstrings, then objectives and 4 objective texts.

    `required_credit` keeps all QUEST_OBJECTIVES_COUNT slots (empty ones
    have count 0) because the quest log's counter i belongs to slot i;
    `required_items` likewise keeps all QUEST_ITEM_OBJECTIVES_COUNT slots.
    """
    r = _Reader(payload)
    info = {
        "quest_id": r.u32(),
        "quest_type": r.u32(),          # QuestType: 0 = auto-complete
        "level": r.i32(),               # -1 = scales with player
        "min_level": r.u32(),
        "sort_id": r.i32(),             # zone id (>0) or QuestSort (<0)
        "info_id": r.u32(),
        "suggested_players": r.u32(),
    }
    info["required_factions"] = [{"faction": r.u32(), "value": r.i32()} for _ in range(PVP_TEAMS_COUNT)]
    info["next_quest_in_chain"] = r.u32()
    info["reward_xp_difficulty"] = r.u32()
    info["reward_money"] = r.i32()      # negative = money required to complete
    info["reward_bonus_money"] = r.u32()
    info["reward_display_spell"] = r.u32()
    info["reward_spell"] = r.i32()
    info["reward_honor"] = r.u32()
    info["reward_kill_honor"] = r.f32()
    info["start_item"] = r.u32()
    info["flags"] = r.u32()
    info["reward_title"] = r.u32()
    info["required_player_kills"] = r.u32()
    info["reward_talents"] = r.u32()
    info["reward_arena_points"] = r.u32()
    info["reward_faction_flags"] = r.u32()
    reward_items = [{"entry": r.u32(), "count": r.u32()} for _ in range(QUEST_REWARD_ITEM_COUNT)]
    reward_choice_items = [{"entry": r.u32(), "count": r.u32()} for _ in range(QUEST_REWARD_CHOICES_COUNT)]
    info["reward_items"] = [i for i in reward_items if i["entry"]]
    info["reward_choice_items"] = [i for i in reward_choice_items if i["entry"]]
    faction_ids = [r.u32() for _ in range(QUEST_REWARD_REPUTATIONS_COUNT)]
    faction_values = [r.i32() for _ in range(QUEST_REWARD_REPUTATIONS_COUNT)]
    faction_overrides = [r.i32() for _ in range(QUEST_REWARD_REPUTATIONS_COUNT)]
    info["reward_reputations"] = [
        {"faction": f, "value": v, "override": o}
        for f, v, o in zip(faction_ids, faction_values, faction_overrides) if f
    ]
    info["poi"] = {"map": r.u32(), "x": r.f32(), "y": r.f32(), "priority": r.u32()}
    info["title"] = r.cstring()             # LogTitle
    info["objectives"] = r.cstring()        # LogDescription: the quest-log objective summary
    info["details"] = r.cstring()           # QuestDescription: the questgiver's story text
    info["area_description"] = r.cstring()  # AreaDescription
    info["completion_text"] = r.cstring()   # QuestCompletionLog: "Return to ..."
    required_credit = []
    for _ in range(QUEST_OBJECTIVES_COUNT):
        entry, is_go = _npc_or_go(r.u32())
        count = r.u32()
        item_drop = r.u32()
        item_drop_count = r.u32()
        required_credit.append({"entry": entry, "gameobject": is_go, "count": count,
                                "item_drop": item_drop, "item_drop_count": item_drop_count})
    info["required_credit"] = required_credit
    info["required_items"] = [{"entry": r.u32(), "count": r.u32()}
                              for _ in range(QUEST_ITEM_OBJECTIVES_COUNT)]
    objective_texts = [r.cstring() for _ in range(QUEST_OBJECTIVES_COUNT)]
    for req, text in zip(required_credit, objective_texts):
        req["text"] = text                  # custom label, e.g. "Burning Crystal destroyed"; "" = use the creature/GO name
    r.done()
    return info


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
    """SMSG_QUESTGIVER_QUEST_LIST (0x185) — PlayerMenu::SendQuestGiverQuestList
    (Entities/Creature/GossipDef.cpp): uint64 npc guid, cstring greeting,
    uint32 emote delay, uint32 emote, uint8 count, then per quest: uint32
    quest id, uint32 icon, int32 level, uint32 flags, uint8 repeatable,
    cstring title."""
    r = _Reader(payload)
    npc_guid = r.u64()
    title = r.cstring()
    emote_delay = r.u32()
    emote_id = r.u32()
    quests = []
    for _ in range(r.u8()):
        quests.append({"quest_id": r.u32(), "icon": r.u32(), "level": r.i32(),
                       "flags": r.u32(), "repeatable": bool(r.u8()), "title": r.cstring()})
    r.done()
    return {"npc_guid": npc_guid, "title": title, "emote_delay": emote_delay,
            "emote_id": emote_id, "quests": quests}


def parse_questgiver_quest_details(payload: bytes) -> dict:
    """SMSG_QUESTGIVER_QUEST_DETAILS (0x188) — QuestGiverQuestDetails::Write
    (QuestPackets.cpp): uint64 npc guid, uint64 inform unit (the player
    sharing the quest, else 0), uint32 quest id, cstring title, cstring
    details, cstring objectives, uint8 auto launched, uint32 flags, uint32
    suggested players, uint8 start cheat, reward choice/reward item lists,
    money/xp/honor/spell/title/talents/arena/faction-flags, 5x3 reputation
    ints, then uint32 emote count + (type, delay) pairs."""
    r = _Reader(payload)
    info = {"npc_guid": r.u64(), "inform_unit": r.u64(), "quest_id": r.u32(),
            "title": r.cstring(), "details": r.cstring(), "objectives": r.cstring(),
            "auto_launched": bool(r.u8()), "flags": r.u32(), "suggested_players": r.u32(),
            "start_cheat": r.u8()}
    info["reward_choice_items"] = _reward_list(r)
    info["reward_items"] = _reward_list(r)
    info["reward_money"] = r.u32()
    info["reward_xp"] = r.u32()             # Player::GetQuestXPReward, actual XP for this player
    info["reward_honor"] = r.u32()
    info["reward_kill_honor"] = r.f32()
    info["reward_display_spell"] = r.u32()
    info["reward_spell"] = r.i32()
    info["reward_title"] = r.u32()
    info["reward_talents"] = r.u32()
    info["reward_arena_points"] = r.u32()
    info["reward_faction_flags"] = r.u32()
    r.off += 4 * 3 * QUEST_REWARD_REPUTATIONS_COUNT
    info["emotes"] = [{"type": r.u32(), "delay": r.u32()} for _ in range(r.i32())]
    r.done()
    return info


def parse_questgiver_request_items(payload: bytes) -> dict:
    """SMSG_QUESTGIVER_REQUEST_ITEMS (0x18B) — QuestGiverRequestItems::Write
    (QuestPackets.cpp): uint64 npc guid, int32 quest id, cstring title,
    cstring completion text, int32 emote delay, int32 emote, int32 auto
    launched, uint32 flags, int32 suggested players, int32 money to get,
    uint32 count + count x (int32 item, int32 amount, uint32 display id),
    then uint32 explored/has-items/has-faction/has-money flags. Only sent
    when the quest needs items or can't be completed yet (else the server
    goes straight to OFFER_REWARD — PlayerMenu::SendQuestGiverRequestItems)."""
    r = _Reader(payload)
    info = {"npc_guid": r.u64(), "quest_id": r.i32(), "title": r.cstring(),
            "request_items_text": r.cstring(), "emote_delay": r.i32(), "emote": r.i32(),
            "auto_launched": bool(r.i32()), "flags": r.u32(), "suggested_players": r.i32(),
            "required_money": r.i32()}
    info["required_items"] = [{"entry": r.i32(), "count": r.i32(), "display_id": r.u32()}
                              for _ in range(r.u32())]
    explored = r.u32()
    r.off += 12                            # has_items/has_faction/has_money: constant 0x04/0x08/0x10
    info["can_complete"] = explored == 0x03  # `Explored = canComplete ? 0x03 : 0x00`
    r.done()
    return info


def parse_questgiver_offer_reward(payload: bytes) -> dict:
    """SMSG_QUESTGIVER_OFFER_REWARD (0x18D) — QuestGiverOfferRewardMessage::
    Write (QuestPackets.cpp): uint64 npc guid, uint32 quest id, cstring
    title, cstring reward text, uint8 auto launched, uint32 flags, uint32
    suggested players, uint32 emote count + (delay, type) pairs, reward
    choice/reward item lists, money, xp, honor, float kill honor, uint32
    unused, display spell, spell, title, talents, arena, faction flags, then
    5x3 reputation ints. CMSG_QUESTGIVER_CHOOSE_REWARD's index picks from
    `reward_choice_items`."""
    r = _Reader(payload)
    info = {"npc_guid": r.u64(), "quest_id": r.u32(), "title": r.cstring(),
            "offer_reward_text": r.cstring(), "auto_launched": bool(r.u8()),
            "flags": r.u32(), "suggested_players": r.u32()}
    info["emotes"] = [{"delay": r.u32(), "type": r.u32()} for _ in range(r.u32())]
    info["reward_choice_items"] = _reward_list(r)
    info["reward_items"] = _reward_list(r)
    info["reward_money"] = r.u32()
    info["reward_xp"] = r.u32()
    info["reward_honor"] = r.u32()
    info["reward_kill_honor"] = r.f32()
    r.u32()                                 # unused
    info["reward_display_spell"] = r.u32()
    info["reward_spell"] = r.i32()
    info["reward_title"] = r.u32()
    info["reward_talents"] = r.u32()
    info["reward_arena_points"] = r.u32()
    info["reward_faction_flags"] = r.u32()
    r.off += 4 * 3 * QUEST_REWARD_REPUTATIONS_COUNT
    r.done()
    return info


def parse_questgiver_quest_complete(payload: bytes) -> dict:
    """SMSG_QUESTGIVER_QUEST_COMPLETE (0x191) — Player::SendQuestReward
    (Entities/Player/Player.cpp): uint32 quest id, xp, money, honor, bonus
    talents, arena points. Its arrival is the turn-in success signal."""
    r = _Reader(payload)
    info = {"quest_id": r.u32(), "xp_reward": r.u32(), "money_reward": r.u32(),
            "honor_reward": r.u32(), "talent_reward": r.u32(), "arena_reward": r.u32()}
    r.done()
    return info


def parse_questgiver_quest_failed(payload: bytes) -> dict:
    """SMSG_QUESTGIVER_QUEST_FAILED (0x192) — Player::SendQuestFailed:
    uint32 quest id, uint32 reason (InventoryResult)."""
    quest_id = pk.u32(payload, 0)
    reason = pk.u32(payload, 4) if len(payload) >= 8 else 0
    return {"quest_id": quest_id, "reason": reason}


def parse_questupdate_add_kill(payload: bytes) -> dict:
    """SMSG_QUESTUPDATE_ADD_KILL (0x199) — Player::SendQuestUpdateAddCreatureOrGo
    (Player.cpp): uint32 quest id, uint32 entry (gameobjects as
    entry | 0x80000000), uint32 new count, uint32 required count, raw
    uint64 victim guid."""
    r = _Reader(payload)
    quest_id = r.u32()
    entry, is_go = _npc_or_go(r.u32())
    info = {"quest_id": quest_id, "entry": entry, "gameobject": is_go,
            "count": r.u32(), "required": r.u32(), "victim_guid": r.u64()}
    r.done()
    return info


def parse_questupdate_add_item(payload: bytes) -> dict:
    """SMSG_QUESTUPDATE_ADD_ITEM (0x19A) — Player::SendQuestUpdateAddItem
    (Player.cpp) sends it with an **empty** payload on 3.3.5 (the item id
    and count writes are commented out). Item progress is only visible in
    the bags, not in this packet or the quest-log counters."""
    return {}


def parse_questupdate_complete(payload: bytes) -> dict:
    """SMSG_QUESTUPDATE_COMPLETE (0x198) — Player::SendQuestComplete:
    uint32 quest id. Every objective is satisfied; still needs
    CMSG_QUESTGIVER_COMPLETE_QUEST at the questgiver to hand it in."""
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
