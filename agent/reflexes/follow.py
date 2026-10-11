#!/usr/bin/env python3
"""Follow-leader reflex (UM-58): follow a party member and assist their
current target, running between LLM think cycles.

Per docs/AGENT-DIRECTION.md ("Agents decide for themselves"), the LLM
decides *whether* to follow or assist — it flips this reflex on/off with the
`follow`/`assist`/`stop_following` tools registered at the bottom of this
module. Once on, the reflex itself keeps formation and matches the leader's
target without another LLM call per tick; that's the whole point — keeping
formation every few hundred ms is too fast/costly for a 10-30s LLM think
step (docs/AGENT-DIRECTION.md, decision 3).

Building blocks, both already merged ahead of this branch:
  - Movement: agent.movement.Mover.move_towards (UM-38) — client-side
    straight-line chase, already re-reads the target's live position every
    internal tick and stops on arrival/stuck/target-lost/stopped.
  - Combat/target: agent.actions.send_target/send_attack and the
    Action/ActionResult/REGISTRY framework (UM-36).

Reflex tick, per the card:
  1. Leader missing from perception (out of range, or on another map —
     this codebase's WorldState has no per-object map ID: update-object
     blocks never carry one, so a leader on another map manifests the same
     way as one out of range, via OUT_OF_RANGE_OBJECTS removing them from
     WorldState.objects) -> stop, record a `leader_lost` event, disable.
  2. Farther than distance+1 yd -> move_towards(leader). Mover.move_towards
     blocks the calling thread until arrival/stuck/lost/stopped, so one
     tick() call can itself span several seconds of real time — the
     production driver's "call tick() every ~250-500ms" cadence
     (agent/__main__.py) is naturally throttled by that; tests drive tick()
     directly against a fake Mover, so no real sleeping happens there.
  3. Within range -> stop moving, face the leader.
  4. assist on -> match the leader's hostile, in-combat target
     (UNIT_FIELD_TARGET, mapped by UM-33) with set_target + auto_attack;
     stop attacking once the target dies or the leader's target clears.
"""

import logging
import time

from .. import actions
from .. import movement
from ..ports import ActionSession, PacketSink, ReflexSlots

log = logging.getLogger("agent.reflexes.follow")

DEFAULT_DISTANCE_YD = 3.0
RANGE_MARGIN_YD = 1.0  # start walking once farther than distance + this
UNIT_FLAG_IN_COMBAT = 0x00080000  # UnitDefines.h — same mask agent/perception/nearby.py uses for in_combat


def _group_member_name(group: dict | None, guid: int) -> str:
    for m in (group or {}).get("members", ()):
        if m["guid"] == guid:
            return m["name"]
    return ""


def follow_target_error(session, world, guid: int, name: str = "") -> str | None:
    """Why `guid` may not be followed, or None when it may (#318). Valid: a human, or the
    party leader. Not valid: the agent itself; anything while this agent leads the party;
    another agent that is not the party leader.

    The rule as implemented: main cannot tell whether another agent is currently following
    (nothing publishes it), so every agent that is not the party leader is rejected. A leader
    agent is safe to follow because it never follows a member itself (the second rule), so a
    chain or a pair of agents following each other cannot form inside a party. Which characters
    are agents comes from session.agent_names (config AGENT_ROSTER); with no roster configured
    every non-leader is treated as a human."""
    name = name or world_player_name(world, guid) or _group_member_name(session.group, guid)
    mine = (session.player_name or "").lower()
    if (guid and guid == session.player_guid) or (name and mine and name.lower() == mine):
        return "an agent cannot follow itself"
    group = session.group
    if group and group["leader_guid"] == session.player_guid:
        return "this agent leads the party; a party leader never follows a member"
    if group and guid and group["leader_guid"] == guid:
        return None
    if name and name.lower() in session.agent_names:
        return (f"{name} is an agent but not the party leader; it may itself be following, "
                "so following it could form a chain or a cycle")
    return None


def world_player_name(world, guid: int) -> str:
    obj = world.get_object(guid)
    return (obj.name or "") if obj is not None else ""


def begin_follow(session, world, guid: int, name: str, *, reason: str,
                 distance: float = DEFAULT_DISTANCE_YD) -> str | None:
    """The one place a follow starts. Validates the target, then starts the reflex and
    records `follow_started` with why; an invalid target records `follow_rejected` and
    returns the reason. Callers: an accepted party invite (FollowReflex.tick), the
    escort-mission hook below and the brain's `follow` action."""
    error = follow_target_error(session, world, guid, name)
    if error is not None:
        session.record_event("follow_rejected", target_name=name, reason=error)
        return error
    group = session.group
    in_party = bool(group) and (guid == group["leader_guid"]
                                or any(m["guid"] == guid for m in group["members"]))
    get_follow_reflex(session).start(guid, leader_name=name, distance=distance, party_bound=in_party)
    session.record_event("follow_started", target_name=name, reason=reason)
    return None


def start_for_escort_mission(session, world, guid: int, name: str,
                             distance: float = DEFAULT_DISTANCE_YD) -> str | None:
    """Hook for the escort mission (#314, not on main yet): the mission calls this to start
    following its escortee. Same validation as every other start; nothing calls it yet."""
    return begin_follow(session, world, guid, name, reason="escort_mission", distance=distance)


class FollowReflex:
    """Per-session follow/assist state. One instance per WoWSession — see
    get_follow_reflex(). Not thread-safe by itself; in production only the
    main loop calls tick(), and LLM actions that touch movement pause it
    first (see pause_for_llm_override)."""

    def __init__(self):
        self.enabled = False
        self.leader_guid: int | None = None
        self.leader_name: str | None = None
        self.distance: float = DEFAULT_DISTANCE_YD
        self.assist: bool = False
        self._assist_target_guid: int | None = None
        self.party_bound = False  # started on a party member: leaving the party ends it

    # ── LLM-facing controls (see the registered actions below) ───────────
    def start(self, leader_guid: int, leader_name: str | None = None,
              distance: float = DEFAULT_DISTANCE_YD, party_bound: bool = False):
        self.enabled = True
        self.party_bound = party_bound
        self.leader_guid = leader_guid
        self.leader_name = leader_name
        self.distance = distance
        self._assist_target_guid = None

    def set_assist(self, on: bool):
        self.assist = on

    def stop(self, session=None, world=None, reason: str = "stopped"):
        """Disable the reflex. If session/world are given, also halts any
        in-progress movement/attack and records a `follow_stopped` event
        (skipped if the reflex was already disabled — no event storm from
        repeated stop() calls, e.g. stop_following() called twice)."""
        was_enabled = self.enabled
        self.enabled = False
        if session is not None and world is not None:
            movement.get_mover(session, world).stop()
            if was_enabled and self._assist_target_guid is not None:
                session.send_packet(actions.CMSG_ATTACKSTOP)
        self.leader_guid = None
        self._assist_target_guid = None
        if session is not None and was_enabled:
            session.record_event("follow_stopped", reason=reason)

    def status(self) -> dict:
        return {
            "following": self.enabled,
            "leader_guid": self.leader_guid,
            "leader_name": self.leader_name,
            "distance": self.distance,
            "assist": self.assist,
        }

    # ── the reflex tick ───────────────────────────────────────────────────
    def tick(self, session: ActionSession, world):
        """One reflex step. Safe to call repeatedly — production:
        agent/__main__.py's loop, between LLM think cycles; tests: call
        directly with a fake session/world. No-op if not enabled."""
        self._settle_accepted_invite(session, world)
        if not self.enabled or self.leader_guid is None:
            return

        reason = self._stop_reason(session, world)
        if reason is not None:
            self.stop(session, world, reason=reason)
            return

        leader = world.get_object(self.leader_guid)
        if leader is None or leader.position is None or session.player_position is None:
            self._lost(session, world, "leader out of perception range or on another map")
            return

        if leader.is_dead():
            self.stop(session, world, reason="target_died")
            return

        distance = leader.distance_to(session.player_position)
        if distance is None:
            self._lost(session, world, "leader out of perception range or on another map")
            return

        mover = movement.get_mover(session, world)
        if distance > self.distance + RANGE_MARGIN_YD:
            result = mover.move_towards(self.leader_guid, stop_distance=self.distance)
            if not result.get("ok") and result.get("error") == "target lost":
                self._lost(session, world, "leader left perception mid-move")
                return
            # "stuck"/"stopped": leave the reflex enabled and just retry
            # next tick — a temporary obstacle or an externally-cancelled
            # move isn't the leader disappearing.
        else:
            mover.stop()
            face = actions.FaceAction()
            if face.check(session, world, guid=self.leader_guid) is None:
                face.execute(session, world, guid=self.leader_guid)

        if self.assist:
            self._tick_assist(session, world, leader)

    def _settle_accepted_invite(self, session, world):
        """The party path (#318): an accepted invite whose leader is a valid target starts
        following once the server has sent the party list. The pending accept is settled
        exactly once: when the list arrives (a running follow is kept, not restarted) or
        when its window ends, whatever the group state. It never outlives either."""
        pending = session.follow_on_accept
        if pending is None:
            return
        inviter, deadline = pending
        group = session.group
        if time.monotonic() > deadline:
            session.follow_on_accept = None
            return
        if group is None:
            return
        session.follow_on_accept = None
        if self.enabled:
            return
        leader_guid = group["leader_guid"]
        if _group_member_name(group, leader_guid).lower() == inviter.lower():
            begin_follow(session, world, leader_guid, inviter, reason="party_invite")

    def _stop_reason(self, session, world) -> str | None:
        """Why following must end now, from session state alone (#318), or None."""
        if not session.is_alive:
            return "self_died"
        if follow_target_error(session, world, self.leader_guid, self.leader_name or "") is not None:
            return "target_invalid"
        if self.party_bound:
            group = session.group
            if group is None:
                return "left_party"
            if self.leader_guid != group["leader_guid"] and \
                    all(m["guid"] != self.leader_guid for m in group["members"]):
                return "target_left_party"
            if any(m["guid"] == self.leader_guid and not m["online"] for m in group["members"]):
                return "target_offline"
        return None

    def _lost(self, session: ActionSession, world, reason: str):
        self.stop(session, world, reason="leader_lost")
        session.record_event("leader_lost", reason=reason)

    def _tick_assist(self, session: ActionSession, world, leader):
        target_guid = leader.target_guid or 0
        if not target_guid:
            self._stop_assist_attack(session)
            return

        if target_guid != self._assist_target_guid:
            session.record_event("leader_changed_target", target_guid=target_guid)

        target = world.get_object(target_guid)
        if target is None or target.is_dead():
            self._stop_assist_attack(session)
            return

        me = world.get_my_object()
        if target.is_hostile_to(me.faction if me is not None else None) is False:
            return  # known-friendly — never assist an attack on it

        in_combat = bool(target.unit_flags and (target.unit_flags & UNIT_FLAG_IN_COMBAT)) or \
            bool(leader.unit_flags and (leader.unit_flags & UNIT_FLAG_IN_COMBAT))
        if not in_combat:
            return

        if target_guid != self._assist_target_guid:
            actions.send_target(session, target_guid)
            actions.send_attack(session, target_guid)
            self._assist_target_guid = target_guid

    def _stop_assist_attack(self, session: PacketSink):
        if self._assist_target_guid is not None:
            session.send_packet(actions.CMSG_ATTACKSTOP)
            self._assist_target_guid = None


def get_follow_reflex(session: ReflexSlots) -> FollowReflex:
    """One FollowReflex per session, created lazily and cached on it —
    mirrors agent.movement.get_mover()."""
    if session.follow_reflex is None:
        session.follow_reflex = FollowReflex()
    return session.follow_reflex


def pause_for_llm_override(session: ActionSession, world, reason: str = "llm_override"):
    """Called by move_to/move_towards/stop_movement (agent/actions/movement.py) before
    they act: an explicit conflicting movement action from the LLM outranks
    the reflex (docs/AI-AGENT-SPEC.md's reflex priority: survival > follow/
    assist > idle), so following pauses and reports why via a
    `follow_stopped` event instead of fighting the next move command. A
    no-op if nothing is currently following."""
    reflex = session.follow_reflex
    if reflex is not None and reflex.enabled:
        reflex.stop(session, world, reason=reason)


if pause_for_llm_override not in actions.base.MOVE_OVERRIDE_HOOKS:
    actions.base.MOVE_OVERRIDE_HOOKS.append(pause_for_llm_override)


def _find_player_by_name(world, name: str):
    """Best-effort: scan perceived players for a case-insensitive name
    match. Player names are resolved by UM-35 (not merged as of this
    writing — agent.perception.ObjectInfo.name is "" until then), so this
    will only find a match once that lands; documented as a known
    limitation rather than blocking this reflex on it."""
    needle = name.lower()
    for obj in world.get_objects().values():
        if obj.object_type == "player" and obj.name and obj.name.lower() == needle:
            return obj
    return None


# ── Tools for the LLM (UM-36 REGISTRY / future UM-44 catalog) ────────────

@actions.register
class FollowAction(actions.Action):
    name = "follow"
    description = ("Follow a nearby party member/player by name, keeping roughly `distance` "
                    "yards away and matching their target if assist is on, until "
                    "stop_following is called or the leader is lost.")
    params = {
        "player_name": {"type": "string", "description": "Name of a nearby player to follow (case-insensitive)."},
        "distance": {"type": "number",
                     "description": f"How close to stay, in yards. Default {DEFAULT_DISTANCE_YD}."},
    }
    required = ("player_name",)

    def check(self, session, world, player_name: str, distance: float = DEFAULT_DISTANCE_YD, **_) -> str | None:
        error = actions.player_name_error(player_name, field="player_name")
        if error is not None:
            return error
        if session.player_position is None:
            return "own position unknown"
        if _find_player_by_name(world, player_name) is None:
            return f"no nearby player named {player_name!r} in perception"
        if distance <= 0:
            return "distance must be positive"
        leader = _find_player_by_name(world, player_name)
        return follow_target_error(session, world, leader.guid, player_name)

    def execute(self, session, world, player_name: str, distance: float = DEFAULT_DISTANCE_YD,
                **_) -> actions.ActionResult:
        leader = _find_player_by_name(world, player_name)
        error = begin_follow(session, world, leader.guid, player_name, reason="llm_requested", distance=distance)
        if error is not None:
            return actions.ActionResult(ok=False, error=error)
        return actions.ActionResult(ok=True, detail={"following": player_name, "distance": distance})


@actions.register
class AssistAction(actions.Action):
    name = "assist"
    description = "Turn assisting the followed leader's current combat target on or off."
    params = {
        "on": {"type": "boolean", "description": "True to assist, false to stop assisting."},
    }
    required = ("on",)

    def check(self, session, world, on: bool, **_) -> str | None:
        if not get_follow_reflex(session).enabled:
            return "not currently following anyone — call follow first"
        return None

    def execute(self, session, world, on: bool, **_) -> actions.ActionResult:
        reflex = get_follow_reflex(session)
        reflex.set_assist(on)
        if not on:
            reflex._stop_assist_attack(session)
        return actions.ActionResult(ok=True, detail={"assist": on})


@actions.register
class StopFollowingAction(actions.Action):
    name = "stop_following"
    description = "Stop following the current leader and stop assisting."
    params = {}
    required = ()

    def execute(self, session, world, **_) -> actions.ActionResult:
        reflex = get_follow_reflex(session)
        was_following = reflex.enabled
        reflex.stop(session, world, reason="llm_requested")
        return actions.ActionResult(ok=True, detail={"was_following": was_following})
