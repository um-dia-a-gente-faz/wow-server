#!/usr/bin/env python3
"""Game actions — chat/social, target, attack for the agent.

CMSG_MESSAGECHAT's wire format and the ChatMsg/Language enums are verified
against TrinityCore branch `3.3.5`:
  src/server/game/Server/Packets/ChatPackets.cpp (ChatMessage::Read)
  src/server/game/Handlers/ChatHandler.cpp (WorldSession::HandleChatMessage)
  src/server/shared/SharedDefines.h (enum ChatMsg, LANG_* constants)
  src/server/game/Server/Packets/PartyPackets.cpp (PartyInviteClient::Read)
  src/server/game/Server/Protocol/Opcodes.h
  src/server/game/Server/Packets/CombatPackets.cpp (AttackSwing::Read)
  src/server/game/Server/Packets/MiscPackets.cpp (SetSelection::Read)

UM-36 adds the Action framework (Action/ActionResult/REGISTRY/catalog()) at
the top of this file — the uniform shape UM-44's LLM loop exposes as tools —
and the first two real actions, `set_target` and `face`. The free functions
below (say/yell/.../send_attack) predate it and stay as-is; set_target and
send_attack now also have Action wrappers registered in REGISTRY.
"""

import math
import struct
import time
from dataclasses import dataclass, field

from . import movement

CMSG_MESSAGECHAT        = 0x095   # chat say/yell/whisper/emote
CMSG_TEXT_EMOTE         = 0x104
CMSG_GROUP_INVITE       = 0x06E
CMSG_GROUP_ACCEPT       = 0x072
CMSG_GROUP_DISBAND      = 0x07B
CMSG_SET_SELECTION      = 0x13D  # target a GUID
CMSG_STAND_STATE_CHANGE = 0x101
CMSG_ATTACKSWING        = 0x141
CMSG_ATTACKSTOP         = 0x142

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

# ── Action framework (UM-36) ─────────────────────────────────────────────
# The uniform shape every action follows, so UM-44's LLM loop can expose
# REGISTRY as a tool catalog without bespoke per-action glue.

DEFAULT_CONFIRM_TIMEOUT_S = 2.0
DEFAULT_CONFIRM_POLL_S = 0.1


@dataclass
class ActionResult:
    ok: bool
    error: str | None = None
    detail: dict = field(default_factory=dict)


def _wait_for(predicate, timeout: float = DEFAULT_CONFIRM_TIMEOUT_S,
              interval: float = DEFAULT_CONFIRM_POLL_S) -> bool:
    """Poll `predicate` (called with no args) until it's true or `timeout`
    seconds pass. Used by an action's execute() to confirm its effect landed
    in perception (e.g. the self target field, UNIT_FIELD_TARGET) instead of
    just trusting the packet was sent. Blocks the calling thread — callers
    run this from the think/act step, not the recv thread."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


class Action:
    """Base for every agent action. Subclasses set `name`/`description`/
    `params` (JSON-schema `properties`, OpenAI/Anthropic-tool-compatible)
    and implement `check`/`execute`; `run` is what callers use."""

    name: str = ""
    description: str = ""
    params: dict = {}
    required: tuple = ()  # subset of params.keys() the schema marks required

    def check(self, session, world, **params) -> str | None:
        """Return an error string if this action shouldn't execute right
        now, else None. Called by run() before execute() — execute()
        implementations can assume check() already passed."""
        return None

    def execute(self, session, world, **params) -> ActionResult:
        raise NotImplementedError

    def run(self, session, world, **params) -> ActionResult:
        error = self.check(session, world, **params)
        if error is not None:
            return ActionResult(ok=False, error=error)
        return self.execute(session, world, **params)

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": self.params,
                "required": list(self.required),
            },
        }


REGISTRY: dict[str, Action] = {}


def register(action_cls: type) -> type:
    """Class decorator: instantiates `action_cls` and adds it to REGISTRY
    under its `.name`. Actions are stateless aside from per-instance
    confirm_timeout/confirm_interval overrides, so one shared instance is
    fine — tests that need a different timeout construct their own
    instance directly instead of going through REGISTRY."""
    instance = action_cls()
    REGISTRY[instance.name] = instance
    return action_cls


def catalog() -> list[dict]:
    """Every registered action's schema, in registration order — the tool
    list UM-44's LLM loop passes to the model."""
    return [a.schema() for a in REGISTRY.values()]


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
    race = getattr(session, "race", 0)
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
    session._send_packet(CMSG_MESSAGECHAT, payload)


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
    session._send_packet(CMSG_TEXT_EMOTE, payload)


def invite_to_group(session, name: str):
    # PartyInviteClient::Read (PartyPackets.cpp): cstring TargetName, uint32 ProposedRoles.
    payload = name.encode("utf-8") + b"\x00" + struct.pack("<I", 0)
    session._send_packet(CMSG_GROUP_INVITE, payload)


def accept_group(session):
    # HandleGroupAcceptOpcode (GroupHandler.cpp) only read_skip<uint32>()s the payload.
    session._send_packet(CMSG_GROUP_ACCEPT, struct.pack("<I", 0))
    session.pending_invite = None


def leave_group(session):
    # HandleGroupDisbandOpcode (GroupHandler.cpp) takes an unused WorldPacket — no payload.
    session._send_packet(CMSG_GROUP_DISBAND)


def send_target(session, guid: int):
    """Target a unit or object by GUID. SetSelection::Read (MiscPackets.cpp):
    a single raw (not packed) uint64 guid."""
    session._send_packet(CMSG_SET_SELECTION, struct.pack("<Q", guid))


def send_attack(session, guid: int):
    """Start auto-attack on target. AttackSwing::Read (CombatPackets.cpp):
    a single raw (not packed) uint64 victim guid — previously sent with no
    payload at all (a malformed packet; found in review, fixed by UM-36)."""
    send_target(session, guid)
    session._send_packet(CMSG_ATTACKSWING, struct.pack("<Q", guid))


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


# ── Registered actions (UM-36) ────────────────────────────────────────────

@register
class SetTargetAction(Action):
    name = "set_target"
    description = "Target a nearby unit, player, or object by its GUID from the perception snapshot."
    params = {
        "guid": {"type": "integer", "description": "GUID of the object to target."},
    }
    required = ("guid",)
    # Overridable (class or instance attribute) so tests don't have to block
    # for the production default — real confirmation normally lands within
    # one or two update-object ticks.
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, guid: int, **_) -> str | None:
        if world.get_object(guid) is None:
            return f"guid {guid:#x} is not currently perceived"
        return None

    def execute(self, session, world, guid: int, **_) -> ActionResult:
        send_target(session, guid)
        confirmed = _wait_for(lambda: (me := world.get_my_object()) is not None and me.target_guid == guid,
                               timeout=self.confirm_timeout, interval=self.confirm_interval)
        if not confirmed:
            return ActionResult(ok=False, error="target field did not update in time",
                                 detail={"guid": guid})
        return ActionResult(ok=True, detail={"guid": guid})


@register
class FaceAction(Action):
    name = "face"
    description = ("Turn in place to face a nearby object by GUID, or a specific x,y position, "
                    "without otherwise moving.")
    params = {
        "guid": {"type": "integer", "description": "GUID of the object to face. Mutually exclusive with x/y."},
        "x": {"type": "number", "description": "Target X coordinate. Requires y; mutually exclusive with guid."},
        "y": {"type": "number", "description": "Target Y coordinate. Requires x; mutually exclusive with guid."},
    }
    required = ()  # exactly one of guid or (x and y) — enforced in check(), not expressible as a flat "required" list

    def check(self, session, world, guid: int | None = None, x: float | None = None,
              y: float | None = None, **_) -> str | None:
        if guid is None and (x is None or y is None):
            return "face needs either guid or both x and y"
        if guid is not None and (x is not None or y is not None):
            return "face takes either guid or x/y, not both"
        if session.player_position is None:
            return "own position unknown"
        if guid is not None:
            target = world.get_object(guid)
            if target is None or target.position is None:
                return f"guid {guid:#x} has no known position"
        return None

    def execute(self, session, world, guid: int | None = None, x: float | None = None,
                y: float | None = None, **_) -> ActionResult:
        _, my_x, my_y, my_z, _my_o = session.player_position
        if guid is not None:
            target = world.get_object(guid)
            _, tx, ty, _tz, _ = target.position
        else:
            tx, ty = x, y
        orientation = math.atan2(ty - my_y, tx - my_x) % (2 * math.pi)

        movement.send_set_facing(session, my_x, my_y, my_z, orientation)
        # The server doesn't echo MSG_MOVE_* back to the sender, so mirror
        # the new orientation locally — matches how session.py mirrors self
        # position/stats from perception for cheap access (_sync_self_from_block).
        map_id = session.player_position[0]
        session.player_position = (map_id, my_x, my_y, my_z, orientation)

        return ActionResult(ok=True, detail={"orientation": orientation})


@register
class MoveToAction(Action):
    name = "move_to"
    description = ("Walk in a straight line to a point on the ground, stopping within "
                    "stop_distance yards. No pathfinding — obstacles will block it (returns "
                    "ok=False, error='stuck'). Blocks until arrival, stuck, or stopped.")
    params = {
        "x": {"type": "number", "description": "Destination X coordinate."},
        "y": {"type": "number", "description": "Destination Y coordinate."},
        "z": {"type": "number", "description": "Destination Z coordinate. Optional — "
                                                 "omit to stay level with the current height (no terrain data in v1)."},
        "stop_distance": {"type": "number", "description": "How close counts as arrived, in yards. Default 1.0."},
    }
    required = ("x", "y")

    def check(self, session, world, x: float, y: float, z: float | None = None,
              stop_distance: float = movement.ARRIVE_STOP_DISTANCE_YD, **_) -> str | None:
        if session.player_position is None:
            return "own position unknown"
        return None

    def execute(self, session, world, x: float, y: float, z: float | None = None,
                stop_distance: float = movement.ARRIVE_STOP_DISTANCE_YD, **_) -> ActionResult:
        mover = movement.get_mover(session, world)
        result = mover.move_to(x, y, z, stop_distance=stop_distance)
        return ActionResult(**result)


@register
class MoveTowardsAction(Action):
    name = "move_towards"
    description = ("Chase a nearby unit or player by GUID, re-targeting its position every "
                    "tick, stopping within stop_distance yards. Blocks until arrival, stuck, "
                    "the target leaving perception, or stopped.")
    params = {
        "guid": {"type": "integer", "description": "GUID of the object to move towards."},
        "stop_distance": {"type": "number", "description": "How close counts as arrived, in yards. Default 1.0."},
    }
    required = ("guid",)

    def check(self, session, world, guid: int,
              stop_distance: float = movement.ARRIVE_STOP_DISTANCE_YD, **_) -> str | None:
        if session.player_position is None:
            return "own position unknown"
        target = world.get_object(guid)
        if target is None or target.position is None:
            return f"guid {guid:#x} has no known position"
        return None

    def execute(self, session, world, guid: int,
                stop_distance: float = movement.ARRIVE_STOP_DISTANCE_YD, **_) -> ActionResult:
        mover = movement.get_mover(session, world)
        result = mover.move_towards(guid, stop_distance=stop_distance)
        return ActionResult(**result)


@register
class StopMovementAction(Action):
    name = "stop_movement"
    description = "Stop any in-progress move_to/move_towards immediately."
    params = {}
    required = ()

    def execute(self, session, world, **_) -> ActionResult:
        mover = movement.get_mover(session, world)
        was_moving = mover.stop()
        return ActionResult(ok=True, detail={"was_moving": was_moving})
