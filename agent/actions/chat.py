"""Chat, group invites and channel chat actions and helpers (UM-68, UM-93; chat actions other than invite/accept are unregistered, UM-98).

Split out of the former agent/actions.py (issue #248).
"""

import struct
import time

from .. import channels as chmod
from ..opcodes import (
    CMSG_GROUP_ACCEPT,
    CMSG_GROUP_DECLINE,
    CMSG_GROUP_DISBAND,
    CMSG_GROUP_INVITE,
    CMSG_GROUP_SET_LEADER,
    CMSG_MESSAGECHAT,
    CMSG_TEXT_EMOTE,
)
from .base import (
    Action,
    ActionResult,
    DEFAULT_CONFIRM_POLL_S,
    DEFAULT_CONFIRM_TIMEOUT_S,
    _wait_for,
    _wait_for_value,
    register,
    send,
)


# ChatMsg (SharedDefines.h) — the values here previously mapped 'say' to 0,
# which is CHAT_MSG_SYSTEM; players can't legitimately send that type.
CHAT_MSG_SAY = 0x01
CHAT_MSG_PARTY = 0x02
CHAT_MSG_GUILD = 0x04
CHAT_MSG_YELL = 0x06
CHAT_MSG_WHISPER = 0x07
CHAT_MSG_EMOTE = 0x0A

# Language (SharedDefines.h)
LANG_UNIVERSAL = 0
LANG_ORCISH = 1
LANG_COMMON = 7


# ChrRaces.dbc race IDs (matches WoWSession.enum_characters()'s 'race' field).
_ALLIANCE_RACES = {1, 3, 4, 7, 11}  # Human, Dwarf, Night Elf, Gnome, Draenei
_HORDE_RACES = {2, 5, 6, 8, 10}     # Orc, Undead, Tauren, Troll, Blood Elf


def _racial_language(session) -> int:
    """LANG_UNIVERSAL (0) is rejected server-side as a "possible
    hacking-attempt" for every message type except AFK/DND
    (ChatHandler.cpp::HandleChatMessage) — even for GM accounts, which only
    get silently upgraded to universal *after* passing that check
    (`if (sender->IsGameMaster()) lang = LANG_UNIVERSAL;`, further down the
    same function). Send the character's own racial language instead; every
    account on this realm today is Horde, so default there when race is
    unknown."""
    race = session.race
    if race in _ALLIANCE_RACES:
        return LANG_COMMON
    return LANG_ORCISH


def _encode_message(message: str) -> bytes:
    """UTF-8 (not ASCII — mangles accents like "Olá"), newlines stripped,
    capped at 255 bytes (ChatHandler.cpp rejects msg.size() > 255) without
    splitting a multi-byte character in half."""
    message = message.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    encoded = message.encode("utf-8")
    if len(encoded) > 255:
        # Truncate, then drop any incomplete multi-byte sequence left
        # dangling at the cut point by decoding leniently and re-encoding.
        encoded = encoded[:255].decode("utf-8", errors="ignore").encode("utf-8")
    return encoded


def _send_chat(session, slash_cmd: int, message: str, target_name: str | None = None):
    # ChatMessage::Read (ChatPackets.cpp): int32 type, int32 language,
    # [whisper/channel only: cstring target], cstring text.
    payload = struct.pack("<ii", slash_cmd, _racial_language(session))
    if target_name is not None:
        payload += target_name.encode("utf-8") + b"\x00"
    payload += _encode_message(message) + b"\x00"
    send(session, CMSG_MESSAGECHAT, payload)


def say(session, message: str):
    _send_chat(session, CHAT_MSG_SAY, message)


def yell(session, message: str):
    _send_chat(session, CHAT_MSG_YELL, message)


def whisper(session, target_name: str, message: str):
    _send_chat(session, CHAT_MSG_WHISPER, message, target_name=target_name)


def emote(session, text: str):
    """/emote text — CHAT_MSG_EMOTE, a free-text roleplay line (distinct
    from text_emote(), which plays a predefined animated emote)."""
    _send_chat(session, CHAT_MSG_EMOTE, text)


def text_emote(session, emote_id: int, target_guid: int = 0):
    """A predefined emote (/wave, /dance, ...) by Emotes.dbc ID.
    CTextEmote::Read (ChatPackets.cpp) reads EmoteID, SoundIndex, then Target
    (the header's member order differs; the Read() order is what's on the wire)."""
    payload = struct.pack("<iiQ", emote_id, 0, target_guid)
    send(session, CMSG_TEXT_EMOTE, payload)


def invite_to_group(session, name: str):
    # PartyInviteClient::Read (PartyPackets.cpp): cstring TargetName, uint32 ProposedRoles.
    payload = name.encode("utf-8") + b"\x00" + struct.pack("<I", 0)
    send(session, CMSG_GROUP_INVITE, payload)


def accept_group(session):
    # HandleGroupAcceptOpcode (GroupHandler.cpp) only read_skip<uint32>()s the payload.
    send(session, CMSG_GROUP_ACCEPT, struct.pack("<I", 0))
    session.pending_invite = None


def decline_group(session):
    # HandleGroupDeclineOpcode (GroupHandler.cpp) ignores the payload.
    send(session, CMSG_GROUP_DECLINE)
    session.pending_invite = None


def promote_leader(session, guid: int):
    # HandleGroupSetLeaderOpcode (GroupHandler.cpp): `recvData >> guid` — a raw
    # 8-byte ObjectGuid; silently ignored unless we lead and guid is a member.
    send(session, CMSG_GROUP_SET_LEADER, struct.pack("<Q", guid))


def leave_group(session):
    # HandleGroupDisbandOpcode (GroupHandler.cpp) takes an unused WorldPacket — no payload.
    send(session, CMSG_GROUP_DISBAND)


def send_chat_message(session, message: str, channel: str = "say", target: str | None = None):
    """Back-compat entry point for WoWSession.send_chat(); prefer the
    say/yell/whisper/emote functions above directly."""
    if channel == "whisper":
        if not target:
            raise ValueError("whisper requires a target name")
        whisper(session, target, message)
    elif channel == "yell":
        yell(session, message)
    elif channel == "emote":
        emote(session, message)
    else:
        say(session, message)


# ── Chat / social (UM-68) ─────────────────────────────────────────────────
# Wraps the plain say/yell/whisper/emote/invite_to_group/accept_group
# functions above as Action subclasses so they show up in catalog() — until
# now they were only reachable by calling the free functions directly, so
# the LLM (agent/think.py) had no way to invoke them at all.
#
# UM-98 (docs/adr/0001-jev-in-the-think-loop.md): chat is deferred, not
# dropped. The chat-generation actions (say/yell/whisper/emote, and
# channel_say below) are no longer @register-ed, so catalog() doesn't offer
# them to the brain; the classes and send functions stay for the future
# roleplay layer. To bring one back, re-add @register.
# invite_to_group/accept_group stay registered.

_JSON_DEBRIS = set('{}[]":,\\')


def chat_text_error(text, field: str = "message") -> str | None:
    """UM-92: reject model output that isn't a real chat line before it
    reaches public chat — live, a tool call leaked `say "}"` into /say.
    Accepts any text with at least one letter or digit and no control
    characters that isn't JSON debris (e.g. `{"message": "hi"}`)."""
    if not isinstance(text, str):
        return f"{field} must be a string"
    stripped = text.strip()
    if not any(ch.isalnum() for ch in stripped):
        return f"{field} {text!r} has no letters or digits — not a chat message"
    if any(ord(ch) < 32 for ch in stripped):
        return f"{field} contains control characters"
    if stripped[0] in "{[" and stripped[-1] in "}]":
        return f"{field} looks like JSON, not a chat message"
    debris = sum(ch in _JSON_DEBRIS for ch in stripped)
    if debris * 2 > len(stripped):
        return f"{field} {text!r} is mostly punctuation — not a chat message"
    return None


def player_name_error(name, field: str = "name") -> str | None:
    """UM-92: WoW character names are 2-12 letters, nothing else
    (ObjectMgr::CheckPlayerName). Catches hallucinated names like
    `}}dotspans` or `: ` before they reach a lookup or a packet."""
    if not isinstance(name, str):
        return f"{field} must be a string"
    stripped = name.strip()
    if not (2 <= len(stripped) <= 12) or not stripped.isalpha():
        return f"{field} {name!r} is not a valid character name (2-12 letters)"
    return None


def _known_player_name(session, world, target_name: str) -> bool:
    """A whisper target is "resolvable" if its name has shown up either as a
    currently-perceived object (agent/perception.py's ObjectInfo.name,
    filled in via agent/names.py's CMSG_NAME_QUERY cache) or as the sender
    of a recent chat message (session.chat_inbox) — either is enough
    evidence the name is real and spelled correctly, without requiring the
    player to be nearby right now (e.g. replying to a whisper from someone
    out of range)."""
    lname = target_name.lower()
    for obj in world.get_objects().values():
        if obj.name and obj.name.lower() == lname:
            return True
    for entry in session.chat_inbox:
        if entry.get("sender_name", "").lower() == lname:
            return True
    return False


class SayAction(Action):  # not registered: chat deferred (UM-98)
    name = "say"
    description = "Speak a chat message aloud (/say) — audible to nearby players."
    params = {
        "message": {"type": "string", "description": "The message to say."},
    }
    required = ("message",)

    def check(self, session, world, message: str, **_) -> str | None:
        return chat_text_error(message)

    def execute(self, session, world, message: str, **_) -> ActionResult:
        say(session, message)
        return ActionResult(ok=True, detail={"message": message})


class YellAction(Action):  # not registered: chat deferred (UM-98)
    name = "yell"
    description = "Shout a chat message (/yell) — audible over a much larger radius than say."
    params = {
        "message": {"type": "string", "description": "The message to yell."},
    }
    required = ("message",)

    def check(self, session, world, message: str, **_) -> str | None:
        return chat_text_error(message)

    def execute(self, session, world, message: str, **_) -> ActionResult:
        yell(session, message)
        return ActionResult(ok=True, detail={"message": message})


class WhisperAction(Action):  # not registered: chat deferred (UM-98)
    name = "whisper"
    description = ("Send a private message (/whisper) to a specific player by name. The name "
                    "must be resolvable — either a currently-perceived player or the sender of "
                    "a recent chat message (see the snapshot's chat_inbox).")
    params = {
        "target_name": {"type": "string", "description": "Exact player name to whisper."},
        "message": {"type": "string", "description": "The message to send."},
    }
    required = ("target_name", "message")
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, target_name: str, message: str, **_) -> str | None:
        error = player_name_error(target_name, field="target_name") or chat_text_error(message)
        if error is not None:
            return error
        if not _known_player_name(session, world, target_name):
            return f"{target_name!r} is not a known/resolvable player name"
        return None

    def execute(self, session, world, target_name: str, message: str, **_) -> ActionResult:
        sent_at = time.monotonic()
        whisper(session, target_name, message)

        # TrinityCore sends SMSG_CHAT_PLAYER_NOT_FOUND only on failure (an
        # offline/whisper-disabled/ignoring target — see
        # WorldSession::SendPlayerNotFoundNotice, ChatHandler.cpp); a
        # successful whisper gets no ack at all. Same fire-and-forget,
        # wait-for-failure-only pattern as SellItemAction.
        def find_failure():
            for e in session.events:
                if (e.get("t", 0) >= sent_at and e.get("kind") == "whisper_failed"
                        and e.get("target_name") == target_name):
                    return e
            return None

        failure = _wait_for_value(find_failure, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"target_name": target_name, "message": message}
        if failure is not None:
            detail["failure"] = failure
            return ActionResult(ok=False, error=f"player {target_name!r} not found", detail=detail)
        return ActionResult(ok=True, detail=detail)


class EmoteAction(Action):  # not registered: chat deferred (UM-98)
    name = "emote"
    description = "Send a free-text roleplay emote (/emote) — distinct from a predefined animated emote."
    params = {
        "text": {"type": "string", "description": "The emote text."},
    }
    required = ("text",)

    def check(self, session, world, text: str, **_) -> str | None:
        return chat_text_error(text, field="text")

    def execute(self, session, world, text: str, **_) -> ActionResult:
        emote(session, text)
        return ActionResult(ok=True, detail={"text": text})


@register
class InviteToGroupAction(Action):
    name = "invite_to_group"
    description = "Invite a player to a party/group by name."
    params = {
        "name": {"type": "string", "description": "Exact player name to invite."},
    }
    required = ("name",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, name: str, **_) -> str | None:
        return player_name_error(name)

    def execute(self, session, world, name: str, **_) -> ActionResult:
        sent_at = time.monotonic()
        invite_to_group(session, name)

        # HandleGroupInviteOpcode (GroupHandler.cpp) answers with
        # SMSG_PARTY_COMMAND_RESULT on both success and failure, but
        # the party-command-result handler only records an event for a
        # non-OK result (see its docstring) — so, like whisper/sell_item,
        # only a failure event is worth waiting for; a timeout (or a
        # silently-successful OK) means the invite went through.
        def find_failure():
            for e in session.events:
                if (e.get("t", 0) >= sent_at and e.get("kind") == "group_invite_failed"
                        and e.get("target_name") == name):
                    return e
            return None

        failure = _wait_for_value(find_failure, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"name": name}
        if failure is not None:
            detail["failure"] = failure
            return ActionResult(ok=False, error=failure.get("result_name", "invite failed"), detail=detail)
        return ActionResult(ok=True, detail=detail)


@register
class AcceptGroupAction(Action):
    name = "accept_group"
    description = "Accept the currently pending party invite (see the snapshot's pending_invite)."
    params = {}
    required = ()

    def check(self, session, world, **_) -> str | None:
        if not session.pending_invite:
            return "no pending group invite to accept"
        return None

    def execute(self, session, world, **_) -> ActionResult:
        invite = session.pending_invite
        accept_group(session)
        return ActionResult(ok=True, detail={"inviter_name": (invite or {}).get("inviter_name")})


@register
class DeclineGroupAction(Action):
    name = "decline_group"
    description = "Decline the currently pending party invite (see the snapshot's pending_invite)."
    params = {}
    required = ()

    def check(self, session, world, **_) -> str | None:
        if not session.pending_invite:
            return "no pending group invite to decline"
        return None

    def execute(self, session, world, **_) -> ActionResult:
        invite = session.pending_invite
        decline_group(session)
        return ActionResult(ok=True, detail={"inviter_name": (invite or {}).get("inviter_name")})


@register
class LeaveGroupAction(Action):
    name = "leave_group"
    description = "Leave the party you are in (see the snapshot's group)."
    params = {}
    required = ()
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, **_) -> str | None:
        if not session.group:
            return "not in a group"
        return None

    def execute(self, session, world, **_) -> ActionResult:
        leave_group(session)
        # The server answers with SMSG_GROUP_LIST (leave form) or SMSG_GROUP_DESTROYED,
        # both of which clear session.group. Wait for that instead of assuming.
        left = _wait_for(lambda: session.group is None,
                         timeout=self.confirm_timeout, interval=self.confirm_interval)
        if not left:
            return ActionResult(ok=False, error="server did not confirm leaving the group")
        return ActionResult(ok=True)


@register
class PromoteLeaderAction(Action):
    name = "promote_leader"
    description = "Hand party leadership to a member of your group (you must be the leader)."
    params = {
        "name": {"type": "string", "description": "Exact name of the group member to make leader."},
    }
    required = ("name",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    @staticmethod
    def _member(session, name: str):
        group = session.group or {}
        return next((m for m in group.get("members", ()) if m["name"].lower() == name.lower()), None)

    def check(self, session, world, name: str, **_) -> str | None:
        err = player_name_error(name)
        if err:
            return err
        group = session.group
        if not group:
            return "not in a group"
        if group["leader_guid"] != session.player_guid:
            return "only the group leader can promote someone"
        if self._member(session, name) is None:
            return f"{name} is not in your group"
        return None

    def execute(self, session, world, name: str, **_) -> ActionResult:
        member = self._member(session, name)
        promote_leader(session, member["guid"])
        # The server ignores a bad request silently; success is the next
        # SMSG_GROUP_LIST naming the new leader.
        changed = _wait_for(lambda: (session.group or {}).get("leader_guid") == member["guid"],
                            timeout=self.confirm_timeout, interval=self.confirm_interval)
        if not changed:
            return ActionResult(ok=False, error="server did not confirm the leader change",
                                detail={"name": member["name"]})
        return ActionResult(ok=True, detail={"name": member["name"]})


@register
class CloseWindowAction(Action):
    name = "close_window"
    description = "Close the currently open gossip/vendor/trainer window."
    params = {}
    required = ()

    def execute(self, session, world, **_) -> ActionResult:
        was_open = world.get_ui_state() is not None
        world.close_window()
        return ActionResult(ok=True, detail={"was_open": was_open})


# ── Channel chat (UM-93) ──────────────────────────────────────────────────
# Kept in its own section (not folded into _send_chat/say/whisper above) so
# parallel work on those (UM-92's text validation) merges cleanly.
# UM-98: ChannelSayAction is not registered (chat deferred, see the Chat /
# social section above); joining channels stays so heard channel chat
# still reaches chat_inbox and the chat-feed relay.

CHAT_MSG_CHANNEL = 0x11  # enum ChatMsg (SharedDefines.h)

# Channel chat is heard zone-wide (or realm-wide in a custom channel), so it
# gets its own outgoing limits (docs/AGENT-DIRECTION.md §4 rule 3): at most
# one channel message per CHANNEL_MIN_INTERVAL_S per agent, and no repeat of
# any of the last CHANNEL_RECENT_MAX messages (compared normalised: case,
# spacing and punctuation ignored). There is no shared outgoing-chat limiter
# to reuse yet (say/whisper send unthrottled today).
CHANNEL_MIN_INTERVAL_S = 60.0
CHANNEL_RECENT_MAX = 10


def _normalise_chat(text: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def build_channel_message(language: int, channel: str, message: str) -> bytes:
    """CMSG_MESSAGECHAT for CHAT_MSG_CHANNEL. ChatMessage::Read
    (ChatPackets.cpp): int32 type, int32 language, cstring Target (the
    channel name), cstring text. HandleChatMessage (ChatHandler.cpp) then
    finds the channel with ChannelMgr::GetChannelForPlayerByNamePart (a
    case-insensitive prefix match over the channels we've joined) and drops
    the message silently if none matches."""
    return (struct.pack("<ii", CHAT_MSG_CHANNEL, language)
            + channel.encode("utf-8") + b"\x00"
            + _encode_message(message) + b"\x00")


def channel_say(session, channel: str, message: str):
    send(session, CMSG_MESSAGECHAT, build_channel_message(_racial_language(session), channel, message))


class ChannelSayAction(Action):  # not registered: chat deferred (UM-98)
    name = "channel_say"
    description = ("Say something in a chat channel you have joined (see the snapshot's "
                   "'channels', e.g. 'General - Eversong Woods'; 'General' is enough). "
                   "Everyone in that channel across the zone hears it. It's background "
                   "chat, not a conversation: keep it rare and short. At most one channel "
                   "message per minute, and never the same message twice.")
    params = {
        "channel": {"type": "string", "description": "A joined channel name, or its start (e.g. 'General')."},
        "message": {"type": "string", "description": "The message to send."},
    }
    required = ("channel", "message")
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S
    min_interval = CHANNEL_MIN_INTERVAL_S

    def check(self, session, world, channel: str, message: str, **_) -> str | None:
        joined = world.get_channels()
        if chmod.find_joined(joined, channel) is None:
            return f"not in channel {channel!r}; joined channels: {sorted(joined) or 'none'}"
        if not message or not message.strip():
            return "message is empty"
        history = session.channel_say_history
        if history:
            wait = self.min_interval - (time.monotonic() - history[-1][0])
            if wait > 0:
                return f"channel chat is rate-limited; wait {wait:.0f}s"
        norm = _normalise_chat(message)
        if any(norm == prev for _, prev in history):
            return "already said that recently; don't repeat channel messages"
        return None

    def execute(self, session, world, channel: str, message: str, **_) -> ActionResult:
        full = chmod.find_joined(world.get_channels(), channel)
        text = _encode_message(message).decode("utf-8")
        inbox = session.chat_inbox
        seen = {id(e) for e in inbox}
        sent_at = time.monotonic()
        channel_say(session, full, message)
        history = session.channel_say_history
        history.append((sent_at, _normalise_chat(message)))
        del history[:-CHANNEL_RECENT_MAX]

        # No ack on success, but Channel::Say (Channel.cpp) sends the message
        # to every member including us, so our own echo confirms it. A
        # refusal comes back as SMSG_CHANNEL_NOTIFY (not_member, muted,
        # throttled, ...), which the session records as a channel_error event.
        def find_outcome():
            for e in list(inbox):
                if (id(e) not in seen and e.get("kind") == "channel"
                        and e.get("sender_guid") == session.player_guid and e.get("text") == text):
                    return {"echo": e}
            for e in list(session.events):
                if e.get("t", 0) >= sent_at and e.get("kind") == "channel_error":
                    return {"error": e}
            return None

        outcome = _wait_for_value(find_outcome, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"channel": full, "message": text}
        if outcome is None:
            return ActionResult(ok=False, error="no echo of the channel message seen (unconfirmed)", detail=detail)
        if "error" in outcome:
            detail["failure"] = outcome["error"]
            return ActionResult(ok=False, error=f"channel refused the message: {outcome['error'].get('reason')}",
                                detail=detail)
        return ActionResult(ok=True, detail=detail)
