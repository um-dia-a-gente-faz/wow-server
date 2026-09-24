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

agent.reflexes.follow (UM-58) registers three more actions (`follow`,
`assist`, `stop_following`) into the same REGISTRY, and this module's
move_to/move_towards/stop_movement pause that reflex before running (see
_pause_follow_reflex) since an explicit LLM move outranks it.
"""

import math
import struct
import time
from dataclasses import dataclass, field

from . import movement
from . import npc
from . import quests as qu
from . import spells
from . import loot as lootmod
from . import item_compare
from . import mail as mailmod
from . import trade as tr
from . import update_fields as uf

CMSG_MESSAGECHAT        = 0x095   # chat say/yell/whisper/emote
CMSG_TEXT_EMOTE         = 0x104
CMSG_GROUP_INVITE       = 0x06E
CMSG_GROUP_ACCEPT       = 0x072
CMSG_GROUP_DISBAND      = 0x07B
CMSG_SET_SELECTION      = 0x13D  # target a GUID
CMSG_STAND_STATE_CHANGE = 0x101
CMSG_ATTACKSWING        = 0x141
CMSG_ATTACKSTOP         = 0x142
CMSG_CAST_SPELL         = 0x12E

MELEE_RANGE_YD = 5.0  # ~ melee weapon range + average combat reach


def _record_event(session, kind: str, **fields):
    """Same shape as WoWSession._record_event — duplicated here (matching
    agent.reflexes.follow/rest's own copy of this) instead of calling
    session._record_event() directly, so an action that records its own
    event (send_mail, UM-60) also works against the plain `events: list`
    fake sessions this module's tests use, which have no _record_event."""
    events = getattr(session, "events", None)
    if events is None:
        return
    events.append({"kind": kind, "t": time.monotonic(), **fields})


def _pause_follow_reflex(session, world):
    """Pause the follow reflex (agent.reflexes.follow, UM-58) before an
    explicit movement action runs — an LLM-picked move outranks the reflex
    (docs/AI-AGENT-SPEC.md's reflex priority: survival > follow/assist >
    idle), so following pauses and reports why instead of fighting the next
    move command. Imported lazily to avoid a circular import: the
    follow-reflex module imports Action/ActionResult/register from this
    one. A no-op if agent.reflexes.follow hasn't been imported yet (nothing
    is following) or nothing is currently following."""
    from .reflexes import follow as _follow
    _follow.pause_for_llm_override(session, world)


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


def _wait_for_value(get_value, timeout: float = DEFAULT_CONFIRM_TIMEOUT_S,
                     interval: float = DEFAULT_CONFIRM_POLL_S):
    """Like _wait_for, but for confirmations that need to return *which*
    event matched (e.g. a cast succeeding vs. failing) — polls `get_value()`
    until it returns something other than None, or `timeout` elapses (then
    returns None)."""
    deadline = time.monotonic() + timeout
    while True:
        value = get_value()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            return None
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
    description = "Target a nearby unit, player, or object by its handle from the perception snapshot."
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the object to target."},
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
    description = ("Turn in place to face a nearby object by handle, or a specific x,y position, "
                    "without otherwise moving.")
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the object to face. Mutually exclusive with x/y."},
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
        _pause_follow_reflex(session, world)
        mover = movement.get_mover(session, world)
        result = mover.move_to(x, y, z, stop_distance=stop_distance)
        return ActionResult(**result)


@register
class MoveTowardsAction(Action):
    name = "move_towards"
    description = ("Chase a nearby unit or player by handle, re-targeting its position every "
                    "tick, stopping within stop_distance yards. Blocks until arrival, stuck, "
                    "the target leaving perception, or stopped.")
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the object to move towards."},
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
        _pause_follow_reflex(session, world)
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
        _pause_follow_reflex(session, world)
        mover = movement.get_mover(session, world)
        was_moving = mover.stop()
        return ActionResult(ok=True, detail={"was_moving": was_moving})


@register
class AutoAttackAction(Action):
    name = "auto_attack"
    description = "Target and start melee auto-attack on a nearby hostile unit."
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the unit to attack."},
    }
    required = ("guid",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, guid: int, **_) -> str | None:
        target = world.get_object(guid)
        if target is None:
            return f"guid {guid:#x} is not currently perceived"
        if target.is_dead():
            return f"guid {guid:#x} is already dead"
        me = world.get_my_object()
        if me is not None and target.is_hostile_to(me.faction) is False:
            return f"guid {guid:#x} is not hostile"
        if session.player_position is None:
            return "own position unknown"
        distance = target.distance_to(session.player_position)
        if distance is not None and distance > MELEE_RANGE_YD:
            return (f"guid {guid:#x} is {distance:.1f} yd away, out of melee range "
                    f"({MELEE_RANGE_YD} yd) — try move_towards first")
        return None

    def execute(self, session, world, guid: int, **_) -> ActionResult:
        sent_at = time.monotonic()
        send_target(session, guid)
        send_attack(session, guid)
        confirmed = _wait_for(
            lambda: any(e.get("kind") == "attack_start" and e.get("victim_guid") == guid
                        and e.get("t", 0) >= sent_at for e in session.events),
            timeout=self.confirm_timeout, interval=self.confirm_interval)
        if not confirmed:
            return ActionResult(ok=False, error="no attack_start event seen", detail={"guid": guid})
        return ActionResult(ok=True, detail={"guid": guid})


@register
class StopAttackAction(Action):
    name = "stop_attack"
    description = "Stop melee auto-attack."
    params = {}
    required = ()

    def execute(self, session, world, **_) -> ActionResult:
        session._send_packet(CMSG_ATTACKSTOP)
        return ActionResult(ok=True)


@register
class CastSpellAction(Action):
    name = "cast_spell"
    description = "Cast a known spell, optionally on a target (defaults to self if target_guid is omitted)."
    params = {
        "spell_id": {"type": "integer", "description": "Spell ID to cast — must be in the agent's spellbook."},
        "target_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the unit to cast on. Omit to cast on self."},
    }
    required = ("spell_id",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, spell_id: int, target_guid: int | None = None, **_) -> str | None:
        if spell_id not in session.spellbook:
            return f"spell {spell_id} is not known"
        cooldown = session.spell_cooldowns.get(spell_id)
        if cooldown and cooldown.get("recovery_time", 0) > 0:
            return f"spell {spell_id} is on cooldown"
        info = spells.get_spell_info(spell_id)
        if info is not None and info.power_cost:
            me = world.get_my_object()
            power_name = uf.POWER_NAMES[info.power_type] if 0 <= info.power_type < len(uf.POWER_NAMES) else None
            have = me.power.get(power_name) if me is not None and power_name is not None else None
            if have is not None and have < info.power_cost:
                return (f"not enough power for spell {spell_id} "
                        f"(need {info.power_cost}, have {have})")
        if target_guid is not None and world.get_object(target_guid) is None:
            return f"guid {target_guid:#x} is not currently perceived"
        return None

    def execute(self, session, world, spell_id: int, target_guid: int | None = None, **_) -> ActionResult:
        if target_guid is not None:
            target = world.get_object(target_guid)
            if target is not None and target.position is not None and session.player_position is not None:
                FaceAction().execute(session, world, guid=target_guid)

        sent_at = time.monotonic()
        session._send_packet(CMSG_CAST_SPELL, spells.build_cast_spell(spell_id, target_guid=target_guid))

        def find_start_or_failure():
            for e in session.events:
                if e.get("t", 0) < sent_at or e.get("spell_id") != spell_id:
                    continue
                if e.get("kind") in ("cast_failed", "spell_start", "spell_go"):
                    return e
            return None

        # The server always answers with SMSG_SPELL_START right away (even
        # for instant casts — TrinityCore's Spell::prepare sends it before
        # checking cast time), then SMSG_SPELL_GO only after the spell's own
        # cast_time (ms) elapses. A fixed confirm_timeout would time out on
        # any cast_time above ~1.9s (e.g. Holy Light's 2.5s) even on success
        # — found live testing against a training dummy — so the wait for
        # the real outcome is extended by the cast time this spell reports.
        event = _wait_for_value(find_start_or_failure, timeout=self.confirm_timeout,
                                 interval=self.confirm_interval)
        if event is None:
            return ActionResult(ok=False, error="no cast confirmation seen (timed out)",
                                 detail={"spell_id": spell_id})
        if event["kind"] in ("cast_failed", "spell_go"):
            if event["kind"] == "cast_failed":
                return ActionResult(ok=False, error=event["reason_name"], detail=event)
            return ActionResult(ok=True, detail=event)

        # event["kind"] == "spell_start": cast is in flight — wait out its
        # reported cast time (plus the normal confirm margin) for the
        # actual outcome.
        started_at = event["t"]
        cast_time_s = event.get("cast_time", 0) / 1000.0

        def find_outcome():
            for e in session.events:
                if e.get("t", 0) <= started_at or e.get("spell_id") != spell_id:
                    continue
                if e.get("kind") in ("cast_failed", "spell_go"):
                    return e
            return None

        outcome = _wait_for_value(find_outcome, timeout=cast_time_s + self.confirm_timeout,
                                   interval=self.confirm_interval)
        if outcome is None:
            return ActionResult(ok=False, error="no cast confirmation seen (timed out)",
                                 detail={"spell_id": spell_id, "cast_time_ms": event.get("cast_time")})
        if outcome["kind"] == "cast_failed":
            return ActionResult(ok=False, error=outcome["reason_name"], detail=outcome)
        return ActionResult(ok=True, detail=outcome)


# ── NPC interaction (UM-40) ───────────────────────────────────────────────
# Depends on UM-36 (this Action framework) and UM-35 (name cache, which
# feeds npc_flags detection via agent/perception.py — see ObjectInfo.
# is_gossip/is_vendor/is_trainer/is_quest_giver). Opcodes/layouts live in
# agent/npc.py; response parsing lands in agent.perception.WorldState's
# ui_state, exposed to the LLM as snapshot()'s 'window' key.

@register
class InteractAction(Action):
    name = "interact"
    description = ("Interact with a nearby NPC or gameobject by handle: opens its gossip, "
                    "vendor, or trainer window (whichever its flags indicate), or activates "
                    "it directly if it's a gameobject. Must be within "
                    f"{npc.INTERACT_RANGE_YD} yd — use move_towards first if not.")
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the NPC or gameobject to interact with."},
    }
    required = ("guid",)

    def check(self, session, world, guid: int, **_) -> str | None:
        target = world.get_object(guid)
        if target is None:
            return f"guid {guid:#x} is not currently perceived"
        if session.player_position is None:
            return "own position unknown"
        distance = target.distance_to(session.player_position)
        if distance is not None and distance > npc.INTERACT_RANGE_YD:
            return (f"guid {guid:#x} is {distance:.1f} yd away, out of interact range "
                    f"({npc.INTERACT_RANGE_YD} yd) — try move_towards first")
        return None

    def execute(self, session, world, guid: int, **_) -> ActionResult:
        target = world.get_object(guid)

        if target.object_type == "gameobject":
            session._send_packet(npc.CMSG_GAMEOBJ_USE, npc.build_gameobj_use(guid))
            return ActionResult(ok=True, detail={"guid": guid, "kind": "gameobject_use"})

        # Gossip takes priority over a bare questgiver hello — real NPCs
        # with vendor/trainer/quest flags almost always also have gossip
        # and reach their quest/vendor/trainer window *through* the gossip
        # menu (agent.npc.parse_gossip_message already carries the offered
        # quests list); only a questgiver with no gossip flag needs the
        # dedicated CMSG_QUESTGIVER_HELLO flow (UM-41).
        if target.is_gossip():
            session._send_packet(npc.CMSG_GOSSIP_HELLO, npc.build_gossip_hello(guid))
            return ActionResult(ok=True, detail={"guid": guid, "kind": "gossip_hello"})
        if target.is_quest_giver():
            session._send_packet(qu.CMSG_QUESTGIVER_HELLO, qu.build_questgiver_hello(guid))
            return ActionResult(ok=True, detail={"guid": guid, "kind": "questgiver_hello"})
        if target.is_vendor():
            session._send_packet(npc.CMSG_LIST_INVENTORY, npc.build_list_inventory(guid))
            return ActionResult(ok=True, detail={"guid": guid, "kind": "list_inventory"})
        if target.is_trainer():
            session._send_packet(npc.CMSG_TRAINER_LIST, npc.build_trainer_list(guid))
            return ActionResult(ok=True, detail={"guid": guid, "kind": "trainer_list"})
        if target.is_lootable():
            return ActionResult(ok=False, error="lootable corpses are out of scope for interact() (see UM-42)")
        return ActionResult(ok=False, error="no known interaction for this target "
                                             "(no gossip/vendor/trainer npc flag, not a gameobject)")


@register
class GossipSelectAction(Action):
    name = "gossip_select"
    description = "Select an option in the currently open gossip window by its index."
    params = {
        "option_index": {"type": "integer", "description": "The option's `index` from the open gossip window."},
        "code": {"type": "string", "description": "Text for options that require a text-entry box (rare; "
                                                    "the window's option marks `coded: true` when needed)."},
    }
    required = ("option_index",)

    def check(self, session, world, option_index: int, code: str | None = None, **_) -> str | None:
        window = world.get_ui_state()
        if window is None or window.get("kind") != "gossip":
            return "no gossip window is open"
        if not any(o["index"] == option_index for o in window.get("options", [])):
            return f"option_index {option_index} is not present in the open gossip menu"
        return None

    def execute(self, session, world, option_index: int, code: str | None = None, **_) -> ActionResult:
        window = world.get_ui_state()
        guid, menu_id = window["npc_guid"], window["menu_id"]
        session._send_packet(npc.CMSG_GOSSIP_SELECT_OPTION,
                              npc.build_gossip_select_option(guid, menu_id, option_index, code=code))
        return ActionResult(ok=True, detail={"guid": guid, "menu_id": menu_id, "option_index": option_index})


def _find_vendor_item(window: dict, slot: int | None, entry: int | None) -> dict | None:
    for item in window.get("items", []):
        if slot is not None and item["slot"] == slot:
            return item
        if slot is None and entry is not None and item["entry"] == entry:
            return item
    return None


@register
class BuyItemAction(Action):
    name = "buy_item"
    description = ("Buy an item from the vendor window currently open for vendor_guid, "
                    "identified by vendor slot or item entry (from the window's items list). "
                    "Waits for the server's confirmation/failure response before returning, so "
                    "ok=True means the purchase actually went through.")
    params = {
        "vendor_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the open vendor."},
        "slot": {"type": "integer", "description": "Vendor slot from the open window's items list."},
        "entry": {"type": "integer", "description": "Item entry, as an alternative to slot."},
        "count": {"type": "integer", "description": "How many to buy. Default 1."},
    }
    required = ("vendor_guid",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, vendor_guid: int, slot: int | None = None,
              entry: int | None = None, count: int = 1, **_) -> str | None:
        if slot is None and entry is None:
            return "buy_item requires slot or entry"
        window = world.get_ui_state()
        if window is None or window.get("kind") != "vendor" or window.get("vendor_guid") != vendor_guid:
            return "no vendor window is open for that guid"
        if _find_vendor_item(window, slot, entry) is None:
            return "item not found in the open vendor window (by slot or entry)"
        return None

    def execute(self, session, world, vendor_guid: int, slot: int | None = None,
                entry: int | None = None, count: int = 1, **_) -> ActionResult:
        window = world.get_ui_state()
        item = _find_vendor_item(window, slot, entry)
        sent_at = time.monotonic()
        session._send_packet(npc.CMSG_BUY_ITEM,
                              npc.build_buy_item(vendor_guid, item["entry"], item["slot"], count))

        # SMSG_BUY_ITEM (personal ack on success, via Player::BuyItemFromVendorSlot
        # -> SendDirectMessage) and SMSG_BUY_FAILED (personal ack on failure, via
        # Player::SendBuyError) are both sent directly to us — mirror
        # CastSpellAction's SMSG_CAST_FAILED pattern and wait for whichever
        # arrives first, tied to this vendor/item.
        def find_outcome():
            for e in session.events:
                if e.get("t", 0) < sent_at or e.get("vendor_guid") != vendor_guid:
                    continue
                if e.get("kind") == "buy_item" and e.get("slot") == item["slot"]:
                    return e
                if e.get("kind") == "buy_failed" and e.get("item_entry") == item["entry"]:
                    return e
            return None

        outcome = _wait_for_value(find_outcome, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"vendor_guid": vendor_guid, "slot": item["slot"], "entry": item["entry"], "count": count}
        if outcome is None:
            detail["error"] = "no buy confirmation seen (timed out)"
            return ActionResult(ok=False, error="no buy confirmation seen (timed out)", detail=detail)
        detail["outcome"] = outcome
        if outcome["kind"] == "buy_failed":
            return ActionResult(ok=False, error=outcome.get("reason_name", "buy failed"), detail=detail)
        return ActionResult(ok=True, detail=detail)


@register
class SellItemAction(Action):
    name = "sell_item"
    description = ("Sell an item at bag/slot to the vendor window currently open for "
                    "vendor_guid. Resolves the item's GUID from bag/slot (UM-42's inventory "
                    "model) and waits for the server's confirmation/failure response before "
                    "returning.")
    params = {
        "vendor_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the vendor to sell to."},
        "bag": {"type": "integer", "description": "Bag byte of the item (255 = equipped items/backpack)."},
        "slot": {"type": "integer", "description": "Inventory slot of the item."},
        "count": {"type": "integer", "description": "How many to sell from the stack. Default: whole stack."},
    }
    required = ("vendor_guid", "bag", "slot")
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, vendor_guid: int, bag: int, slot: int,
              count: int | None = None, **_) -> str | None:
        window = world.get_ui_state()
        if window is None or window.get("kind") != "vendor" or window.get("vendor_guid") != vendor_guid:
            return "no vendor window is open for that guid"
        if _find_item_guid(world, bag, slot) is None:
            return f"no known item at bag={bag} slot={slot}"
        return None

    def execute(self, session, world, vendor_guid: int, bag: int, slot: int,
                count: int | None = None, **_) -> ActionResult:
        item_guid = _find_item_guid(world, bag, slot)
        sent_at = time.monotonic()
        session._send_packet(npc.CMSG_SELL_ITEM,
                              npc.build_sell_item(vendor_guid, item_guid, count or 0))

        # Same fire-and-forget pattern as buy_item: only a failure carries an
        # explicit personal ack (SMSG_SELL_ITEM has no confirmation payload
        # worth waiting on), so wait briefly for sell_failed and otherwise
        # treat the timeout as success.
        def find_failure():
            for e in session.events:
                if (e.get("t", 0) >= sent_at and e.get("kind") == "sell_failed"
                        and e.get("vendor_guid") == vendor_guid and e.get("item_guid") == item_guid):
                    return e
            return None

        failure = _wait_for_value(find_failure, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"vendor_guid": vendor_guid, "bag": bag, "slot": slot, "item_guid": item_guid,
                   "count": count}
        if failure is not None:
            detail["failure"] = failure
            return ActionResult(ok=False, error=failure.get("reason_name", "sell failed"), detail=detail)
        return ActionResult(ok=True, detail=detail)


@register
class TrainSpellAction(Action):
    name = "train_spell"
    description = ("Learn a spell from the trainer window currently open for trainer_guid. "
                    "Waits for the server's confirmation/failure response before returning, so "
                    "ok=True means the spell was actually learned.")
    params = {
        "trainer_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the open trainer."},
        "spell_id": {"type": "integer", "description": "Spell ID from the open trainer window's spells list."},
    }
    required = ("trainer_guid", "spell_id")
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, trainer_guid: int, spell_id: int, **_) -> str | None:
        window = world.get_ui_state()
        if window is None or window.get("kind") != "trainer" or window.get("trainer_guid") != trainer_guid:
            return "no trainer window is open for that guid"
        if not any(s["spell_id"] == spell_id for s in window.get("spells", [])):
            return f"spell {spell_id} is not offered by the open trainer window"
        return None

    def execute(self, session, world, trainer_guid: int, spell_id: int, **_) -> ActionResult:
        sent_at = time.monotonic()
        session._send_packet(npc.CMSG_TRAINER_BUY_SPELL, npc.build_train_spell(trainer_guid, spell_id))

        def find_outcome():
            for e in session.events:
                if (e.get("t", 0) >= sent_at and e.get("kind") in ("train_succeeded", "train_failed")
                        and e.get("trainer_guid") == trainer_guid and e.get("spell_id") == spell_id):
                    return e
            return None

        outcome = _wait_for_value(find_outcome, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"trainer_guid": trainer_guid, "spell_id": spell_id}
        if outcome is None:
            return ActionResult(ok=False, error="no training confirmation seen (timed out)", detail=detail)
        detail["outcome"] = outcome
        if outcome["kind"] == "train_failed":
            return ActionResult(ok=False, error=outcome.get("reason_name", "training failed"), detail=detail)
        return ActionResult(ok=True, detail=detail)


# ── Chat / social (UM-68) ─────────────────────────────────────────────────
# Wraps the plain say/yell/whisper/emote/invite_to_group/accept_group
# functions above as Action subclasses so they show up in catalog() — until
# now they were only reachable by calling the free functions directly, so
# the LLM (agent/think.py) had no way to invoke them at all.

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
    for entry in getattr(session, "chat_inbox", ()):
        if entry.get("sender_name", "").lower() == lname:
            return True
    return False


@register
class SayAction(Action):
    name = "say"
    description = "Speak a chat message aloud (/say) — audible to nearby players."
    params = {
        "message": {"type": "string", "description": "The message to say."},
    }
    required = ("message",)

    def execute(self, session, world, message: str, **_) -> ActionResult:
        say(session, message)
        return ActionResult(ok=True, detail={"message": message})


@register
class YellAction(Action):
    name = "yell"
    description = "Shout a chat message (/yell) — audible over a much larger radius than say."
    params = {
        "message": {"type": "string", "description": "The message to yell."},
    }
    required = ("message",)

    def execute(self, session, world, message: str, **_) -> ActionResult:
        yell(session, message)
        return ActionResult(ok=True, detail={"message": message})


@register
class WhisperAction(Action):
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


@register
class EmoteAction(Action):
    name = "emote"
    description = "Send a free-text roleplay emote (/emote) — distinct from a predefined animated emote."
    params = {
        "text": {"type": "string", "description": "The emote text."},
    }
    required = ("text",)

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

    def execute(self, session, world, name: str, **_) -> ActionResult:
        sent_at = time.monotonic()
        invite_to_group(session, name)

        # HandleGroupInviteOpcode (GroupHandler.cpp) answers with
        # SMSG_PARTY_COMMAND_RESULT on both success and failure, but
        # session._handle_party_command_result only records an event for a
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
        if not getattr(session, "pending_invite", None):
            return "no pending group invite to accept"
        return None

    def execute(self, session, world, **_) -> ActionResult:
        invite = session.pending_invite
        accept_group(session)
        return ActionResult(ok=True, detail={"inviter_name": (invite or {}).get("inviter_name")})


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


# ── Loot / inventory (UM-42) ──────────────────────────────────────────────
#
# Wire layouts verified against TrinityCore branch `3.3.5`:
#   src/server/game/Handlers/LootHandler.cpp (CMSG_LOOT/_MONEY/_RELEASE,
#     CMSG_AUTOSTORE_LOOT_ITEM)
#   src/server/game/Server/Packets/LootPackets.h/.cpp (SMSG_LOOT_RESPONSE and
#     friends)
#   src/server/game/Entities/Player/Player.cpp (SendLoot/SendLootRelease/
#     SendLootError/SendNewItem — build several of these packets by hand)
#   src/server/game/Handlers/SpellHandler.cpp (HandleUseItemOpcode)
#   src/server/game/Server/Packets/ItemPackets.cpp (CMSG_DESTROYITEM)
# See agent/loot.py for the byte-level detail on each of these.

LOOT_RANGE_YD = 5.0
UNIT_FLAG_IN_COMBAT = 0x00080000  # UnitDefines.h — same bit perception.py's _object_dict uses


def _find_item_guid(world, bag: int, slot: int) -> int | None:
    """Resolve a (bag, slot) inventory position to the item GUID sitting
    there, from the self player's own INV_SLOT_HEAD/PACK_SLOT_1 fields.
    v1 only understands bag == INVENTORY_SLOT_BAG_0 (equipped items,
    equipped bag containers, and backpack contents) — an item inside a
    *non-backpack* bag isn't addressable yet (would need decoding that
    bag's own CONTAINER_FIELD_SLOT_1 array, not modeled in this card)."""
    if bag != uf.INVENTORY_SLOT_BAG_0:
        return None
    me = world.get_my_object()
    if me is None:
        return None
    return uf.decode_equipment_and_inventory_guids(me.raw_fields).get(slot)


@register
class LootAction(Action):
    name = "loot"
    description = ("Loot a nearby lootable corpse/creature (UNIT_DYNFLAG_LOOTABLE): opens the "
                    "loot window, takes all money and every item (v1: take-everything, no "
                    "selective looting), then releases it. Must be within 5 yards.")
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the lootable corpse/creature."},
    }
    required = ("guid",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, guid: int, **_) -> str | None:
        target = world.get_object(guid)
        if target is None:
            return f"guid {guid:#x} is not currently perceived"
        if not target.is_lootable():
            return f"guid {guid:#x} is not lootable"
        if session.player_position is None:
            return "own position unknown"
        distance = target.distance_to(session.player_position)
        if distance is not None and distance > LOOT_RANGE_YD:
            return (f"guid {guid:#x} is {distance:.1f} yd away, out of loot range "
                    f"({LOOT_RANGE_YD} yd) — try move_towards first")
        return None

    def execute(self, session, world, guid: int, **_) -> ActionResult:
        sent_at = time.monotonic()
        session._send_packet(lootmod.CMSG_LOOT, lootmod.build_loot(guid))

        def find_loot_response():
            for e in session.events:
                if e.get("t", 0) >= sent_at and e.get("kind") == "loot_response" and e.get("guid") == guid:
                    return e
            return None

        response = _wait_for_value(find_loot_response, timeout=self.confirm_timeout,
                                    interval=self.confirm_interval)
        if response is None:
            return ActionResult(ok=False, error="no loot response seen (timed out)", detail={"guid": guid})
        if not response.get("success"):
            return ActionResult(ok=False, error=response.get("failure_reason_name", "loot failed"),
                                 detail=response)

        coins = response.get("coins", 0)
        if coins:
            session._send_packet(lootmod.CMSG_LOOT_MONEY, lootmod.build_loot_money())

        looted_items = list(response.get("items", []))
        for item in looted_items:
            session._send_packet(lootmod.CMSG_AUTOSTORE_LOOT_ITEM,
                                  lootmod.build_autostore_loot_item(item["slot"]))

        def find_inventory_failure():
            for e in session.events:
                if e.get("t", 0) >= sent_at and e.get("kind") == "inventory_change_failure":
                    return e
            return None

        # A short, non-configurable wait: long enough for the server's
        # autostore replies to land, short enough not to meaningfully slow
        # every loot down when nothing goes wrong.
        failure = _wait_for_value(find_inventory_failure, timeout=0.3, interval=self.confirm_interval)

        session._send_packet(lootmod.CMSG_LOOT_RELEASE, lootmod.build_loot_release(guid))

        detail = {"guid": guid, "coins": coins, "items": looted_items}
        if failure is not None:
            detail["inventory_error"] = failure
            return ActionResult(ok=False, error="inventory_change_failure while storing loot", detail=detail)
        return ActionResult(ok=True, detail=detail)


@register
class UseItemAction(Action):
    name = "use_item"
    description = ("Use/consume an item from inventory — e.g. eat food or drink water. Not usable "
                    "while in combat (server-enforced for most consumables; checked here too).")
    params = {
        "bag": {"type": "integer", "description": "Bag byte (255 = equipped items/backpack; "
                                                    "see CMSG_USE_ITEM's bag field)."},
        "slot": {"type": "integer", "description": "Slot within that bag (0-18 equipment, "
                                                     "19-22 equipped bags, 23-38 backpack)."},
        "target_guid": {"type": "string", "description": "Optional target snapshot handle (a string like \"p1\"), e.g. a bandage "
                                                            "used on an ally. Omit to target self."},
    }
    required = ("bag", "slot")

    def check(self, session, world, bag: int, slot: int, target_guid: int | None = None, **_) -> str | None:
        me = world.get_my_object()
        if me is not None and me.unit_flags and (me.unit_flags & UNIT_FLAG_IN_COMBAT):
            return "cannot use items while in combat"
        if _find_item_guid(world, bag, slot) is None:
            return f"no known item at bag={bag} slot={slot}"
        return None

    def execute(self, session, world, bag: int, slot: int, target_guid: int | None = None, **_) -> ActionResult:
        item_guid = _find_item_guid(world, bag, slot)
        spell_id = 0
        item_obj = world.get_object(item_guid) if item_guid else None
        if item_obj is not None and item_obj.entry is not None:
            item_info = world.items.items.get(item_obj.entry)
            if item_info:
                for s in item_info.get("spells", []):
                    if s["trigger"] == lootmod.ITEM_SPELLTRIGGER_ON_USE:
                        spell_id = s["spell_id"]
                        break
        session._send_packet(
            lootmod.CMSG_USE_ITEM,
            lootmod.build_use_item(bag, slot, item_guid, spell_id=spell_id, target_guid=target_guid))
        return ActionResult(ok=True, detail={"bag": bag, "slot": slot, "item_guid": item_guid,
                                              "spell_id": spell_id})


@register
class DestroyItemAction(Action):
    name = "destroy_item"
    description = ("Permanently destroy `count` of the item at bag/slot. Irreversible — requires "
                    "confirm=true as an explicit safety guard against accidental calls.")
    params = {
        "bag": {"type": "integer", "description": "Bag byte (255 = equipped items/backpack)."},
        "slot": {"type": "integer", "description": "Slot within that bag."},
        "count": {"type": "integer", "description": "How many to destroy from the stack."},
        "confirm": {"type": "boolean", "description": "Must be true — a safety guard, not read by "
                                                        "the server."},
    }
    required = ("bag", "slot", "count", "confirm")

    def check(self, session, world, bag: int, slot: int, count: int, confirm: bool = False,
              **_) -> str | None:
        if not confirm:
            return "destroy_item requires confirm=true"
        if count <= 0:
            return "count must be positive"
        return None

    def execute(self, session, world, bag: int, slot: int, count: int, confirm: bool = False,
                **_) -> ActionResult:
        session._send_packet(lootmod.CMSG_DESTROYITEM, lootmod.build_destroy_item(bag, slot, count))
        return ActionResult(ok=True, detail={"bag": bag, "slot": slot, "count": count})


# ── Item comparison / equip (UM-69) ───────────────────────────────────────
#
# Wire layout for CMSG_AUTOEQUIP_ITEM verified against TrinityCore branch
# `3.3.5` (src/server/game/Handlers/ItemHandler.cpp,
# HandleAutoEquipItemOpcode) — see agent/loot.py's build_autoequip_item for
# the byte-level detail and its opcode-value caveat (not independently
# confirmed against a live server in this sandbox, no network access here).
# Scoring/usability heuristics live in agent/item_compare.py (v1: primary
# stat for class + item level tiebreak, no talent/spec awareness).


def _resolve_item_template(world, bag: int, slot: int):
    """(item_guid, entry, template_dict) for the item at bag/slot, or
    (None, None, None) if there's no known item there. If the item's guid/
    entry are known but its template (name/stats/armor/...) hasn't come
    back from CMSG_ITEM_QUERY_SINGLE yet, returns (guid, entry, None) —
    also requests the query (world.items.want_item) so it'll be available
    on a later call. Callers treat a None template as "not enough
    information yet", never as a crash."""
    item_guid = _find_item_guid(world, bag, slot)
    if item_guid is None:
        return None, None, None
    item_obj = world.get_object(item_guid)
    if item_obj is None or item_obj.entry is None:
        return item_guid, None, None
    world.items.want_item(item_obj.entry)
    template = world.items.items.get(item_obj.entry)
    return item_guid, item_obj.entry, template


@register
class CompareItemsAction(Action):
    name = "compare_items"
    description = ("Compare two items at given bag/slot positions for the character's own class "
                    "(UM-69 v1 heuristic: primary stat for class + item level as a tiebreaker, no "
                    "talent/spec awareness — see agent.item_compare's docstring). Returns which "
                    "item scores higher and a human-readable reason.")
    params = {
        "bag_a": {"type": "integer", "description": "Bag byte of the first item (255 = equipped items/backpack)."},
        "slot_a": {"type": "integer", "description": "Inventory slot of the first item."},
        "bag_b": {"type": "integer", "description": "Bag byte of the second item (255 = equipped items/backpack)."},
        "slot_b": {"type": "integer", "description": "Inventory slot of the second item."},
    }
    required = ("bag_a", "slot_a", "bag_b", "slot_b")

    def check(self, session, world, bag_a: int, slot_a: int, bag_b: int, slot_b: int, **_) -> str | None:
        _, _, template_a = _resolve_item_template(world, bag_a, slot_a)
        if template_a is None:
            return f"no known item template at bag={bag_a} slot={slot_a} (not queried yet, or empty slot)"
        _, _, template_b = _resolve_item_template(world, bag_b, slot_b)
        if template_b is None:
            return f"no known item template at bag={bag_b} slot={slot_b} (not queried yet, or empty slot)"
        if template_a.get("inventory_type") != template_b.get("inventory_type"):
            return (f"items are for different equip slots (inventory_type "
                    f"{template_a.get('inventory_type')} vs {template_b.get('inventory_type')})")
        return None

    def execute(self, session, world, bag_a: int, slot_a: int, bag_b: int, slot_b: int, **_) -> ActionResult:
        _, entry_a, template_a = _resolve_item_template(world, bag_a, slot_a)
        _, entry_b, template_b = _resolve_item_template(world, bag_b, slot_b)
        class_id = getattr(session, "class_", 0)
        result = item_compare.compare(template_a, template_b, class_id)
        detail = {
            "entry_a": entry_a, "entry_b": entry_b,
            "usability_error_a": item_compare.usability_error(template_a, class_id),
            "usability_error_b": item_compare.usability_error(template_b, class_id),
            **result,
        }
        return ActionResult(ok=True, detail=detail)


@register
class EquipItemAction(Action):
    name = "equip_item"
    description = ("Equip the item at bag/slot (CMSG_AUTOEQUIP_ITEM) — the server picks the equip "
                    "slot from the item itself. Refuses up front if the item's cached template says "
                    "it can't be used by this character (wrong armor type, class-restricted); "
                    "otherwise waits for the server's confirmation/failure response before "
                    "returning, so ok=True means the item was actually equipped.")
    params = {
        "bag": {"type": "integer", "description": "Bag byte of the item (255 = equipped items/backpack)."},
        "slot": {"type": "integer", "description": "Inventory slot of the item."},
    }
    required = ("bag", "slot")
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, bag: int, slot: int, **_) -> str | None:
        item_guid, _, template = _resolve_item_template(world, bag, slot)
        if item_guid is None:
            return f"no known item at bag={bag} slot={slot}"
        if template is not None:
            error = item_compare.usability_error(template, getattr(session, "class_", 0))
            if error is not None:
                return error
        return None

    def execute(self, session, world, bag: int, slot: int, **_) -> ActionResult:
        item_guid, entry, _ = _resolve_item_template(world, bag, slot)
        sent_at = time.monotonic()
        session._send_packet(lootmod.CMSG_AUTOEQUIP_ITEM, lootmod.build_autoequip_item(bag, slot))

        def find_failure():
            for e in session.events:
                if e.get("t", 0) >= sent_at and e.get("kind") == "inventory_change_failure":
                    return e
            return None

        failure = _wait_for_value(find_failure, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"bag": bag, "slot": slot, "item_guid": item_guid, "entry": entry}
        if failure is not None:
            detail["failure"] = failure
            return ActionResult(ok=False, error=failure.get("reason_name", "equip failed"), detail=detail)
        return ActionResult(ok=True, detail=detail)
# ── Quests (UM-41) ─────────────────────────────────────────────────────────
# Wire layouts + citations/caveats live in agent/quests.py — none of this
# module's byte layouts have been live-verified against a real server (see
# the module docstring there); everything here is still wrapped by
# session.py's usual per-packet try/except-and-drop.

QUEST_INTERACT_RANGE_YD = qu.INTERACT_RANGE_YD


def _open_quest_giver_window(session, world, npc_guid: int, quest_id: int, kinds: tuple):
    window = world.get_ui_state()
    if window is None or window.get("kind") not in kinds:
        return None
    if window.get("npc_guid") != npc_guid:
        return None
    if window.get("quest_id") is not None and window.get("quest_id") != quest_id:
        return None
    return window


@register
class AcceptQuestAction(Action):
    name = "accept_quest"
    description = ("Accept a quest offered by a nearby questgiver. Requires that NPC's quest "
                    "list/quest details window, or a gossip window listing the quest, to "
                    "already be open (interact() with the NPC first) and the quest to be "
                    "present in it.")
    params = {
        "npc_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the questgiver NPC."},
        "quest_id": {"type": "integer", "description": "Quest ID to accept, from the open "
                                                         "quest_list/quest_details/gossip "
                                                         "window."},
    }
    required = ("npc_guid", "quest_id")

    def check(self, session, world, npc_guid: int, quest_id: int, **_) -> str | None:
        target = world.get_object(npc_guid)
        if target is None:
            return f"guid {npc_guid:#x} is not currently perceived"
        if session.player_position is not None:
            distance = target.distance_to(session.player_position)
            if distance is not None and distance > QUEST_INTERACT_RANGE_YD:
                return (f"guid {npc_guid:#x} is {distance:.1f} yd away, out of interact range "
                        f"({QUEST_INTERACT_RANGE_YD} yd) — try move_towards first")
        window = world.get_ui_state()
        if window is None or window.get("kind") not in ("quest_list", "quest_details", "gossip"):
            return "no quest list/details/gossip window is open for that NPC — interact() with it first"
        if window.get("npc_guid") != npc_guid:
            return "the open quest window belongs to a different NPC"
        if window.get("kind") in ("quest_list", "gossip"):
            # A gossip-primary questgiver (npc_flags with both gossip and
            # questgiver set, e.g. Magistrix Erona) opens a "gossip" window
            # that already lists its offered quests (agent.npc.
            # parse_gossip_message's `quests`), never a quest_list/
            # quest_details window — accept_quest must work against that
            # window's `quests` the same way it does for quest_list's.
            if not any(q["quest_id"] == quest_id for q in window.get("quests", [])):
                return f"quest {quest_id} is not offered in the open quest window"
        elif window.get("quest_id") != quest_id:
            return "the open quest details window is for a different quest"
        return None

    def execute(self, session, world, npc_guid: int, quest_id: int, **_) -> ActionResult:
        window = world.get_ui_state()
        if window is not None and window.get("kind") == "gossip":
            # The gossip window only carries the quest's id/title/level, not
            # the full quest details the server expects the client to have
            # queried before accepting it — bridge that with
            # CMSG_QUESTGIVER_QUERY_QUEST (build_questgiver_query_quest) so
            # accept_quest works directly from a gossip window without the
            # LLM needing to know about the quest_list/gossip distinction.
            session._send_packet(qu.CMSG_QUESTGIVER_QUERY_QUEST,
                                  qu.build_questgiver_query_quest(npc_guid, quest_id))
        session._send_packet(qu.CMSG_QUESTGIVER_ACCEPT_QUEST,
                              qu.build_questgiver_accept_quest(npc_guid, quest_id))
        return ActionResult(ok=True, detail={"npc_guid": npc_guid, "quest_id": quest_id})


@register
class CompleteQuestAction(Action):
    name = "complete_quest"
    description = ("Tell a nearby questgiver you're ready to turn in quest_id — the first step "
                    "of turning a quest in. Opens the request-items or offer-reward window "
                    "(check the perception snapshot's `window` after calling this); use "
                    "turn_in_quest to actually finish once that window is open.")
    params = {
        "npc_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the questgiver NPC."},
        "quest_id": {"type": "integer", "description": "Quest ID to complete, from quest_log."},
    }
    required = ("npc_guid", "quest_id")

    def check(self, session, world, npc_guid: int, quest_id: int, **_) -> str | None:
        target = world.get_object(npc_guid)
        if target is None:
            return f"guid {npc_guid:#x} is not currently perceived"
        if session.player_position is not None:
            distance = target.distance_to(session.player_position)
            if distance is not None and distance > QUEST_INTERACT_RANGE_YD:
                return (f"guid {npc_guid:#x} is {distance:.1f} yd away, out of interact range "
                        f"({QUEST_INTERACT_RANGE_YD} yd) — try move_towards first")
        quest_log = world.build_quest_log()
        if not any(q["quest_id"] == quest_id for q in quest_log):
            return f"quest {quest_id} is not in the quest log"
        return None

    def execute(self, session, world, npc_guid: int, quest_id: int, **_) -> ActionResult:
        session._send_packet(qu.CMSG_QUESTGIVER_COMPLETE_QUEST,
                              qu.build_questgiver_complete_quest(npc_guid, quest_id))
        return ActionResult(ok=True, detail={"npc_guid": npc_guid, "quest_id": quest_id})


@register
class TurnInQuestAction(Action):
    name = "turn_in_quest"
    description = ("Finish turning in quest_id at a nearby questgiver, picking reward_choice "
                    "(index into the open offer-reward window's reward_choice_items, if any). "
                    "Requires the offer-reward window to already be open (call complete_quest "
                    "first).")
    params = {
        "npc_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the questgiver NPC."},
        "quest_id": {"type": "integer", "description": "Quest ID to turn in."},
        "reward_choice": {"type": "integer", "description": "Index of the reward item to pick, "
                                                              "from the open window's "
                                                              "reward_choice_items. Default 0."},
    }
    required = ("npc_guid", "quest_id")

    def check(self, session, world, npc_guid: int, quest_id: int, reward_choice: int = 0,
              **_) -> str | None:
        window = _open_quest_giver_window(session, world, npc_guid, quest_id, ("quest_offer_reward",))
        if window is None:
            return ("no offer-reward window is open for that NPC/quest — call complete_quest "
                     "first")
        choices = window.get("reward_choice_items", [])
        if choices and not (0 <= reward_choice < len(choices)):
            return f"reward_choice {reward_choice} is out of range (0..{len(choices) - 1})"
        return None

    def execute(self, session, world, npc_guid: int, quest_id: int, reward_choice: int = 0,
                **_) -> ActionResult:
        session._send_packet(qu.CMSG_QUESTGIVER_CHOOSE_REWARD,
                              qu.build_questgiver_choose_reward(npc_guid, quest_id, reward_choice))
        return ActionResult(ok=True, detail={"npc_guid": npc_guid, "quest_id": quest_id,
                                              "reward_choice": reward_choice})


@register
class AbandonQuestAction(Action):
    name = "abandon_quest"
    description = "Drop a quest from the quest log by its slot number. Irreversible."
    params = {
        "slot": {"type": "integer", "description": "Quest log slot (0-24), from quest_log's "
                                                     "`slot` field — not the quest id."},
    }
    required = ("slot",)

    def check(self, session, world, slot: int, **_) -> str | None:
        quest_log = world.build_quest_log()
        if not any(q["slot"] == slot for q in quest_log):
            return f"quest log slot {slot} is empty"
        return None

    def execute(self, session, world, slot: int, **_) -> ActionResult:
        session._send_packet(qu.CMSG_QUESTLOG_REMOVE_QUEST, qu.build_questlog_remove_quest(slot))
        return ActionResult(ok=True, detail={"slot": slot})


# ── Player trade (UM-59) ──────────────────────────────────────────────────
# Wire layouts verified against TrinityCore branch `3.3.5`, see
# agent/trade.py's docstring for the sources and, importantly, why our own
# offer (my_items/my_gold in world.trade) is tracked optimistically
# client-side rather than read back from a server packet.
#
# Confirmation shape: unlike most other actions' "wait for success-or-
# failure, timeout = failure", trade's own accept/open/accept_request calls
# routinely get *no* server reply at all when they succeed (the other side
# just hasn't acted yet — that's the normal, common case, not a stall). So
# those three wait briefly for a definite rejection and treat "nothing came
# back" as ok=True ("sent, no rejection seen yet"), never as a timeout
# failure. offer_item/offer_gold are different: TradeData::SetItem/SetMoney
# *always* answer the sender (TRADE_STATUS_BACK_TO_TRADE on success,
# TRADE_STATUS_TRADE_CANCELED/NOT_ON_TAPLIST/CLOSE_WINDOW on rejection), so
# those two use the normal wait-for-outcome-or-timeout-fails pattern.

TRADE_CONFIRM_TIMEOUT_S = DEFAULT_CONFIRM_TIMEOUT_S


def _find_trade_status_event(session, sent_at: float, status_names: tuple) -> dict | None:
    for e in session.events:
        if e.get("t", 0) < sent_at or e.get("kind") != "trade_status":
            continue
        if e.get("status_name") in status_names:
            return e
    return None


def _find_event_since(session, sent_at: float, kind: str) -> dict | None:
    for e in session.events:
        if e.get("t", 0) >= sent_at and e.get("kind") == kind:
            return e
    return None


def _resolve_offered_item(world, bag: int, slot: int):
    """(item_guid, entry, name, count, is_soulbound) for the item at
    bag/slot, or None if there's no known item there. `is_soulbound` reads
    the item *instance*'s own ITEM_FIELD_FLAGS (Item::IsSoulBound(),
    ItemTemplate.h ITEM_FIELD_FLAG_SOULBOUND) — distinct from the static
    template's Bonding type: a BoE item only becomes soulbound once bound,
    and this flag reflects that actual current state, not the item type."""
    item_guid = _find_item_guid(world, bag, slot)
    if item_guid is None:
        return None
    item_obj = world.get_object(item_guid)
    if item_obj is None:
        return None
    decoded = uf.decode_item_fields(item_obj.raw_fields) if item_obj.raw_fields else {}
    flags = decoded.get("item_flags") or 0
    is_soulbound = bool(flags & tr.ITEM_FIELD_FLAG_SOULBOUND)
    name = item_obj.name or None
    if name is None and item_obj.entry is not None:
        cached = world.items.items.get(item_obj.entry)
        if cached:
            name = cached["name"]
    return {"guid": item_guid, "entry": item_obj.entry, "name": name,
            "count": decoded.get("count"), "is_soulbound": is_soulbound}


@register
class OpenTradeAction(Action):
    name = "open_trade"
    description = (f"Request a trade with a nearby player. Must be within {tr.TRADE_DISTANCE_YD:.1f} "
                    "yd. The other side sees the request and decides via accept_trade_request.")
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the player to trade with."},
    }
    required = ("guid",)
    confirm_timeout = TRADE_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, guid: int, **_) -> str | None:
        if world.get_trade() is not None:
            return "a trade is already pending/open — cancel_trade first"
        target = world.get_object(guid)
        if target is None:
            return f"guid {guid:#x} is not currently perceived"
        if not target.is_player():
            return f"guid {guid:#x} is not a player"
        if session.player_position is not None:
            distance = target.distance_to(session.player_position)
            if distance is not None and distance > tr.TRADE_DISTANCE_YD:
                return (f"guid {guid:#x} is {distance:.1f} yd away, out of trade range "
                        f"({tr.TRADE_DISTANCE_YD:.1f} yd) — try move_towards first")
        return None

    def execute(self, session, world, guid: int, **_) -> ActionResult:
        sent_at = time.monotonic()
        world.start_trade_request(guid, initiated_by_me=True)
        session._send_packet(tr.CMSG_INITIATE_TRADE, tr.build_initiate_trade(guid))

        failure = _wait_for_value(lambda: _find_event_since(session, sent_at, "trade_cancelled"),
                                   timeout=self.confirm_timeout, interval=self.confirm_interval)
        if failure is not None:
            return ActionResult(ok=False, error=failure.get("reason", "trade request rejected"),
                                 detail=failure)
        return ActionResult(ok=True, detail={"guid": guid, "trade": world.get_trade()})


@register
class AcceptTradeRequestAction(Action):
    name = "accept_trade_request"
    description = "Accept a pending incoming trade request (see the trade window's `phase`: 'requested')."
    params = {}
    required = ()
    confirm_timeout = TRADE_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, **_) -> str | None:
        trade = world.get_trade()
        if trade is None or trade["phase"] != "requested":
            return "no incoming trade request is pending"
        if trade["initiated_by_me"]:
            return "you initiated this trade; wait for the other side to accept, don't accept your own request"
        return None

    def execute(self, session, world, **_) -> ActionResult:
        sent_at = time.monotonic()
        session._send_packet(tr.CMSG_BEGIN_TRADE, tr.build_begin_trade())

        failure = _wait_for_value(lambda: _find_event_since(session, sent_at, "trade_cancelled"),
                                   timeout=self.confirm_timeout, interval=self.confirm_interval)
        if failure is not None:
            return ActionResult(ok=False, error=failure.get("reason", "trade request expired"),
                                 detail=failure)
        return ActionResult(ok=True, detail={"trade": world.get_trade()})


@register
class OfferItemAction(Action):
    name = "offer_item"
    description = ("Put an item from your bag/slot into the open trade's next free slot "
                    "(0-5; pass trade_slot to pick a specific one). Offers the item's whole "
                    "stack — trade doesn't support splitting a partial count. Rejected if the "
                    "item is soulbound.")
    params = {
        "bag": {"type": "integer", "description": "Bag byte of the item (255 = equipped items/backpack)."},
        "slot": {"type": "integer", "description": "Inventory slot of the item."},
        "trade_slot": {"type": "integer", "description": "Which of the 6 trade slots (0-5) to put "
                                                           "it in. Default: the first free one."},
    }
    required = ("bag", "slot")
    confirm_timeout = TRADE_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def _pick_trade_slot(self, world, trade_slot: int | None) -> int | str:
        trade = world.get_trade()
        if trade_slot is not None:
            if not (0 <= trade_slot < tr.TRADE_SLOT_TRADED_COUNT):
                return f"trade_slot must be 0-{tr.TRADE_SLOT_TRADED_COUNT - 1}"
            return trade_slot
        for s in range(tr.TRADE_SLOT_TRADED_COUNT):
            if s not in trade["my_items"]:
                return s
        return "all 6 trade slots are already offered — pass trade_slot to replace one"

    def check(self, session, world, bag: int, slot: int, trade_slot: int | None = None, **_) -> str | None:
        trade = world.get_trade()
        if trade is None or trade["phase"] != "open":
            return "no trade window is open"
        item = _resolve_offered_item(world, bag, slot)
        if item is None:
            return f"no known item at bag={bag} slot={slot}"
        if item["is_soulbound"]:
            return f"item {item['entry']} is soulbound and can't be traded"
        picked = self._pick_trade_slot(world, trade_slot)
        if isinstance(picked, str):
            return picked
        return None

    def execute(self, session, world, bag: int, slot: int, trade_slot: int | None = None,
                **_) -> ActionResult:
        # Re-checked, not just trusted from check() — found in review
        # (same category as UM-60's mailbox-action fixes): the trade could
        # be cancelled by the partner, or the item moved/consumed, via a
        # concurrent update on the recv thread between check() and
        # execute(). A bare `item["guid"]`/`trade["my_items"]` below would
        # raise TypeError on a None — think.py's `except TypeError` does
        # catch that, but mislabels it as "bad params" even though the
        # LLM's params were fine.
        trade = world.get_trade()
        if trade is None or trade["phase"] != "open":
            return ActionResult(ok=False, error="no trade window is open")
        item = _resolve_offered_item(world, bag, slot)
        if item is None:
            return ActionResult(ok=False, error=f"item at bag={bag} slot={slot} is no longer there")
        picked_slot = self._pick_trade_slot(world, trade_slot)
        if isinstance(picked_slot, str):
            return ActionResult(ok=False, error=picked_slot)
        detail = {"bag": bag, "slot": slot, "trade_slot": picked_slot, "item": item}
        # TradeData::SetItem (TradeData.cpp) early-returns with no reply at
        # all — not even TRADE_STATUS_BACK_TO_TRADE — when this exact item
        # guid is already sitting in this exact trade slot. Found live
        # testing: re-offering the same item into the same slot always
        # timed out even though nothing was actually wrong. Skip the round
        # trip in that case instead of waiting for a confirmation that will
        # never come.
        if trade["my_items"].get(picked_slot, {}).get("guid") == item["guid"]:
            detail["note"] = "already offered at that trade slot — no packet sent"
            return ActionResult(ok=True, detail=detail)
        sent_at = time.monotonic()
        session._send_packet(tr.CMSG_SET_TRADE_ITEM, tr.build_set_trade_item(picked_slot, bag, slot))

        outcome = _wait_for_value(
            lambda: _find_trade_status_event(session, sent_at, ("back_to_trade", "trade_canceled", "not_on_taplist")),
            timeout=self.confirm_timeout, interval=self.confirm_interval)
        if outcome is None:
            return ActionResult(ok=False, error="no trade confirmation seen (timed out)", detail=detail)
        if outcome["status_name"] != "back_to_trade":
            detail["outcome"] = outcome
            return ActionResult(ok=False, error=outcome["status_name"], detail=detail)
        world.set_my_trade_item(picked_slot, item)
        return ActionResult(ok=True, detail=detail)


@register
class OfferGoldAction(Action):
    name = "offer_gold"
    description = "Set the gold (in copper) offered in the open trade. Replaces any previous amount."
    params = {
        "amount": {"type": "integer", "description": "Copper to offer (100 copper = 1 silver, "
                                                       "10000 = 1 gold). 0 clears a previous offer."},
    }
    required = ("amount",)
    confirm_timeout = TRADE_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, amount: int, **_) -> str | None:
        trade = world.get_trade()
        if trade is None or trade["phase"] != "open":
            return "no trade window is open"
        if amount < 0:
            return "amount must be >= 0"
        have = getattr(session, "coinage", 0) or 0
        if amount > have:
            return f"not enough gold (have {have}, tried to offer {amount})"
        return None

    def execute(self, session, world, amount: int, **_) -> ActionResult:
        detail = {"amount": amount}
        # Re-checked, not just trusted from check() — found in review, same
        # TOCTOU category as OfferItemAction above: the trade could be
        # cancelled by the partner between check() and execute().
        trade = world.get_trade()
        if trade is None or trade["phase"] != "open":
            return ActionResult(ok=False, error="no trade window is open", detail=detail)
        # TradeData::SetMoney (TradeData.cpp) early-returns with no reply at
        # all when `amount` equals what's already offered (0 the first time,
        # since my_gold starts at 0) — found live testing: offer_gold(0)
        # right after opening a trade always timed out even though nothing
        # was wrong. Skip the round trip in that case, same fix as
        # OfferItemAction.
        if trade["my_gold"] == amount:
            detail["note"] = "already offering that amount — no packet sent"
            return ActionResult(ok=True, detail=detail)
        sent_at = time.monotonic()
        session._send_packet(tr.CMSG_SET_TRADE_GOLD, tr.build_set_trade_gold(amount))

        outcome = _wait_for_value(
            lambda: _find_trade_status_event(session, sent_at, ("back_to_trade", "close_window")),
            timeout=self.confirm_timeout, interval=self.confirm_interval)
        if outcome is None:
            return ActionResult(ok=False, error="no trade confirmation seen (timed out)", detail=detail)
        if outcome["status_name"] != "back_to_trade":
            detail["outcome"] = outcome
            return ActionResult(ok=False, error=outcome.get("result_name", outcome["status_name"]), detail=detail)
        world.set_my_trade_gold(amount)
        return ActionResult(ok=True, detail=detail)


@register
class AcceptTradeAction(Action):
    name = "accept_trade"
    description = ("Accept the open trade — but only given what you currently believe the other "
                    "side is offering (from the trade window's `their_gold`/`their_items`). "
                    "Rejected if that no longer matches the live offer, so re-check the window "
                    "before calling this again; a change on either side after an accept un-accepts "
                    "both sides automatically (server behavior), so this never lets a stale offer "
                    "through silently.")
    params = {
        "expected_their_gold": {"type": "integer", "description": "Gold you believe the other "
                                                                    "side has offered. Default 0."},
        "expected_their_item_entries": {"type": "array", "items": {"type": "integer"},
                                         "description": "Item entries you believe the other side "
                                                         "has offered (any order). Omit/empty if "
                                                         "you believe they're offering no items."},
    }
    required = ()
    confirm_timeout = TRADE_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, expected_their_gold: int = 0,
              expected_their_item_entries: list | None = None, **_) -> str | None:
        trade = world.get_trade()
        if trade is None or trade["phase"] != "open":
            return "no trade window is open"
        actual_gold = trade["their_gold"]
        if actual_gold != expected_their_gold:
            return (f"the other side's offer has changed: expected {expected_their_gold} copper, "
                    f"they're now offering {actual_gold} — re-check the trade window")
        actual_entries = sorted(item["entry"] for item in trade["their_items"].values())
        expected_entries = sorted(expected_their_item_entries or [])
        if actual_entries != expected_entries:
            return (f"the other side's offer has changed: expected items {expected_entries}, "
                    f"they're now offering {actual_entries} — re-check the trade window")
        return None

    def execute(self, session, world, expected_their_gold: int = 0,
                expected_their_item_entries: list | None = None, **_) -> ActionResult:
        sent_at = time.monotonic()
        session._send_packet(tr.CMSG_ACCEPT_TRADE, tr.build_accept_trade())
        world.set_my_trade_accepted()

        def find_outcome():
            return (_find_event_since(session, sent_at, "trade_completed")
                    or _find_event_since(session, sent_at, "trade_cancelled"))

        outcome = _wait_for_value(find_outcome, timeout=self.confirm_timeout,
                                   interval=self.confirm_interval)
        if outcome is None:
            # The common case: the other side hasn't accepted yet — the
            # server gives the accepting player no ack at all until *both*
            # sides have (see this section's confirmation-shape note above).
            return ActionResult(ok=True, detail={"status": "accepted_locally_waiting_on_other_side"})
        if outcome["kind"] == "trade_completed":
            return ActionResult(ok=True, detail=outcome)
        return ActionResult(ok=False, error=outcome.get("reason", "trade cancelled"), detail=outcome)


@register
class CancelTradeAction(Action):
    name = "cancel_trade"
    description = "Cancel/decline the pending or open trade."
    params = {}
    required = ()
    confirm_timeout = TRADE_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, **_) -> str | None:
        if world.get_trade() is None:
            return "no trade is pending or open"
        return None

    def execute(self, session, world, **_) -> ActionResult:
        sent_at = time.monotonic()
        session._send_packet(tr.CMSG_CANCEL_TRADE, tr.build_cancel_trade())
        # CMSG_CANCEL_TRADE has no rejection path (Player::TradeCancel always
        # succeeds and always answers the sender, per agent/trade.py) — wait
        # for that answer as a nicety, but a slow/missed event here still
        # isn't a real failure the way a timeout is for e.g. buy_item.
        event = _wait_for_value(lambda: _find_event_since(session, sent_at, "trade_cancelled"),
                                 timeout=self.confirm_timeout, interval=self.confirm_interval)
        return ActionResult(ok=True, detail=event or {"status": "cancel_sent"})


# ── Mailbox (UM-60) ────────────────────────────────────────────────────────
# Wire layouts verified against TrinityCore branch `3.3.5`, see
# agent/mail.py's docstring for the sources.


def _find_nearby_mailbox(session, world):
    """Closest currently-perceived mailbox (ObjectInfo.is_mailbox()) within
    MAILBOX_INTERACT_RANGE_YD, or None. A gameobject's type only becomes
    known once its SMSG_GAMEOBJECT_QUERY_RESPONSE arrives (queued
    automatically the moment it's first perceived, same as its name) — a
    mailbox just perceived this instant may not be recognized yet."""
    if session.player_position is None:
        return None
    best = None
    best_dist = None
    for obj in world.get_objects().values():
        if not obj.is_mailbox():
            continue
        dist = obj.distance_to(session.player_position)
        if dist is None or dist > mailmod.MAILBOX_INTERACT_RANGE_YD:
            continue
        if best_dist is None or dist < best_dist:
            best, best_dist = obj, dist
    return best


def _mail_item_flags(world, bag: int, slot: int):
    """(item_guid, entry, is_soulbound) for the item at bag/slot, or None if
    there's no known item there. UM-59 (player trade) has the identical
    soulbound check in its own _resolve_offered_item — worth factoring into
    one shared helper instead of two independent copies."""
    item_guid = _find_item_guid(world, bag, slot)
    if item_guid is None:
        return None
    item_obj = world.get_object(item_guid)
    if item_obj is None:
        return None
    decoded = uf.decode_item_fields(item_obj.raw_fields) if item_obj.raw_fields else {}
    flags = decoded.get("item_flags") or 0
    is_soulbound = bool(flags & mailmod.ITEM_FIELD_FLAG_SOULBOUND)
    return item_guid, item_obj.entry, is_soulbound


@register
class OpenMailboxAction(Action):
    name = "open_mailbox"
    description = (f"Open the nearest mailbox within {mailmod.MAILBOX_INTERACT_RANGE_YD:.0f} yd "
                    "and request its inbox (check the perception snapshot's `mailbox` after "
                    "calling this).")
    params = {}
    required = ()
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, **_) -> str | None:
        if _find_nearby_mailbox(session, world) is None:
            return "no mailbox is within interact range — try move_towards a mailbox first"
        return None

    def execute(self, session, world, **_) -> ActionResult:
        mailbox = _find_nearby_mailbox(session, world)
        if mailbox is None:
            # Re-checked, not just trusted from check(): the mailbox is a
            # stationary gameobject, so this only fires on a genuine race
            # (e.g. it left perception via a concurrent OUT_OF_RANGE_OBJECTS
            # on the recv thread between check() and execute()) — found in
            # review. Reported as a normal ActionResult, not an
            # AttributeError, since that wouldn't be caught by think.py's
            # `except TypeError` and would crash the think cycle instead.
            return ActionResult(ok=False, error="mailbox no longer in range")
        world.open_mailbox_request(mailbox.guid)
        session._send_packet(mailmod.CMSG_GET_MAIL_LIST, mailmod.build_get_mail_list(mailbox.guid))

        def has_list():
            current = world.get_mailbox()
            return current is not None and current.get("mails") is not None

        got_list = _wait_for(has_list, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"mailbox_guid": mailbox.guid, "mailbox": world.get_mailbox()}
        if not got_list:
            return ActionResult(ok=False, error="no mail list response seen (timed out)", detail=detail)
        return ActionResult(ok=True, detail=detail)


@register
class SendMailAction(Action):
    name = "send_mail"
    description = ("Send mail (with optional gold and one optional item) to a player by name — "
                    "works even if they're offline or far away. Costs "
                    f"{mailmod.MAIL_POSTAGE_COPPER} copper postage in addition to any gold sent. "
                    "Requires a nearby mailbox.")
    params = {
        "to": {"type": "string", "description": "Recipient character name."},
        "subject": {"type": "string", "description": "Mail subject."},
        "body": {"type": "string", "description": "Mail body text."},
        "gold": {"type": "integer", "description": "Copper to send. Default 0."},
        "bag": {"type": "integer", "description": "Bag byte of an item to attach (255 = equipped "
                                                    "items/backpack). Omit to send no item."},
        "slot": {"type": "integer", "description": "Inventory slot of the item to attach — "
                                                     "required together with bag."},
    }
    required = ("to", "subject", "body")
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, to: str, subject: str, body: str, gold: int = 0,
              bag: int | None = None, slot: int | None = None, **_) -> str | None:
        if _find_nearby_mailbox(session, world) is None:
            return "no mailbox is within interact range — try move_towards a mailbox first"
        if gold < 0:
            return "gold must be >= 0"
        item_guid = None
        if (bag is None) != (slot is None):
            return "bag and slot must be given together"
        if bag is not None:
            resolved = _mail_item_flags(world, bag, slot)
            if resolved is None:
                return f"no known item at bag={bag} slot={slot}"
            item_guid, _entry, is_soulbound = resolved
            if is_soulbound:
                return "item is soulbound and can't be mailed"
        postage = mailmod.MAIL_POSTAGE_COPPER
        have = getattr(session, "coinage", 0) or 0
        if postage + gold > have:
            return f"not enough gold (have {have}, need {postage + gold} including postage)"
        return None

    def execute(self, session, world, to: str, subject: str, body: str, gold: int = 0,
                bag: int | None = None, slot: int | None = None, **_) -> ActionResult:
        mailbox = _find_nearby_mailbox(session, world)
        if mailbox is None:
            # Same TOCTOU guard as OpenMailboxAction.execute() — found in review.
            return ActionResult(ok=False, error="mailbox no longer in range")
        item_guid = None
        if bag is not None:
            # Re-checked, not just trusted from check() — found in review:
            # the item could be moved/consumed by a concurrent inventory
            # update on the recv thread between check() and execute(). A
            # bare unpack of None would raise TypeError, which think.py
            # *does* catch (unlike the mailbox case above) but mislabels
            # as "bad params" — misleading, since the LLM's params were
            # fine when it called this.
            resolved = _mail_item_flags(world, bag, slot)
            if resolved is None:
                return ActionResult(ok=False, error=f"item at bag={bag} slot={slot} is no longer there")
            item_guid, _entry, _is_soulbound = resolved
        sent_at = time.monotonic()
        session._send_packet(mailmod.CMSG_SEND_MAIL,
                              mailmod.build_send_mail(mailbox.guid, to, subject, body,
                                                       money=gold, item_guid=item_guid))

        def find_result():
            for e in session.events:
                if e.get("t", 0) < sent_at or e.get("kind") != "mail_result":
                    continue
                if e.get("command") == mailmod.MAIL_SEND:
                    return e
            return None

        outcome = _wait_for_value(find_result, timeout=self.confirm_timeout,
                                   interval=self.confirm_interval)
        detail = {"to": to, "subject": subject, "gold": gold, "bag": bag, "slot": slot}
        if outcome is None:
            _record_event(session, "mail_error", reason="no send-mail confirmation seen (timed out)", to=to)
            return ActionResult(ok=False, error="no send-mail confirmation seen (timed out)", detail=detail)
        detail["outcome"] = outcome
        if outcome["error_code"] != mailmod.MAIL_OK:
            _record_event(session, "mail_error", reason=outcome["error_name"], to=to)
            return ActionResult(ok=False, error=outcome["error_name"], detail=detail)
        summary = {"to": to, "subject": subject, "gold": gold, "item_attached": bag is not None}
        _record_event(session, "mail_sent", to=to, summary=summary)
        return ActionResult(ok=True, detail=detail)


@register
class TakeMailAction(Action):
    name = "take_mail"
    description = ("Take all money and every item attached to mail_id from the currently open "
                    "mailbox window (call open_mailbox first) — v1 take-everything, no selective "
                    "taking, same as loot().")
    params = {
        "mail_id": {"type": "integer", "description": "Mail id, from the open mailbox window's "
                                                        "`mails` list."},
    }
    required = ("mail_id",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, mail_id: int, **_) -> str | None:
        mailbox = world.get_mailbox()
        if mailbox is None or mailbox.get("mails") is None:
            return "no mailbox window is open — call open_mailbox first"
        mail = next((m for m in mailbox["mails"] if m["mail_id"] == mail_id), None)
        if mail is None:
            return f"mail_id {mail_id} is not in the open mailbox window"
        # A COD amount is charged automatically when taking the item(s) —
        # HandleMailTakeItem checks HasEnoughMoney(m->COD) server-side.
        if mail["attachments"] and mail["cod"] > (getattr(session, "coinage", 0) or 0):
            return (f"not enough gold to pay this mail's {mail['cod']} copper COD "
                     f"(have {getattr(session, 'coinage', 0) or 0})")
        return None

    def execute(self, session, world, mail_id: int, **_) -> ActionResult:
        mailbox = world.get_mailbox()
        mail = next((m for m in (mailbox or {}).get("mails") or [] if m["mail_id"] == mail_id), None)
        if mail is None:
            # Re-checked, not just trusted from check() — found in review:
            # a concurrent SMSG_MAIL_LIST_RESULT (recv thread) could replace
            # world.mailbox between check() and execute(). A bare next()
            # would raise StopIteration here, which think.py's `except
            # TypeError` doesn't catch, crashing the think cycle instead of
            # degrading to a normal ActionResult(ok=False).
            return ActionResult(ok=False, error=f"mail_id {mail_id} is no longer in the open mailbox window")

        def find_result(command, sent_at):
            def _find():
                for e in session.events:
                    if e.get("t", 0) < sent_at or e.get("kind") != "mail_result":
                        continue
                    if e.get("mail_id") == mail_id and e.get("command") == command:
                        return e
                return None
            return _find

        outcomes = {"money": None, "items": []}
        if mail["money"]:  # COD is charged automatically alongside taking an item, not via TAKE_MONEY
            sent_at = time.monotonic()
            session._send_packet(mailmod.CMSG_MAIL_TAKE_MONEY,
                                  mailmod.build_mail_take_money(mailbox["mailbox_guid"], mail_id))
            outcomes["money"] = _wait_for_value(find_result(mailmod.MAIL_MONEY_TAKEN, sent_at),
                                                 timeout=self.confirm_timeout,
                                                 interval=self.confirm_interval)

        for att in mail["attachments"]:
            # A fresh sent_at per attachment, not a shared one, matches by
            # time order instead of the response's `attach_id` — found in
            # review: SMSG_SEND_MAIL_RESULT only includes `attach_id` on
            # success (or item-expired); a same-mail_id failure for a
            # *different* reason (e.g. equip error) carries no attach_id at
            # all, so attach_id-matching would never see it and this
            # attachment would time out instead of surfacing the real
            # error. Safe because each take-item send blocks on its own
            # reply before the next is sent (sequential, not pipelined), so
            # "the next mail_result for this mail_id/command" is
            # unambiguous.
            sent_at = time.monotonic()
            session._send_packet(mailmod.CMSG_MAIL_TAKE_ITEM,
                                  mailmod.build_mail_take_item(mailbox["mailbox_guid"], mail_id,
                                                                att["attach_id"]))
            outcomes["items"].append(_wait_for_value(
                find_result(mailmod.MAIL_ITEM_TAKEN, sent_at),
                timeout=self.confirm_timeout, interval=self.confirm_interval))

        detail = {"mail_id": mail_id, "outcomes": outcomes}
        money_ok = outcomes["money"] is None or outcomes["money"].get("error_code") == mailmod.MAIL_OK
        items_ok = all(o is not None and o.get("error_code") == mailmod.MAIL_OK for o in outcomes["items"])
        if not money_ok or not items_ok:
            return ActionResult(ok=False, error="one or more mail attachments failed to take", detail=detail)
        return ActionResult(ok=True, detail=detail)


@register
class DeleteMailAction(Action):
    name = "delete_mail"
    description = ("Delete mail_id from the currently open mailbox window (call open_mailbox "
                    "first). Irreversible — take any attachments first, they're lost otherwise.")
    params = {
        "mail_id": {"type": "integer", "description": "Mail id, from the open mailbox window's "
                                                        "`mails` list."},
    }
    required = ("mail_id",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, mail_id: int, **_) -> str | None:
        mailbox = world.get_mailbox()
        if mailbox is None or mailbox.get("mails") is None:
            return "no mailbox window is open — call open_mailbox first"
        if not any(m["mail_id"] == mail_id for m in mailbox["mails"]):
            return f"mail_id {mail_id} is not in the open mailbox window"
        return None

    def execute(self, session, world, mail_id: int, **_) -> ActionResult:
        mailbox = world.get_mailbox()
        sent_at = time.monotonic()
        session._send_packet(mailmod.CMSG_MAIL_DELETE,
                              mailmod.build_mail_delete(mailbox["mailbox_guid"], mail_id))

        def find_result():
            for e in session.events:
                if e.get("t", 0) < sent_at or e.get("kind") != "mail_result":
                    continue
                if e.get("mail_id") == mail_id and e.get("command") == mailmod.MAIL_DELETED:
                    return e
            return None

        outcome = _wait_for_value(find_result, timeout=self.confirm_timeout,
                                   interval=self.confirm_interval)
        detail = {"mail_id": mail_id, "outcome": outcome}
        if outcome is None:
            return ActionResult(ok=False, error="no delete confirmation seen (timed out)", detail=detail)
        if outcome["error_code"] != mailmod.MAIL_OK:
            # e.g. a COD mail can't be deleted before it's paid/returned (MAIL_ERR_INTERNAL_ERROR)
            return ActionResult(ok=False, error=outcome["error_name"], detail=detail)
        return ActionResult(ok=True, detail=detail)