#!/usr/bin/env python3
"""Chat channels (UM-93): joining General/custom channels and parsing
SMSG_CHANNEL_NOTIFY. Pure builders/parsers (no I/O), same split as
agent/mail.py and agent/trade.py; agent/session.py dispatches the notify and
agent/actions.py's `channel_say` sends into a joined channel.

Verified against TrinityCore branch `3.3.5` (commit 48128f325ac5):
  src/server/game/Server/Protocol/Opcodes.h (CMSG_JOIN_CHANNEL 0x097,
    CMSG_LEAVE_CHANNEL 0x098, SMSG_CHANNEL_NOTIFY 0x099)
  src/server/game/Server/Packets/ChannelPackets.cpp (JoinChannel::Read,
    LeaveChannel::Read, ChannelNotify::Write)
  src/server/game/Handlers/ChannelHandler.cpp (HandleJoinChannel)
  src/server/game/Chat/Channels/ChannelMgr.cpp (GetSystemChannel,
    GetChannelForPlayerByNamePart — how CHAT_MSG_CHANNEL finds the channel)
  src/server/game/Chat/Channels/Channel.h (enum ChatNotify, ChannelFlags)
  src/server/game/Chat/Channels/ChannelAppenders.h (YouJoinedAppend: the
    notify's channel name is Channel::GetName(), e.g. "General - Eversong Woods")
  src/server/game/Entities/Player/Player.cpp (UpdateLocalChannels: "The
    client handles it automatically after loading" — the server does NOT
    auto-join General at login; a real client sends CMSG_JOIN_CHANNEL itself)

System channels (ChatChannels.dbc) are joined by ID: HandleJoinChannel
ignores the name when ChatChannelId != 0 and picks the zone's instance of
the channel itself (GetSystemChannel(id, zone)). The server then names it
"General - <Zone>" from the DBC pattern. Custom channels (ChatChannelId 0)
are joined/created by name. IDs below were read from the live realm's
ChatChannels.dbc (/opt/wowmap-data/dbc/ChatChannels.dbc).
"""

import struct

from . import packets as pk

CMSG_JOIN_CHANNEL = 0x097
CMSG_LEAVE_CHANNEL = 0x098
SMSG_CHANNEL_NOTIFY = 0x099

# ChatChannels.dbc (enUS name pattern in comments), keyed by the short name
# a player types ("/join General"). Lower-cased for lookup.
SYSTEM_CHANNEL_IDS = {
    "general": 1,            # "General - %s"
    "trade": 2,              # "Trade - %s" (cities only)
    "localdefense": 22,      # "LocalDefense - %s"
    "worlddefense": 23,      # "WorldDefense"
    "guildrecruitment": 25,  # "GuildRecruitment - %s" (cities only)
    "lookingforgroup": 26,   # "LookingForGroup" (Channel.RestrictedLfg = 1 on this realm)
}

MAX_CHANNEL_NAME_LEN = 31  # ChannelHandler.cpp MAX_CHANNEL_NAME_STR

# enum ChatNotify (Channel.h)
CHAT_JOINED_NOTICE = 0x00
CHAT_LEFT_NOTICE = 0x01
CHAT_YOU_JOINED_NOTICE = 0x02
CHAT_YOU_LEFT_NOTICE = 0x03
CHAT_WRONG_PASSWORD_NOTICE = 0x04
CHAT_NOT_MEMBER_NOTICE = 0x05
CHAT_MODE_CHANGE_NOTICE = 0x0C
CHAT_MUTED_NOTICE = 0x11
CHAT_PLAYER_KICKED_NOTICE = 0x12
CHAT_BANNED_NOTICE = 0x13
CHAT_PLAYER_BANNED_NOTICE = 0x14
CHAT_PLAYER_UNBANNED_NOTICE = 0x15
CHAT_WRONG_FACTION_NOTICE = 0x1A
CHAT_INVALID_NAME_NOTICE = 0x1B
CHAT_THROTTLED_NOTICE = 0x1F
CHAT_NOT_IN_AREA_NOTICE = 0x20
CHAT_NOT_IN_LFG_NOTICE = 0x21

NOTIFY_NAMES = {
    0x00: "joined", 0x01: "left", 0x02: "you_joined", 0x03: "you_left",
    0x04: "wrong_password", 0x05: "not_member", 0x06: "not_moderator",
    0x07: "password_changed", 0x08: "owner_changed", 0x09: "player_not_found",
    0x0A: "not_owner", 0x0B: "channel_owner", 0x0C: "mode_change",
    0x0D: "announcements_on", 0x0E: "announcements_off", 0x0F: "moderation_on",
    0x10: "moderation_off", 0x11: "muted", 0x12: "player_kicked", 0x13: "banned",
    0x14: "player_banned", 0x15: "player_unbanned", 0x16: "player_not_banned",
    0x17: "player_already_member", 0x18: "invite", 0x19: "invite_wrong_faction",
    0x1A: "wrong_faction", 0x1B: "invalid_name", 0x1C: "not_moderated",
    0x1D: "player_invited", 0x1E: "player_invite_banned", 0x1F: "throttled",
    0x20: "not_in_area", 0x21: "not_in_lfg", 0x22: "voice_on", 0x23: "voice_off",
}

# Notices that mean "you can't join / can't speak here" — recorded as
# channel_error events so a join or channel_say can fail with a reason.
ERROR_NOTICES = {
    CHAT_WRONG_PASSWORD_NOTICE, CHAT_NOT_MEMBER_NOTICE, CHAT_MUTED_NOTICE,
    CHAT_BANNED_NOTICE, CHAT_WRONG_FACTION_NOTICE, CHAT_INVALID_NAME_NOTICE,
    CHAT_THROTTLED_NOTICE, CHAT_NOT_IN_AREA_NOTICE, CHAT_NOT_IN_LFG_NOTICE,
}

# ChannelNotify::Write groups (ChannelPackets.cpp)
_GUID_NOTICES = {0x00, 0x01, 0x07, 0x08, 0x0D, 0x0E, 0x0F, 0x10, 0x17, 0x18, 0x22, 0x23}
_NAME_NOTICES = {0x09, 0x0B, 0x16, 0x1D, 0x1E}
_TARGET_SENDER_NOTICES = {0x12, 0x14, 0x15}

DEFAULT_CHANNELS = "General"


def system_channel_id(name: str) -> int:
    """ChatChannels.dbc ID for a short system-channel name ("General"), or 0
    for a custom channel. Also accepts the full server-side name
    ("General - Eversong Woods")."""
    short = name.split(" - ", 1)[0].strip().lower()
    return SYSTEM_CHANNEL_IDS.get(short, 0)


def parse_channel_spec(spec: str | None) -> list[str]:
    """AGENT_CHANNELS: comma-separated channel names, e.g. "General,world".
    Empty/None falls back to DEFAULT_CHANNELS; "none" disables auto-join.
    Duplicates (case-insensitive) are dropped, order kept."""
    if spec is None or not spec.strip():
        spec = DEFAULT_CHANNELS
    if spec.strip().lower() == "none":
        return []
    out, seen = [], set()
    for raw in spec.split(","):
        name = raw.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def build_join_channel(name: str, password: str = "") -> bytes:
    """CMSG_JOIN_CHANNEL payload. JoinChannel::Read: int32 ChatChannelId,
    uint8 CreateVoiceSession, uint8 Internal, cstring ChannelName, cstring
    Password. A system channel is joined by ID (the server ignores the name
    then, and only logs it); a custom channel by name, ID 0."""
    if not name:
        raise ValueError("channel name is required")
    channel_id = system_channel_id(name)
    if not channel_id:
        if name[0].isdigit():
            raise ValueError("custom channel names can't start with a digit")
        if len(name) > MAX_CHANNEL_NAME_LEN:
            raise ValueError(f"channel name longer than {MAX_CHANNEL_NAME_LEN} characters")
    return (struct.pack("<iBB", channel_id, 0, 0)
            + name.encode("utf-8") + b"\x00"
            + password.encode("utf-8") + b"\x00")


def build_leave_channel(name: str) -> bytes:
    """CMSG_LEAVE_CHANNEL payload. LeaveChannel::Read: int32 ZoneChannelID,
    cstring ChannelName (the full joined name)."""
    return struct.pack("<i", system_channel_id(name)) + name.encode("utf-8") + b"\x00"


def _cstring_utf8(data: bytes, off: int) -> tuple[str, int]:
    end = data.index(0, off)
    return data[off:end].decode("utf-8", errors="replace"), end + 1


def parse_channel_notify(payload: bytes) -> dict:
    """SMSG_CHANNEL_NOTIFY. ChannelNotify::Write: uint8 Type, cstring
    Channel, then a Type-dependent tail (raw uint64 GUIDs, not packed):

      you_joined:  uint8 flags, int32 channel_id, int32 instance_id
      you_left:    int32 channel_id, uint8 suspended
      joined/left/owner_changed/...: uint64 sender_guid
      player_not_found/channel_owner/...: cstring sender_name
      mode_change: uint64 sender_guid, uint8 old_flags, uint8 new_flags
      player_kicked/banned/unbanned: uint64 target_guid, uint64 sender_guid
      everything else (errors like not_member, muted, throttled): nothing
    """
    off = 0
    kind = payload[off]; off += 1
    channel, off = _cstring_utf8(payload, off)
    out = {"notice": kind, "notice_name": NOTIFY_NAMES.get(kind, f"notice_{kind}"),
           "channel": channel}
    if kind == CHAT_YOU_JOINED_NOTICE:
        out["flags"] = payload[off]; off += 1
        out["channel_id"], out["instance_id"] = struct.unpack_from("<ii", payload, off); off += 8
    elif kind == CHAT_YOU_LEFT_NOTICE:
        out["channel_id"] = struct.unpack_from("<i", payload, off)[0]; off += 4
        out["suspended"] = bool(payload[off]); off += 1
    elif kind in _GUID_NOTICES:
        out["sender_guid"] = pk.u64(payload, off); off += 8
    elif kind in _NAME_NOTICES:
        out["sender_name"], off = _cstring_utf8(payload, off)
    elif kind == CHAT_MODE_CHANGE_NOTICE:
        out["sender_guid"] = pk.u64(payload, off); off += 8
        out["old_flags"] = payload[off]; out["new_flags"] = payload[off + 1]; off += 2
    elif kind in _TARGET_SENDER_NOTICES:
        out["target_guid"] = pk.u64(payload, off)
        out["sender_guid"] = pk.u64(payload, off + 8); off += 16
    if off != len(payload):
        raise ValueError(f"SMSG_CHANNEL_NOTIFY {out['notice_name']}: consumed {off} of {len(payload)} bytes")
    return out


def find_joined(channels: dict, name: str) -> str | None:
    """The joined channel `name` refers to, matching the server's own lookup
    for CHAT_MSG_CHANNEL (ChannelMgr::GetChannelForPlayerByNamePart: a
    case-insensitive prefix of the full joined name). So "general" finds
    "General - Eversong Woods". Returns the full name, or None."""
    lname = name.strip().lower()
    if not lname:
        return None
    for full in channels:
        if full.lower().startswith(lname):
            return full
    return None
