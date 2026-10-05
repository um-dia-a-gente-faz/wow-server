#!/usr/bin/env python3
"""Death recovery (UM-43): release spirit -> corpse run -> reclaim corpse,
with a spirit-healer fallback if the corpse can't be reached.

Verified against TrinityCore branch `3.3.5`:
  src/server/game/Server/Protocol/Opcodes.h
  src/server/game/Server/Packets/MiscPackets.h/.cpp
    (RepopRequest::Read, ReclaimCorpse::Read)
  src/server/game/Handlers/MiscHandler.cpp
    (HandleRepopRequest, HandleReclaimCorpse)
  src/server/game/Handlers/NPCHandler.cpp
    (HandleSpiritHealerActivateOpcode — raw uint64 guid, ObjectGuid::operator>>)
  src/server/game/Entities/Player/Player.h
    (CORPSE_RECLAIM_RADIUS = 39; PLAYER_FLAGS_GHOST = 0x10)

agent/session.py owns the receive side (SMSG_CORPSE_RECLAIM_DELAY,
SMSG_DEATH_RELEASE_LOC, MSG_CORPSE_QUERY's response) and the 'death' event;
this module owns the two LLM-facing actions plus the corpse-run/fallback
state machine.

'death' and 'resurrect' events match docs/AI-AGENT-SPEC.md's Events table.
"""

import math
import struct
import time

from . import actions
from . import movement
from .perception import PLAYER_FLAGS_GHOST, UNIT_NPC_FLAG_SPIRITHEALER

CMSG_REPOP_REQUEST          = 0x15A
CMSG_RECLAIM_CORPSE         = 0x1D2
CMSG_SPIRIT_HEALER_ACTIVATE = 0x21C
MSG_CORPSE_QUERY            = 0x216  # empty client->server request

CORPSE_RECLAIM_RADIUS_YD = 39.0  # Player.h CORPSE_RECLAIM_RADIUS
CORPSE_OBJECT_MATCH_RADIUS_YD = 5.0  # how close a "corpse" object must be to
                                     # session.corpse_position to be *our*
                                     # corpse — there's no GUID in MSG_CORPSE_
                                     # QUERY's response, only a position, so
                                     # this is a best-effort match against
                                     # whatever corpse object perception has
SPIRIT_HEALER_SEARCH_RANGE_YD = 200.0  # ghosts can see much further than 50yd
                                       # in the real client; generous on purpose
SPIRIT_HEALER_STOP_DISTANCE_YD = 5.0

MAX_DETOUR_ATTEMPTS = 3
DETOUR_ANGLE_DEG = 30.0
DETOUR_DISTANCE_YD = 15.0

RESURRECT_CONFIRM_TIMEOUT_S = 5.0
RESURRECT_CONFIRM_POLL_S = 0.2


def _is_ghost(world) -> bool:
    me = world.get_my_object()
    return bool(me is not None and me.is_ghost())


def _query_corpse(session) -> bool:
    """Ask the server where our corpse is (nothing else ever sends
    MSG_CORPSE_QUERY, so session.corpse_position stays None otherwise — gh-208)
    and wait briefly for the response. True once a position is known."""
    if session.corpse_position is not None:
        return True
    session._send_packet(MSG_CORPSE_QUERY, b'')
    return actions._wait_for(lambda: session.corpse_position is not None,
                             timeout=RESURRECT_CONFIRM_TIMEOUT_S, interval=RESURRECT_CONFIRM_POLL_S)


def _rotate(dx: float, dy: float, angle_rad: float) -> tuple:
    cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
    return dx * cos_a - dy * sin_a, dx * sin_a + dy * cos_a


def _corpse_run(session, world, target_pos: tuple,
                 max_detours: int = MAX_DETOUR_ATTEMPTS,
                 detour_angle_deg: float = DETOUR_ANGLE_DEG,
                 detour_distance_yd: float = DETOUR_DISTANCE_YD) -> dict:
    """Straight-line move_to the corpse; on 'stuck', try alternating
    +/-detour_angle_deg waypoints (widening by detour_angle_deg on each full
    +/- pair) a few times before giving up. Returns the last move_to-shaped
    result dict (agent.movement's {"ok", "error", "detail"} shape).

    A pure function of (session, world, target_pos) plus the injected mover
    (via movement.get_mover) so tests can swap in a fake mover that scripts
    a 'stuck' -> ... -> 'ok' sequence without any real movement/threads.
    """
    mover = movement.get_mover(session, world)
    _map_id, tx, ty, tz = target_pos
    result = mover.move_to(tx, ty, tz)
    if result.get("ok") or result.get("error") != "stuck":
        return result

    sign = 1
    angle_deg = detour_angle_deg
    for _attempt in range(max_detours):
        pos = session.player_position
        if pos is None:
            return result
        _, cx, cy, _cz, _o = pos
        dx, dy = tx - cx, ty - cy
        dist = math.hypot(dx, dy) or 1e-6
        rdx, rdy = _rotate(dx, dy, math.radians(angle_deg * sign))
        norm = math.hypot(rdx, rdy) or 1e-6
        step = min(detour_distance_yd, dist)
        waypoint_x = cx + rdx / norm * step
        waypoint_y = cy + rdy / norm * step

        detour_result = mover.move_to(waypoint_x, waypoint_y, _cz)
        if not detour_result.get("ok"):
            result = detour_result
            if result.get("error") != "stuck":
                return result
        else:
            result = mover.move_to(tx, ty, tz)
            if result.get("ok") or result.get("error") != "stuck":
                return result

        if sign == -1:
            angle_deg += detour_angle_deg  # widen after each full +/- pair
        sign *= -1

    return result


def _find_my_corpse(world, corpse_position: tuple, max_dist: float = CORPSE_OBJECT_MATCH_RADIUS_YD):
    """The closest perceived 'corpse' object to session.corpse_position.
    MSG_CORPSE_QUERY's response carries a position but no GUID (see
    agent/session.py::_handle_corpse_query_response), so once close enough
    for it to actually be in perception this is a positional match, not a
    GUID lookup — reasonable given a player has at most one own corpse
    nearby at a time."""
    best = None
    best_dist = None
    for obj in world.get_objects().values():
        if obj.object_type != "corpse":
            continue
        dist = obj.distance_to(corpse_position)
        if dist is None or dist > max_dist:
            continue
        if best_dist is None or dist < best_dist:
            best, best_dist = obj, dist
    return best


def _find_spirit_healer(world, my_position, max_range: float = SPIRIT_HEALER_SEARCH_RANGE_YD):
    best = None
    best_dist = None
    for obj in world.get_objects().values():
        if not (obj.npc_flags and (obj.npc_flags & UNIT_NPC_FLAG_SPIRITHEALER)):
            continue
        dist = obj.distance_to(my_position) if my_position is not None else None
        if dist is None or dist > max_range:
            continue
        if best_dist is None or dist < best_dist:
            best, best_dist = obj, dist
    return best


def _activate_spirit_healer(session, world, guid: int) -> actions.ActionResult:
    mover = movement.get_mover(session, world)
    move_result = mover.move_towards(guid, stop_distance=SPIRIT_HEALER_STOP_DISTANCE_YD)
    if not move_result.get("ok"):
        return actions.ActionResult(ok=False,
                                     error=f"could not reach spirit healer ({move_result.get('error')})",
                                     detail=move_result)
    session._send_packet(CMSG_SPIRIT_HEALER_ACTIVATE, struct.pack('<Q', guid))
    confirmed = actions._wait_for(lambda: not _is_ghost(world),
                                   timeout=RESURRECT_CONFIRM_TIMEOUT_S, interval=RESURRECT_CONFIRM_POLL_S)
    if not confirmed:
        return actions.ActionResult(ok=False, error="no resurrection confirmed after spirit healer activation",
                                     detail={"guid": guid})
    session._record_event("resurrect", method="spirit_healer")
    return actions.ActionResult(ok=True, detail={"method": "spirit_healer", "guid": guid})


@actions.register
class ReleaseSpiritAction(actions.Action):
    name = "release_spirit"
    description = ("After dying, release your spirit to become a ghost at the graveyard. "
                    "Required before a corpse run/reclaim_corpse.")
    params = {}
    required = ()

    def check(self, session, world, **_):
        me = world.get_my_object()
        if me is None:
            return "own object not yet perceived"
        if me.is_ghost():
            return "already released — already a ghost"
        if not me.is_dead():
            return "not dead"
        return None

    def execute(self, session, world, **_) -> actions.ActionResult:
        session._send_packet(CMSG_REPOP_REQUEST, struct.pack('<B', 0))
        confirmed = actions._wait_for(lambda: _is_ghost(world),
                                       timeout=RESURRECT_CONFIRM_TIMEOUT_S, interval=RESURRECT_CONFIRM_POLL_S)
        if not confirmed:
            return actions.ActionResult(ok=False, error="no ghost state confirmed after release")
        session._send_packet(MSG_CORPSE_QUERY, b'')  # populate corpse_position for the corpse run (gh-208)
        return actions.ActionResult(ok=True, detail={"position": session.player_position})


@actions.register
class ReclaimCorpseAction(actions.Action):
    name = "reclaim_corpse"
    description = ("Walk to your corpse and resurrect there. Falls back to the nearest "
                    "spirit healer if the corpse can't be reached (obstacle, out of range, "
                    "or no corpse perceived). Asks the server for the corpse position if unknown. "
                    "Requires release_spirit first.")
    params = {}
    required = ()

    def check(self, session, world, **_):
        me = world.get_my_object()
        if me is None or not me.is_ghost():
            return "not a ghost — call release_spirit first"
        ready_at = session.corpse_reclaim_ready_at
        if ready_at is not None and time.monotonic() < ready_at:
            return f"corpse reclaim still on cooldown ({ready_at - time.monotonic():.0f}s left)"
        return None

    def execute(self, session, world, **_) -> actions.ActionResult:
        if not _query_corpse(session):
            run_result = {"ok": False, "error": "corpse position unknown (no MSG_CORPSE_QUERY response)"}
        else:
            run_result = _corpse_run(session, world, session.corpse_position)
        if run_result.get("ok"):
            corpse = _find_my_corpse(world, session.corpse_position)
            if corpse is not None:
                distance = corpse.distance_to(session.player_position) if session.player_position else None
                if distance is None or distance <= CORPSE_RECLAIM_RADIUS_YD:
                    session._send_packet(CMSG_RECLAIM_CORPSE, struct.pack('<Q', corpse.guid))
                    confirmed = actions._wait_for(
                        lambda: not _is_ghost(world),
                        timeout=RESURRECT_CONFIRM_TIMEOUT_S, interval=RESURRECT_CONFIRM_POLL_S)
                    if confirmed:
                        session._record_event("resurrect", method="corpse")
                        return actions.ActionResult(ok=True, detail={"method": "corpse", "corpse_guid": corpse.guid})
                    # fall through to the spirit-healer fallback below

        healer = _find_spirit_healer(world, session.player_position)
        if healer is None:
            return actions.ActionResult(
                ok=False,
                error=f"corpse run failed ({run_result.get('error')}) and no spirit healer in perception",
                detail=run_result)
        return _activate_spirit_healer(session, world, healer.guid)
