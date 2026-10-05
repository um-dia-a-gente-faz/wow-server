#!/usr/bin/env python3
"""MovementInfo wire writer — the client-side counterpart of
agent.update_object.parse_movement_info: the same packed-guid + MovementInfo
shape, but built for *sending* instead of parsed from a received packet.
Used by every MSG_MOVE_* the agent sends (agent/actions.py's `face`; the
straight-line Mover below for UM-38).

Verified against TrinityCore branch `3.3.5`:
  src/server/game/Server/Packets/MovementPackets.cpp
    (WorldPackets::Movement::ClientPlayerMovement::Read: packed guid, then
    the MovementInfo operator>>; symmetric with operator<< — see
    docs/PROTOCOL-NOTES.md's SMSG_MESSAGECHAT-adjacent movement section)
  src/server/game/Handlers/MovementHandler.cpp
    (HandleMovementOpcodes/HandleMovementOpcode — every MSG_MOVE_* the
    client sends shares this one wire shape; ValidateAndGetUnitBeingMoved
    requires CMSG_SET_ACTIVE_MOVER to have been sent first — see
    agent/session.py::login_character and docs/PROTOCOL-NOTES.md)
  src/server/game/Server/Protocol/Opcodes.h
  src/server/game/Entities/Unit/UnitDefines.h (MOVEMENTFLAG_FORWARD, UnitMoveType)

UM-38's straight-line movement (Mover, _simulate) is client-authoritative,
same as the real protocol: the agent simulates its own position at a fixed
tick rate and reports it, rather than asking the server to move it. No
terrain height (no navmesh in v1) — z is linearly interpolated start-to-
destination, which will look wrong over hills; see docs/ROADMAP.md's
movement section.
"""

import math
import struct
import threading
import time

from . import packets as pk
from .update_object import (
    MOVEMENTFLAG_ONTRANSPORT,
    MOVEMENTFLAG_FALLING,
    MOVEMENTFLAG_FORWARD,
    MOVEMENTFLAG_SWIMMING,
    MOVEMENTFLAG_FLYING,
    MOVEMENTFLAG_SPLINE_ELEVATION,
    MOVEMENTFLAG2_ALWAYS_ALLOW_PITCHING,
)

MSG_MOVE_SET_FACING = 0x0DA
MSG_MOVE_START_FORWARD = 0x0B5
MSG_MOVE_STOP = 0x0B7
MSG_MOVE_HEARTBEAT = 0x0EE


def client_time_ms() -> int:
    """A plausible value for MovementInfo.time — TrinityCore's
    ValidateMovementInfo only sanity-checks *deltas* between successive
    positions for real movement (speed/anti-cheat), not this field's
    absolute value, so a monotonic per-process clock is enough; real
    movement tracking (UM-38) may want a shared counter instead."""
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def build_movement_info(guid: int, x: float, y: float, z: float, o: float,
                         move_flags: int = 0, move_flags2: int = 0,
                         fall_time: int = 0, pitch: float = 0.0,
                         spline_elevation: float = 0.0) -> bytes:
    """packed guid + MovementInfo, ready to send right after any MSG_MOVE_*
    opcode. Only the conditional fields the agent can actually produce today
    are supported (SWIMMING/FLYING/ALWAYS_ALLOW_PITCHING -> pitch,
    SPLINE_ELEVATION -> spline_elevation); ONTRANSPORT and FALLING raise,
    since the agent doesn't ride transports or jump yet — better than
    silently sending a malformed packet.
    """
    if move_flags & MOVEMENTFLAG_ONTRANSPORT:
        raise NotImplementedError("agent doesn't send ONTRANSPORT movement yet")
    if move_flags & MOVEMENTFLAG_FALLING:
        raise NotImplementedError("agent doesn't send FALLING movement yet")

    body = pk.pack_packed_guid(guid)
    body += struct.pack('<I', move_flags)
    body += struct.pack('<H', move_flags2)
    body += struct.pack('<I', client_time_ms())
    body += struct.pack('<4f', x, y, z, o)

    if (move_flags & (MOVEMENTFLAG_SWIMMING | MOVEMENTFLAG_FLYING)) \
            or (move_flags2 & MOVEMENTFLAG2_ALWAYS_ALLOW_PITCHING):
        body += struct.pack('<f', pitch)

    body += struct.pack('<I', fall_time)

    if move_flags & MOVEMENTFLAG_SPLINE_ELEVATION:
        body += struct.pack('<f', spline_elevation)

    return body


def send_set_facing(session, x: float, y: float, z: float, o: float):
    """MSG_MOVE_SET_FACING (0x0DA): turn in place to face orientation `o`,
    without otherwise moving — agent/actions.py::FaceAction."""
    payload = build_movement_info(session.player_guid, x, y, z, o)
    session._send_packet(MSG_MOVE_SET_FACING, payload)


def send_start_forward(session, x: float, y: float, z: float, o: float):
    payload = build_movement_info(session.player_guid, x, y, z, o, move_flags=MOVEMENTFLAG_FORWARD)
    session._send_packet(MSG_MOVE_START_FORWARD, payload)


def send_heartbeat(session, x: float, y: float, z: float, o: float):
    payload = build_movement_info(session.player_guid, x, y, z, o, move_flags=MOVEMENTFLAG_FORWARD)
    session._send_packet(MSG_MOVE_HEARTBEAT, payload)


def send_stop(session, x: float, y: float, z: float, o: float):
    payload = build_movement_info(session.player_guid, x, y, z, o, move_flags=0)
    session._send_packet(MSG_MOVE_STOP, payload)


# ── Straight-line movement (UM-38) ───────────────────────────────────────
# UnitMoveType (UnitDefines.h): index of run speed in ObjectInfo.speeds.
RUN_SPEED_INDEX = 1
DEFAULT_RUN_SPEED_YPS = 7.0  # base run speed for most races/classes at low level, no auras

TICK_INTERVAL_S = 0.1               # frequent position updates keep observers' interpolation smooth
ARRIVE_STOP_DISTANCE_YD = 1.0
REFACE_DRIFT_DEG = 10.0
SERVER_DRIFT_MAX_YD = 3.0           # self position from perception vs. our simulation
STUCK_PROGRESS_MIN_YD = 0.5
STUCK_PROGRESS_WINDOW_S = 3.0


def run_speed_of(obj) -> float:
    """`obj.speeds[RUN_SPEED_INDEX]` if we've ever seen a LIVING movement
    block for it (perception.py), else DEFAULT_RUN_SPEED_YPS."""
    if obj is not None and obj.speeds and len(obj.speeds) > RUN_SPEED_INDEX:
        return obj.speeds[RUN_SPEED_INDEX] or DEFAULT_RUN_SPEED_YPS
    return DEFAULT_RUN_SPEED_YPS


def _dead_before_release(world) -> bool:
    """A dead player remains at the corpse until the server releases the spirit."""
    me = world.get_my_object() if world is not None else None
    return bool(me is not None and me.is_dead() and not me.is_ghost())


def _heading(from_x: float, from_y: float, to_x: float, to_y: float) -> float:
    return math.atan2(to_y - from_y, to_x - from_x) % (2 * math.pi)


def _angle_diff(a: float, b: float) -> float:
    """Smallest signed difference a-b, wrapped to (-pi, pi]."""
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def _simulate(session, world, get_target, stop_distance: float, run_speed: float | None,
              tick_interval: float, stop_event: threading.Event, clock, sleep) -> dict:
    """The movement tick loop — a plain function (not a Mover method) so
    tests can drive it directly with a fake clock/sleep and no real thread.

    `get_target()` -> (x, y, z|None), called once per tick: move_to's caller
    gives a fixed point; move_towards's caller re-reads a guid's perceived
    position from WorldState each tick (so it chases). Returns None if the
    target is no longer available (e.g. the object left perception).

    Returns {"ok": bool, "error": str | None, "detail": dict} — wrapped into
    an agent.actions.ActionResult by the caller (kept as a plain dict here
    so this module never needs to import actions.py, which imports this one).
    """
    map_id, sx, sy, sz, _so = session.player_position
    if _dead_before_release(world):
        return {"ok": False, "error": "dead before spirit release", "detail": {}}
    target = get_target()
    if target is None:
        return {"ok": False, "error": "no target position", "detail": {}}
    tx, ty, tz = target
    if tz is None:
        tz = sz

    speed = run_speed if run_speed is not None else run_speed_of(world.get_my_object())

    cur = [sx, sy, sz]
    heading = _heading(cur[0], cur[1], tx, ty)
    send_start_forward(session, cur[0], cur[1], cur[2], heading)
    session.player_position = (map_id, cur[0], cur[1], cur[2], heading)
    world.update_my_position_from_simulation((map_id, cur[0], cur[1], cur[2], heading))

    last_tick_t = clock()
    last_check_t = last_tick_t
    last_check_remaining = math.hypot(tx - cur[0], ty - cur[1])
    start_remaining = last_check_remaining or 1e-6
    # The server doesn't echo our own position back via update-object for
    # ordinary client-driven movement (confirmed live: my_server_position
    # sat at our login/spawn position throughout a real move_to, which would
    # otherwise have made the drift check below fire on *every* move once we
    # got >SERVER_DRIFT_MAX_YD from spawn). Only compare against a server
    # position that actually *changed* since this move started — a genuine
    # mid-flight correction (teleport, knockback), not stale pre-move data.
    initial_server_position = world.my_server_position

    def _stop_and_report(ok: bool, error: str | None, detail: dict) -> dict:
        send_stop(session, cur[0], cur[1], cur[2], heading)
        session.player_position = (map_id, cur[0], cur[1], cur[2], heading)
        world.update_my_position_from_simulation((map_id, cur[0], cur[1], cur[2], heading))
        detail = dict(detail, position=(cur[0], cur[1], cur[2]))
        return {"ok": ok, "error": error, "detail": detail}

    while True:
        # Do not send even a stop packet here: TrinityCore treats client
        # movement as authoritative, and a stop carrying a changed position
        # can move the unreleased corpse too.
        if _dead_before_release(world):
            return {"ok": False, "error": "dead before spirit release", "detail": {}}
        if stop_event.is_set():
            return _stop_and_report(False, "stopped", {})

        sleep(tick_interval)
        now = clock()

        if _dead_before_release(world):
            return {"ok": False, "error": "dead before spirit release", "detail": {}}

        target = get_target()
        if target is None:
            return _stop_and_report(False, "target lost", {})
        tx, ty, new_tz = target
        if new_tz is not None:
            tz = new_tz

        remaining = math.hypot(tx - cur[0], ty - cur[1])
        if remaining <= stop_distance:
            return _stop_and_report(True, None, {"distance": remaining})

        # Advance by elapsed wall time rather than the requested sleep. Thread
        # scheduling and packet work can make a tick longer than its interval;
        # using a fixed step then makes our reported coordinates lag movement
        # timestamps and invites the client to correct the character backwards.
        elapsed = max(0.0, now - last_tick_t)
        last_tick_t = now
        step = min(speed * elapsed, remaining)
        cur[0] += (tx - cur[0]) / remaining * step
        cur[1] += (ty - cur[1]) / remaining * step
        # No terrain height (no navmesh in v1): interpolate z by overall
        # progress toward the destination — for move_towards this converges
        # on the target's own (repeatedly refreshed) z as we close in.
        travelled_frac = min(1.0, 1.0 - (remaining - step) / start_remaining)
        cur[2] = sz + (tz - sz) * travelled_frac

        # Both sides of the comparison must be known — if initial_server_position
        # was None (our own CREATE block hadn't been processed by the recv
        # thread yet when this move started, a real race right after login),
        # that block's belated arrival must not look like "drift from
        # nothing" the moment it finally shows up.
        server_pos = world.my_server_position
        if (initial_server_position is not None and server_pos is not None
                and server_pos != initial_server_position and server_pos[0] == map_id):
            server_drift = math.hypot(server_pos[1] - cur[0], server_pos[2] - cur[1])
            if server_drift > SERVER_DRIFT_MAX_YD:
                return _stop_and_report(False, "stuck", {"reason": "server_drift", "drift": server_drift})

        if now - last_check_t >= STUCK_PROGRESS_WINDOW_S:
            progressed = last_check_remaining - remaining
            last_check_t = now
            last_check_remaining = remaining
            if progressed < STUCK_PROGRESS_MIN_YD:
                return _stop_and_report(False, "stuck", {"reason": "no_progress"})

        # Skip re-facing when this tick's move landed (near) exactly on the
        # target: atan2(~0, ~0) is numerically unstable there and would
        # swing `heading` to a spurious direction one tick before the
        # arrival check above catches it anyway.
        post_move_remaining = math.hypot(tx - cur[0], ty - cur[1])
        if post_move_remaining > 1e-3:
            new_heading = _heading(cur[0], cur[1], tx, ty)
            if abs(_angle_diff(new_heading, heading)) > math.radians(REFACE_DRIFT_DEG):
                heading = new_heading
                send_set_facing(session, cur[0], cur[1], cur[2], heading)
        send_heartbeat(session, cur[0], cur[1], cur[2], heading)
        session.player_position = (map_id, cur[0], cur[1], cur[2], heading)
        world.update_my_position_from_simulation((map_id, cur[0], cur[1], cur[2], heading))


class Mover:
    """Drives straight-line movement (move_to/move_towards) on a background
    thread, so stop() can interrupt an in-flight move from a different
    thread — needed once reflexes (UM-58) run concurrently with the LLM
    think loop. move_to()/move_towards() still block the calling thread
    until the move finishes (matching agent.actions.Action.execute()'s
    synchronous contract) — they just don't have to be the *only* thing
    that can end it.

    One Mover per session — see get_mover().
    """

    def __init__(self, session, world, clock=time.monotonic, sleep=time.sleep):
        self.session = session
        self.world = world
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def stop(self, timeout: float = 2.0) -> bool:
        """Signal any in-flight movement to stop and wait for it to exit.
        Returns True if a movement was actually running."""
        with self._lock:
            thread = self._thread
        if thread is None or not thread.is_alive():
            return False
        self._stop_event.set()
        thread.join(timeout=timeout)
        return True

    def move_to(self, x: float, y: float, z: float | None = None,
                stop_distance: float = ARRIVE_STOP_DISTANCE_YD,
                run_speed: float | None = None, tick_interval: float = TICK_INTERVAL_S) -> dict:
        return self._run(lambda: (x, y, z), stop_distance, run_speed, tick_interval)

    def move_towards(self, guid: int, stop_distance: float = ARRIVE_STOP_DISTANCE_YD,
                      run_speed: float | None = None, tick_interval: float = TICK_INTERVAL_S) -> dict:
        def get_target():
            obj = self.world.get_object(guid)
            if obj is None or obj.position is None:
                return None
            return obj.position[1], obj.position[2], obj.position[3]
        return self._run(get_target, stop_distance, run_speed, tick_interval)

    def _run(self, get_target, stop_distance: float, run_speed: float | None,
              tick_interval: float) -> dict:
        self.stop()  # a fresh move_to/move_towards replaces whatever was running
        stop_event = threading.Event()
        self._stop_event = stop_event
        result: dict = {}

        def worker():
            result.update(_simulate(self.session, self.world, get_target, stop_distance,
                                     run_speed, tick_interval, stop_event, self._clock, self._sleep))

        thread = threading.Thread(target=worker, daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        thread.join()
        return result


def get_mover(session, world) -> Mover:
    """One Mover per session, created lazily and cached on the session —
    actions.py's move_to/move_towards/stop_movement all need to reach the
    *same* Mover so stop_movement can cancel a move_to issued moments
    earlier from a different action call."""
    mover = getattr(session, "_mover", None)
    if mover is None:
        mover = Mover(session, world)
        session._mover = mover
    return mover
