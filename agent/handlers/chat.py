"""Chat, group-invite and channel packets."""

import logging
import struct

from .. import channels as ch_mod
from .. import packets as pk
from .. import perception as per
from ..router import ROUTER

log = logging.getLogger("agent.session")

from ..opcodes import (
    SMSG_GROUP_INVITE,
    SMSG_PARTY_COMMAND_RESULT,
    SMSG_MESSAGECHAT,
    SMSG_CHAT_PLAYER_NOT_FOUND,
    SMSG_GM_MESSAGECHAT,
)

# Whisper/group-invite failure acks (UM-68 bugfix). Verified against
# TrinityCore branch `3.3.5`:
#   src/server/game/Server/Protocol/Opcodes.h
#   src/server/game/Handlers/ChatHandler.cpp (WorldSession::
#     SendPlayerNotFoundNotice — sent when the whisper target is
#     offline/unresolvable; payload is just the target name)
#   src/server/game/Handlers/GroupHandler.cpp (WorldSession::SendPartyResult,
#     used by HandleGroupInviteOpcode on both success (ERR_PARTY_RESULT_OK)
#     and failure (e.g. ERR_BAD_PLAYER_NAME_S, ERR_ALREADY_IN_GROUP_S) —
#     WhisperAction/InviteToGroupAction only treat a non-OK `result` as a
#     failure, per the SendPartyResult call sites)

# PartyResult (Group.h) — only the values SendPartyResult actually uses from
# HandleGroupInviteOpcode; anything else falls back to f"result_{n}".
PARTY_RESULT_NAMES = {
    0: "ok", 1: "bad_player_name", 2: "target_not_in_group",
    3: "target_not_in_instance", 4: "group_full", 5: "already_in_group",
    6: "not_in_group", 7: "not_leader", 8: "player_wrong_faction",
    9: "ignoring_you", 13: "invite_restricted", 19: "invite_in_combat",
}
ERR_PARTY_RESULT_OK = 0


# prompt builder treats *_LEADER/RAID/PARTY/GUILD/WHISPER/name-mention kinds
# as addressed to the agent and everything else (including monster_* other
# than monster_whisper) as background chatter — see docs/AGENT-DIRECTION.md
# "Communication happens in game only" > anti-loop rule 4.
CHAT_MSG_SYSTEM = 0x00
CHAT_MSG_SAY = 0x01
CHAT_MSG_PARTY = 0x02
CHAT_MSG_RAID = 0x03
CHAT_MSG_GUILD = 0x04
CHAT_MSG_OFFICER = 0x05
CHAT_MSG_YELL = 0x06
CHAT_MSG_WHISPER = 0x07
CHAT_MSG_WHISPER_FOREIGN = 0x08
CHAT_MSG_WHISPER_INFORM = 0x09
CHAT_MSG_EMOTE = 0x0A
CHAT_MSG_TEXT_EMOTE = 0x0B
CHAT_MSG_MONSTER_SAY = 0x0C
CHAT_MSG_MONSTER_PARTY = 0x0D
CHAT_MSG_MONSTER_YELL = 0x0E
CHAT_MSG_MONSTER_WHISPER = 0x0F
CHAT_MSG_MONSTER_EMOTE = 0x10

CHAT_MSG_CHANNEL = 0x11
CHAT_MSG_CHANNEL_JOIN = 0x12
CHAT_MSG_CHANNEL_LEAVE = 0x13
CHAT_MSG_CHANNEL_LIST = 0x14
CHAT_MSG_CHANNEL_NOTICE = 0x15
CHAT_MSG_CHANNEL_NOTICE_USER = 0x16
CHAT_MSG_AFK = 0x17
CHAT_MSG_DND = 0x18
CHAT_MSG_IGNORED = 0x19
CHAT_MSG_SKILL = 0x1A
CHAT_MSG_LOOT = 0x1B
CHAT_MSG_MONEY = 0x1C
CHAT_MSG_OPENING = 0x1D
CHAT_MSG_TRADESKILLS = 0x1E
CHAT_MSG_PET_INFO = 0x1F
CHAT_MSG_COMBAT_MISC_INFO = 0x20
CHAT_MSG_COMBAT_XP_GAIN = 0x21
CHAT_MSG_COMBAT_HONOR_GAIN = 0x22
CHAT_MSG_COMBAT_FACTION_CHANGE = 0x23
CHAT_MSG_BG_SYSTEM_NEUTRAL = 0x24
CHAT_MSG_BG_SYSTEM_ALLIANCE = 0x25
CHAT_MSG_BG_SYSTEM_HORDE = 0x26
CHAT_MSG_RAID_LEADER = 0x27
CHAT_MSG_RAID_WARNING = 0x28
CHAT_MSG_RAID_BOSS_EMOTE = 0x29
CHAT_MSG_RAID_BOSS_WHISPER = 0x2A
CHAT_MSG_FILTERED = 0x2B
CHAT_MSG_BATTLEGROUND = 0x2C
CHAT_MSG_BATTLEGROUND_LEADER = 0x2D
CHAT_MSG_RESTRICTED = 0x2E
CHAT_MSG_BATTLENET = 0x2F
CHAT_MSG_ACHIEVEMENT = 0x30
CHAT_MSG_GUILD_ACHIEVEMENT = 0x31
CHAT_MSG_ARENA_POINTS = 0x32
CHAT_MSG_PARTY_LEADER = 0x33
CHAT_MSG_ADDON = 0xFF

CHAT_KIND_NAMES = {
    CHAT_MSG_SYSTEM: "system", CHAT_MSG_SAY: "say", CHAT_MSG_PARTY: "party",
    CHAT_MSG_RAID: "raid", CHAT_MSG_GUILD: "guild", CHAT_MSG_OFFICER: "officer",
    CHAT_MSG_YELL: "yell", CHAT_MSG_WHISPER: "whisper",
    CHAT_MSG_WHISPER_FOREIGN: "whisper_foreign", CHAT_MSG_WHISPER_INFORM: "whisper_inform",
    CHAT_MSG_EMOTE: "emote", CHAT_MSG_TEXT_EMOTE: "text_emote",
    CHAT_MSG_MONSTER_SAY: "monster_say", CHAT_MSG_MONSTER_PARTY: "monster_party",
    CHAT_MSG_MONSTER_YELL: "monster_yell", CHAT_MSG_MONSTER_WHISPER: "monster_whisper",
    CHAT_MSG_MONSTER_EMOTE: "monster_emote", CHAT_MSG_CHANNEL: "channel",
    CHAT_MSG_CHANNEL_JOIN: "channel_join", CHAT_MSG_CHANNEL_LEAVE: "channel_leave",
    CHAT_MSG_CHANNEL_LIST: "channel_list", CHAT_MSG_CHANNEL_NOTICE: "channel_notice",
    CHAT_MSG_CHANNEL_NOTICE_USER: "channel_notice_user", CHAT_MSG_AFK: "afk",
    CHAT_MSG_DND: "dnd", CHAT_MSG_IGNORED: "ignored", CHAT_MSG_SKILL: "skill",
    CHAT_MSG_LOOT: "loot", CHAT_MSG_MONEY: "money", CHAT_MSG_OPENING: "opening",
    CHAT_MSG_TRADESKILLS: "tradeskills", CHAT_MSG_PET_INFO: "pet_info",
    CHAT_MSG_COMBAT_MISC_INFO: "combat_misc_info", CHAT_MSG_COMBAT_XP_GAIN: "combat_xp_gain",
    CHAT_MSG_COMBAT_HONOR_GAIN: "combat_honor_gain",
    CHAT_MSG_COMBAT_FACTION_CHANGE: "combat_faction_change",
    CHAT_MSG_BG_SYSTEM_NEUTRAL: "bg_system_neutral", CHAT_MSG_BG_SYSTEM_ALLIANCE: "bg_system_alliance",
    CHAT_MSG_BG_SYSTEM_HORDE: "bg_system_horde", CHAT_MSG_RAID_LEADER: "raid_leader",
    CHAT_MSG_RAID_WARNING: "raid_warning", CHAT_MSG_RAID_BOSS_EMOTE: "raid_boss_emote",
    CHAT_MSG_RAID_BOSS_WHISPER: "raid_boss_whisper", CHAT_MSG_FILTERED: "filtered",
    CHAT_MSG_BATTLEGROUND: "battleground", CHAT_MSG_BATTLEGROUND_LEADER: "battleground_leader",
    CHAT_MSG_RESTRICTED: "restricted", CHAT_MSG_BATTLENET: "battlenet",
    CHAT_MSG_ACHIEVEMENT: "achievement", CHAT_MSG_GUILD_ACHIEVEMENT: "guild_achievement",
    CHAT_MSG_ARENA_POINTS: "arena_points", CHAT_MSG_PARTY_LEADER: "party_leader",
    CHAT_MSG_ADDON: "addon",
}

# WorldPackets::Chat::Chat::Write (ChatPackets.cpp) branches on SlashCmd for
# what comes between the (sender_guid, flags) header and the chat text —
# everything not listed here uses the "default" branch (optional GM sender
# name, optional channel name, then a bare target_guid).
CHAT_KINDS_MONSTER = frozenset((
    CHAT_MSG_MONSTER_SAY, CHAT_MSG_MONSTER_PARTY, CHAT_MSG_MONSTER_YELL,
    CHAT_MSG_MONSTER_WHISPER, CHAT_MSG_MONSTER_EMOTE,
    CHAT_MSG_RAID_BOSS_EMOTE, CHAT_MSG_RAID_BOSS_WHISPER, CHAT_MSG_BATTLENET,
))
CHAT_KINDS_BG_SYSTEM = frozenset((CHAT_MSG_BG_SYSTEM_NEUTRAL, CHAT_MSG_BG_SYSTEM_ALLIANCE, CHAT_MSG_BG_SYSTEM_HORDE))
CHAT_KINDS_ACHIEVEMENT = frozenset((CHAT_MSG_ACHIEVEMENT, CHAT_MSG_GUILD_ACHIEVEMENT))

# ObjectGuid::HighGuid (ObjectGuid.h) — the high 16 bits of a raw (unpacked)
# 64-bit GUID. Only need enough of the enum to replicate IsPlayer()/IsPet()
# for the monster/bg-system branches' conditional target name.
_HIGHGUID_PLAYER = 0x0000
_HIGHGUID_PET = 0xF140


def _guid_is_player(guid: int) -> bool:
    return guid != 0 and (guid >> 48) == _HIGHGUID_PLAYER


def _guid_is_pet(guid: int) -> bool:
    return (guid >> 48) == _HIGHGUID_PET



def _read_len_string(data: bytes, off: int) -> tuple[str, int]:
    """A uint32-length-prefixed string (length includes the trailing null),
    as ChatPackets.cpp writes SenderName/ChatText — distinct from
    agent.packets.cstring's null-scan, which is for the plain cstrings used
    elsewhere in the same packet (e.g. channel name)."""
    length = struct.unpack_from('<I', data, off)[0]; off += 4
    s = data[off:off + length - 1].decode('utf-8', 'replace') if length > 0 else ""
    off += length
    return s, off


def handle_messagechat(ctx, opcode: int, payload: bytes):
    """SMSG_MESSAGECHAT / SMSG_GM_MESSAGECHAT (WorldPackets::Chat::Chat::Write,
    ChatPackets.cpp). SlashCmd (uint8) selects one of four shapes for
    what comes between the (sender_guid, flags) header and the chat
    text:

    - CHAT_KINDS_MONSTER (NPC say/party/yell/whisper/emote, raid-boss
      emote/whisper, battle.net): uint32 name_len + sender_name (always,
      not just on the GM opcode — the client never otherwise knows an
      NPC's name), uint64 target_guid, then [uint32 name_len +
      target_name] only if target_guid is set and isn't a player or pet
      (ObjectGuid::HighGuid, top 16 bits of the raw guid).
    - CHAT_MSG_WHISPER_FOREIGN: uint32 name_len + sender_name, uint64
      target_guid (no conditional target name).
    - CHAT_KINDS_BG_SYSTEM: uint64 target_guid, then [uint32 name_len +
      target_name] only if target_guid is set and isn't a player.
    - default (say/yell/whisper/party/guild/officer/emote/channel/
      achievement/everything else): [uint32 name_len + sender_name, only
      on SMSG_GM_MESSAGECHAT], [cstring channel, only for
      CHAT_MSG_CHANNEL], uint64 target_guid.

    Then always: uint32 text_len + text, uint8 chat_tag, and — only for
    CHAT_KINDS_ACHIEVEMENT — a trailing uint32 achievement_id (not
    needed by callers yet, left unconsumed since it's the last field).

    target_guid is deliberately not exported (UM-47). It is
    Chat::Initialize's `receiver` argument, and TrinityCore passes the
    *same object* as sender and receiver for player chat —
    Player::Say/Yell/TextEmote and both halves of Player::Whisper all
    call Initialize(..., X, X, ...). Live captures confirm it: every
    payload in tests/fixtures/chat has target_guid == sender_guid. It
    never identifies the listener, and for a whisper the addressee is
    already in sender_guid (see below), so reading it only invites the
    mistake of treating it as "who this was said to".

    The whisper pair is the one place kind alone isn't enough to know
    who is who: the recipient gets CHAT_MSG_WHISPER with the whisperer
    in sender_guid, while the whisperer gets CHAT_MSG_WHISPER_INFORM
    with the *addressee* in sender_guid.
    """
    off = 0
    slash_cmd = payload[off]; off += 1
    off += 4  # language (int32) — not needed by callers yet
    sender_guid = pk.u64(payload, off); off += 8
    off += 4  # flags (uint32), always 0 in 3.3.5

    sender_name = ""
    target_name = ""
    target_guid = 0
    channel = None

    if slash_cmd in CHAT_KINDS_MONSTER:
        sender_name, off = _read_len_string(payload, off)
        target_guid = pk.u64(payload, off); off += 8
        if target_guid and not _guid_is_player(target_guid) and not _guid_is_pet(target_guid):
            target_name, off = _read_len_string(payload, off)
    elif slash_cmd == CHAT_MSG_WHISPER_FOREIGN:
        sender_name, off = _read_len_string(payload, off)
        off += 8  # target_guid — see the docstring; not exported
    elif slash_cmd in CHAT_KINDS_BG_SYSTEM:
        target_guid = pk.u64(payload, off); off += 8
        if target_guid and not _guid_is_player(target_guid):
            target_name, off = _read_len_string(payload, off)
    else:
        if opcode == SMSG_GM_MESSAGECHAT:
            sender_name, off = _read_len_string(payload, off)
        if slash_cmd == CHAT_MSG_CHANNEL:
            channel, off = pk.cstring(payload, off)
        off += 8  # target_guid — see the docstring; not exported

    text, off = _read_len_string(payload, off)
    off += 1  # chat_tag (uint8) — not needed by callers yet

    entry = {
        "kind": CHAT_KIND_NAMES.get(slash_cmd, f"type_{slash_cmd}"),
        "sender_guid": sender_guid,
        "sender_name": sender_name,
        "channel": channel,
        "text": text,
    }
    if target_name:
        entry["target_name"] = target_name
    ctx.state.chat_inbox.append(entry)
    relay_chat(ctx, entry)


def relay_chat(ctx, entry: dict):
    """UM-47: mirror a heard message to tools/chat-feed, if a relay is
    configured. Never raises and never blocks the recv loop — see
    agent/chat_relay.py."""
    relay = ctx.state.chat_relay
    if relay is None:
        return
    try:
        relay.submit(entry, resolve_name=ctx.state.world_state.resolve_player_name)
    except Exception:  # noqa: BLE001 — chat must keep flowing to the agent itself
        log.debug("chat relay submit failed", exc_info=True)


def handle_group_invite(ctx, payload: bytes):
    # PartyInvite::Write (PartyPackets.cpp): uint8 can_accept, cstring
    # inviter_name, uint32 proposed_roles, ... (LFG fields, ignored).
    off = 1  # skip can_accept
    inviter_name, off = pk.cstring(payload, off)
    ctx.state.pending_invite = {"inviter_name": inviter_name}
    log.info("group invite from %s", inviter_name)


def handle_chat_player_not_found(ctx, payload: bytes):
    """SMSG_CHAT_PLAYER_NOT_FOUND (0x2A9): WorldSession::
    SendPlayerNotFoundNotice (ChatHandler.cpp) — a single cstring, the
    target name that couldn't be resolved (offline, doesn't exist, or
    (for whisper) has whispers disabled/is ignoring us). Sent instead of
    any ack for the CMSG_MESSAGECHAT that triggered it, so
    WhisperAction.execute() polls session.events for this rather than a
    direct per-message ack."""
    target_name, _ = pk.cstring(payload, 0)
    ctx.state.record_event("whisper_failed", target_name=target_name)


def handle_channel_notify(ctx, payload: bytes):
    """SMSG_CHANNEL_NOTIFY (0x099, agent.channels.parse_channel_notify).
    you_joined/you_left update world_state.channels; they and every
    error notice (not_member, muted, throttled, ...) are also recorded
    as channel_joined / channel_left / channel_error events so
    join_channels() and ChannelSayAction can confirm the outcome."""
    try:
        data = ch_mod.parse_channel_notify(payload)
    except (IndexError, ValueError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_CHANNEL_NOTIFY ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_channel_notify(data)
    if data["notice"] == ch_mod.CHAT_YOU_JOINED_NOTICE:
        ctx.state.record_event("channel_joined", channel=data["channel"],
                            channel_id=data["channel_id"], flags=data["flags"])
    elif data["notice"] == ch_mod.CHAT_YOU_LEFT_NOTICE:
        ctx.state.record_event("channel_left", channel=data["channel"], channel_id=data["channel_id"])
    elif data["notice"] in ch_mod.ERROR_NOTICES:
        ctx.state.record_event("channel_error", channel=data["channel"],
                            notice=data["notice"], reason=data["notice_name"])


def handle_party_command_result(ctx, payload: bytes):
    """SMSG_PARTY_COMMAND_RESULT (0x07F): WorldSession::SendPartyResult
    (GroupHandler.cpp) — uint32 operation, cstring member_name, uint32
    result (PartyResult), uint32 val (LFG-cooldown related, unused
    here). HandleGroupInviteOpcode sends this on both success
    (ERR_PARTY_RESULT_OK) and failure (e.g. a bad name or a target
    already in a group) — only record it as an event on failure, so
    InviteToGroupAction's timeout-is-success default still applies to
    the OK case without special-casing it."""
    off = 0
    operation = pk.u32(payload, off); off += 4
    member_name, off = pk.cstring(payload, off)
    result = pk.u32(payload, off); off += 4
    if result == ERR_PARTY_RESULT_OK:
        return
    ctx.state.record_event("group_invite_failed", target_name=member_name,
                        result=result,
                        result_name=PARTY_RESULT_NAMES.get(result, f"result_{result}"))


def _messagechat_for(opcode):
    def handler(ctx, payload):
        handle_messagechat(ctx, opcode, payload)
    return handler


ROUTER.register_all({
    SMSG_MESSAGECHAT: _messagechat_for(SMSG_MESSAGECHAT),
    SMSG_GM_MESSAGECHAT: _messagechat_for(SMSG_GM_MESSAGECHAT),
    SMSG_GROUP_INVITE: handle_group_invite,
    SMSG_CHAT_PLAYER_NOT_FOUND: handle_chat_player_not_found,
    ch_mod.SMSG_CHANNEL_NOTIFY: handle_channel_notify,
    SMSG_PARTY_COMMAND_RESULT: handle_party_command_result,
})
