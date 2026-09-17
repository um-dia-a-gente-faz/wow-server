#!/usr/bin/env python3
"""MovementInfo wire writer — the client-side counterpart of
agent.update_object.parse_movement_info: the same packed-guid + MovementInfo
shape, but built for *sending* instead of parsed from a received packet.
Used by every MSG_MOVE_* the agent sends (agent/actions.py's `face` today;
straight-line movement reuses this in UM-38).

Verified against TrinityCore branch `3.3.5`:
  src/server/game/Server/Packets/MovementPackets.cpp
    (WorldPackets::Movement::ClientPlayerMovement::Read: packed guid, then
    the MovementInfo operator>>; symmetric with operator<< — see
    docs/PROTOCOL-NOTES.md's SMSG_MESSAGECHAT-adjacent movement section)
  src/server/game/Handlers/MovementHandler.cpp
    (HandleMovementOpcodes/HandleMovementOpcode — MSG_MOVE_SET_FACING and
    every other MSG_MOVE_* the client sends share this one wire shape)
  src/server/game/Server/Protocol/Opcodes.h (MSG_MOVE_SET_FACING = 0x0DA)
"""

import struct
import time

from . import packets as pk
from .update_object import (
    MOVEMENTFLAG_ONTRANSPORT,
    MOVEMENTFLAG_FALLING,
    MOVEMENTFLAG_SWIMMING,
    MOVEMENTFLAG_FLYING,
    MOVEMENTFLAG_SPLINE_ELEVATION,
    MOVEMENTFLAG2_ALWAYS_ALLOW_PITCHING,
)

MSG_MOVE_SET_FACING = 0x0DA


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
