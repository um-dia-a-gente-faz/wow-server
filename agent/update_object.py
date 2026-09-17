#!/usr/bin/env python3
"""Pure parser for SMSG_UPDATE_OBJECT / SMSG_COMPRESSED_UPDATE_OBJECT payloads
(WoW 3.3.5a, build 12340).

No I/O, no WorldState — agent/session.py wires the result into WorldState
(UM-34). VALUES_UPDATE parses into a raw {field_index: uint32} dict here;
mapping that to named, typed unit/object fields is agent/update_fields.py.

Every constant below is copied from TrinityCore branch `3.3.5` and cited by
file. Verify against that source, not against docs/NEXT-AGENT-HANDOFF.md
(known wrong per UM-32's card) if the two disagree.
"""

import struct
from dataclasses import dataclass, field

from . import packets as pk

# OBJECT_UPDATE_TYPE
# src/server/game/Entities/Object/Updates/UpdateData.h
UPDATETYPE_VALUES = 0
UPDATETYPE_MOVEMENT = 1
UPDATETYPE_CREATE_OBJECT = 2
UPDATETYPE_CREATE_OBJECT2 = 3
UPDATETYPE_OUT_OF_RANGE_OBJECTS = 4
UPDATETYPE_NEAR_OBJECTS = 5

# OBJECT_UPDATE_FLAGS
# src/server/game/Entities/Object/Updates/UpdateData.h
UPDATEFLAG_SELF = 0x0001
UPDATEFLAG_TRANSPORT = 0x0002
UPDATEFLAG_HAS_TARGET = 0x0004
UPDATEFLAG_UNKNOWN = 0x0008
UPDATEFLAG_LOWGUID = 0x0010
UPDATEFLAG_LIVING = 0x0020
UPDATEFLAG_STATIONARY_POSITION = 0x0040
UPDATEFLAG_VEHICLE = 0x0080
UPDATEFLAG_POSITION = 0x0100
UPDATEFLAG_ROTATION = 0x0200
UPDATEFLAG_NO_BIRTH_ANIM = 0x0400

# TypeID (the byte following the packed GUID in CREATE_OBJECT[2] blocks)
# src/server/game/Entities/Object/ObjectGuid.h
TYPEID_OBJECT = 0
TYPEID_ITEM = 1
TYPEID_CONTAINER = 2
TYPEID_UNIT = 3
TYPEID_PLAYER = 4
TYPEID_GAMEOBJECT = 5
TYPEID_DYNAMICOBJECT = 6
TYPEID_CORPSE = 7

OBJECT_TYPE_NAMES = {
    TYPEID_OBJECT: "object",
    TYPEID_ITEM: "item",
    TYPEID_CONTAINER: "container",
    TYPEID_UNIT: "unit",
    TYPEID_PLAYER: "player",
    TYPEID_GAMEOBJECT: "gameobject",
    TYPEID_DYNAMICOBJECT: "dynobject",
    TYPEID_CORPSE: "corpse",
}

# MovementFlags (uint32) — only the bits this parser needs to branch on.
# src/server/game/Entities/Unit/UnitDefines.h
MOVEMENTFLAG_ONTRANSPORT = 0x00000200
MOVEMENTFLAG_FALLING = 0x00001000
MOVEMENTFLAG_SWIMMING = 0x00200000
MOVEMENTFLAG_FLYING = 0x02000000
MOVEMENTFLAG_SPLINE_ELEVATION = 0x04000000
MOVEMENTFLAG_SPLINE_ENABLED = 0x08000000

# MovementFlags2 (uint16 on the wire, despite being declared uint32 in the enum)
# src/server/game/Entities/Unit/UnitDefines.h
MOVEMENTFLAG2_ALWAYS_ALLOW_PITCHING = 0x00000020
MOVEMENTFLAG2_INTERPOLATED_MOVEMENT = 0x00000400

# MoveSplineFlag (Movement::MoveSplineFlag::eFlags)
# src/server/game/Movement/Spline/MoveSplineFlag.h
SPLINEFLAG_PARABOLIC = 0x00000800
SPLINEFLAG_FINAL_POINT = 0x00008000
SPLINEFLAG_FINAL_TARGET = 0x00010000
SPLINEFLAG_FINAL_ANGLE = 0x00020000
SPLINEFLAG_CATMULLROM = 0x00040000
SPLINEFLAG_CYCLIC = 0x00080000
SPLINEFLAG_ANIMATION = 0x00200000
SPLINEFLAG_FLYING = 0x00002000
SPLINEFLAG_MASK_CATMULLROM = SPLINEFLAG_FLYING | SPLINEFLAG_CATMULLROM

# Movement::MonsterMoveType (SMSG_MONSTER_MOVE's Face byte)
# src/server/game/Movement/Spline/MovementTypedefs.h
MONSTER_MOVE_NORMAL = 0
MONSTER_MOVE_STOP = 1
MONSTER_MOVE_FACING_SPOT = 2
MONSTER_MOVE_FACING_TARGET = 3
MONSTER_MOVE_FACING_ANGLE = 4


@dataclass
class UpdateBlock:
    update_type: int
    guid: int = 0
    object_type: int | None = None             # TYPEID_*; only CREATE_OBJECT[2]
    movement: dict | None = None                # only MOVEMENT / CREATE_OBJECT[2]
    fields: dict | None = None                  # {field_index: uint32}; only VALUES / CREATE_OBJECT[2] — see agent/update_fields.py to decode
    guids: list = field(default_factory=list)   # only OUT_OF_RANGE_OBJECTS / NEAR_OBJECTS


def parse_update_object(data: bytes) -> list[UpdateBlock]:
    """Parse an already-inflated SMSG_UPDATE_OBJECT payload into UpdateBlocks.

    Layout (UpdateData.cpp::BuildPacket, UpdateData.h):
        uint32 block_count
        block*: uint8 update_type, ...  (shape depends on update_type)

    Raises IndexError/struct.error on truncated data, ValueError on an
    update_type outside 0-5 — the caller (agent/session.py) turns both into
    PerceptionParseError.
    """
    off = 0
    block_count = pk.u32(data, off); off += 4
    blocks = []
    for _ in range(block_count):
        update_type = data[off]; off += 1

        if update_type in (UPDATETYPE_OUT_OF_RANGE_OBJECTS, UPDATETYPE_NEAR_OBJECTS):
            guid_count = pk.u32(data, off); off += 4
            guids = []
            for _ in range(guid_count):
                guid, off = pk.unpack_packed_guid(data, off)
                guids.append(guid)
            blocks.append(UpdateBlock(update_type=update_type, guids=guids))
            continue

        if update_type == UPDATETYPE_VALUES:
            guid, off = pk.unpack_packed_guid(data, off)
            fields, off = _parse_values_update(data, off)
            blocks.append(UpdateBlock(update_type=update_type, guid=guid, fields=fields))
            continue

        if update_type == UPDATETYPE_MOVEMENT:
            guid, off = pk.unpack_packed_guid(data, off)
            movement, off = _parse_movement_update(data, off)
            blocks.append(UpdateBlock(update_type=update_type, guid=guid, movement=movement))
            continue

        if update_type in (UPDATETYPE_CREATE_OBJECT, UPDATETYPE_CREATE_OBJECT2):
            # Object::BuildCreateUpdateBlockForPlayer (Object.cpp):
            # packed guid, uint8 objectTypeId, movement update, values update.
            guid, off = pk.unpack_packed_guid(data, off)
            object_type = data[off]; off += 1
            movement, off = _parse_movement_update(data, off)
            fields, off = _parse_values_update(data, off)
            blocks.append(UpdateBlock(update_type=update_type, guid=guid, object_type=object_type,
                                       movement=movement, fields=fields))
            continue

        raise ValueError(f"unknown update_type {update_type} at offset {off - 1}")

    if off != len(data):
        # A real SMSG_UPDATE_OBJECT payload is exactly block_count blocks,
        # back to back, no padding (UpdateData::BuildPacket). Leftover bytes
        # mean a block above under-consumed — a parser bug, not something to
        # silently ignore.
        raise ValueError(f"parsed {off} of {len(data)} bytes after {block_count} block(s) "
                          f"({len(data) - off} leftover)")
    return blocks


def _parse_movement_update(data: bytes, off: int) -> tuple[dict, int]:
    """The movement portion of a MOVEMENT or CREATE_OBJECT[2] block.

    Object::BuildMovementUpdate (Object.cpp): uint16 update_flags, then at
    most one position form — LIVING, else POSITION, else STATIONARY_POSITION
    (checked in that order) — then trailing flag-gated fields, each written
    only if its bit is set, in this fixed order: UNKNOWN, LOWGUID, HAS_TARGET,
    TRANSPORT, VEHICLE, ROTATION.
    """
    update_flags = pk.u16(data, off); off += 2
    info = {"update_flags": update_flags}

    if update_flags & UPDATEFLAG_LIVING:
        # Unit::BuildMovementPacket (Unit.cpp): MovementInfo, then 9 speeds,
        # then spline data if MOVEMENTFLAG_SPLINE_ENABLED.
        move_info, off = parse_movement_info(data, off)
        info.update(move_info)

        info["speeds"] = struct.unpack_from('<9f', data, off); off += 4 * 9

        if move_info["move_flags"] & MOVEMENTFLAG_SPLINE_ENABLED:
            info["spline"], off = _parse_create_object_spline_block(data, off)

    elif update_flags & UPDATEFLAG_POSITION:
        # packed transport guid (a lone 0x00 byte when not on a transport,
        # itself a valid packed GUID for 0), x/y/z, then a *second* x/y/z
        # (transport-relative offset if on a transport, the same absolute
        # position again otherwise), o, then o again (ditto).
        transport_guid, off = pk.unpack_packed_guid(data, off)
        x = pk.f32(data, off); off += 4
        y = pk.f32(data, off); off += 4
        z = pk.f32(data, off); off += 4
        off += 4 * 3  # second x/y/z (transport offset, or a duplicate — not needed yet)
        o = pk.f32(data, off); off += 4
        off += 4  # second o (ditto)
        info.update(transport_guid=transport_guid, x=x, y=y, z=z, o=o)

    elif update_flags & UPDATEFLAG_STATIONARY_POSITION:
        x = pk.f32(data, off); off += 4
        y = pk.f32(data, off); off += 4
        z = pk.f32(data, off); off += 4
        o = pk.f32(data, off); off += 4
        info.update(x=x, y=y, z=z, o=o)

    if update_flags & UPDATEFLAG_UNKNOWN:
        off += 4
    if update_flags & UPDATEFLAG_LOWGUID:
        off += 4
    if update_flags & UPDATEFLAG_HAS_TARGET:
        target_guid, off = pk.unpack_packed_guid(data, off)
        info["target_guid"] = target_guid
    if update_flags & UPDATEFLAG_TRANSPORT:
        off += 4
    if update_flags & UPDATEFLAG_VEHICLE:
        off += 4 + 4
    if update_flags & UPDATEFLAG_ROTATION:
        off += 8

    return info, off


def _parse_create_object_spline_block(data: bytes, off: int) -> tuple[dict, int]:
    """Spline data appended to a LIVING movement block when
    MOVEMENTFLAG_SPLINE_ENABLED is set (a moving NPC or player, mid-CREATE).

    WorldPackets::Movement::CommonMovement::WriteCreateObjectSplineDataBlock
    (MovementPackets.cpp): uint32 spline_flags, then at most one facing form
    gated by Mask_Final_Facing (Final_Angle: float | Final_Target: uint64 raw
    guid, NOT packed | Final_Point: float x,y,z), int32 time_passed, uint32
    duration, uint32 spline_id, two unused float duration modifiers (always
    1.0f on the wire, not meaningful to callers), float vertical_acceleration,
    uint32 effect_start_time, uint32 point_count + that many raw float x,y,z
    points (MoveSpline::getPath() verbatim — unlike SMSG_MONSTER_MOVE, this
    block never uses the packed-delta compression), uint8 evaluation mode
    (Spline::EvaluationMode: 0 linear, 1 catmullrom, 2 unused bezier3), then
    float x,y,z final destination (zero if the spline is cyclic).
    """
    spline_flags = pk.u32(data, off); off += 4
    info = {"spline_flags": spline_flags}

    if spline_flags & SPLINEFLAG_FINAL_ANGLE:
        info["final_angle"] = pk.f32(data, off); off += 4
    elif spline_flags & SPLINEFLAG_FINAL_TARGET:
        info["final_target_guid"] = pk.u64(data, off); off += 8
    elif spline_flags & SPLINEFLAG_FINAL_POINT:
        info["final_point"] = (pk.f32(data, off), pk.f32(data, off + 4), pk.f32(data, off + 8))
        off += 12

    info["time_passed"] = struct.unpack_from('<i', data, off)[0]; off += 4
    info["duration"] = pk.u32(data, off); off += 4
    info["spline_id"] = pk.u32(data, off); off += 4
    off += 4 * 2  # duration_mod, next_duration_mod (float, always 1.0 on the wire)
    info["vertical_acceleration"] = pk.f32(data, off); off += 4
    info["effect_start_time"] = pk.u32(data, off); off += 4

    point_count = pk.u32(data, off); off += 4
    points = []
    for _ in range(point_count):
        points.append((pk.f32(data, off), pk.f32(data, off + 4), pk.f32(data, off + 8)))
        off += 12
    info["points"] = points

    info["mode"] = data[off]; off += 1
    info["destination"] = (pk.f32(data, off), pk.f32(data, off + 4), pk.f32(data, off + 8))
    off += 12

    return info, off


def parse_movement_info(data: bytes, off: int) -> tuple[dict, int]:
    """The shared MovementInfo wire format (ByteBuffer operator<</>> for
    MovementInfo, MovementPackets.cpp): uint32 move_flags, uint16
    move_flags2, uint32 time, float x,y,z,o, then conditional
    transport/pitch/fall/jump/spline-elevation fields.

    This is the prefix _parse_movement_update's LIVING branch uses (which
    then appends 9 speeds and, if MOVEMENTFLAG_SPLINE_ENABLED, spline data —
    see _parse_create_object_spline_block) and also the *entire* payload
    (after a packed mover GUID) of a broadcast MSG_MOVE_* packet: those never
    carry speeds or spline data (WorldPackets::Movement::MoveUpdate::Write
    just writes guid + MovementInfo, nothing more).
    """
    move_flags = pk.u32(data, off); off += 4
    move_flags2 = pk.u16(data, off); off += 2
    off += 4  # time (uint32) — server tick count, not needed by callers yet
    x = pk.f32(data, off); off += 4
    y = pk.f32(data, off); off += 4
    z = pk.f32(data, off); off += 4
    o = pk.f32(data, off); off += 4
    info = {"move_flags": move_flags, "move_flags2": move_flags2, "x": x, "y": y, "z": z, "o": o}

    if move_flags & MOVEMENTFLAG_ONTRANSPORT:
        transport_guid, off = pk.unpack_packed_guid(data, off)
        tx = pk.f32(data, off); off += 4
        ty = pk.f32(data, off); off += 4
        tz = pk.f32(data, off); off += 4
        to = pk.f32(data, off); off += 4
        off += 4  # transport.time (uint32)
        off += 1  # transport.seat (int8)
        info.update(transport_guid=transport_guid, tx=tx, ty=ty, tz=tz, to=to)
        if move_flags2 & MOVEMENTFLAG2_INTERPOLATED_MOVEMENT:
            off += 4  # transport.time2 (uint32)

    if (move_flags & (MOVEMENTFLAG_SWIMMING | MOVEMENTFLAG_FLYING)) \
            or (move_flags2 & MOVEMENTFLAG2_ALWAYS_ALLOW_PITCHING):
        info["pitch"] = pk.f32(data, off); off += 4

    off += 4  # fall_time (uint32)

    if move_flags & MOVEMENTFLAG_FALLING:
        off += 4 * 4  # jump: zspeed, sinAngle, cosAngle, xyspeed

    if move_flags & MOVEMENTFLAG_SPLINE_ELEVATION:
        off += 4  # splineElevation

    return info, off


def _unpack_xyz_delta(word: int) -> tuple[float, float, float]:
    """Inverse of ByteBuffer::appendPackXYZ (ByteBuffer.h): 11/11/10-bit
    signed fields (x, y, z, each a quarter-yard unit), packed into a uint32
    low-to-high. Used for SMSG_MONSTER_MOVE's intermediate waypoints, which
    are sent as a delta from the midpoint of the spline's start and end
    rather than an absolute position."""
    x_bits = word & 0x7FF
    y_bits = (word >> 11) & 0x7FF
    z_bits = (word >> 22) & 0x3FF
    if x_bits & 0x400:
        x_bits -= 0x800
    if y_bits & 0x400:
        y_bits -= 0x800
    if z_bits & 0x200:
        z_bits -= 0x400
    return (x_bits * 0.25, y_bits * 0.25, z_bits * 0.25)


def parse_monster_move(payload: bytes) -> dict:
    """SMSG_MONSTER_MOVE (0x0DD) — an NPC's new destination/path, sent
    whenever a creature starts, retargets, or resumes a spline. Does NOT
    handle the SMSG_MONSTER_MOVE_TRANSPORT variant (0x2AE, transport-relative
    movement e.g. NPCs riding a boat) — a different, unimplemented layout;
    callers must not feed that opcode's payload in here.

    WorldPackets::Movement::MonsterMove::Write /
    MonsterMove::InitializeSplineData (MovementPackets.cpp): packed guid
    mover, uint8 vehicle_exit_voluntary, float x,y,z (starting position, no
    orientation), then MovementMonsterSpline: uint32 spline_id, uint8
    move_type (MonsterMoveType). If move_type != MONSTER_MOVE_STOP, a
    MovementSpline follows: an optional facing form gated by move_type
    (FACING_TARGET: uint64 raw guid | FACING_ANGLE: float | FACING_SPOT:
    float x,y,z), uint32 flags, an optional AnimTierTransition (uint8 +
    uint32) if flags has SPLINEFLAG_ANIMATION, uint32 move_time, an optional
    JumpExtraData (float + uint32) if flags has SPLINEFLAG_PARABOLIC, uint32
    point_count, then point_count points: raw float x,y,z each if flags has
    the CatmullRom mask (Flying|Catmullrom), otherwise one raw float x,y,z
    (the final destination) followed by point_count-1 packed-delta uint32s
    (each a compressed offset from the midpoint of start and destination —
    see _unpack_xyz_delta).

    Raises IndexError/struct.error on truncated data, ValueError if bytes
    are left over after parsing (a parser bug, not something to silently
    ignore — mirrors parse_update_object's own check).
    """
    off = 0
    mover_guid, off = pk.unpack_packed_guid(payload, off)
    vehicle_exit_voluntary = payload[off]; off += 1
    pos = (pk.f32(payload, off), pk.f32(payload, off + 4), pk.f32(payload, off + 8)); off += 12

    spline_id = pk.u32(payload, off); off += 4
    move_type = payload[off]; off += 1

    info = {
        "mover_guid": mover_guid,
        "vehicle_exit_voluntary": bool(vehicle_exit_voluntary),
        "pos": pos,
        "spline_id": spline_id,
        "move_type": move_type,
    }

    if move_type != MONSTER_MOVE_STOP:
        if move_type == MONSTER_MOVE_FACING_TARGET:
            info["face_guid"] = pk.u64(payload, off); off += 8
        elif move_type == MONSTER_MOVE_FACING_ANGLE:
            info["face_direction"] = pk.f32(payload, off); off += 4
        elif move_type == MONSTER_MOVE_FACING_SPOT:
            info["face_spot"] = (pk.f32(payload, off), pk.f32(payload, off + 4), pk.f32(payload, off + 8))
            off += 12

        flags = pk.u32(payload, off); off += 4
        info["flags"] = flags

        if flags & SPLINEFLAG_ANIMATION:
            info["anim_tier"] = payload[off]; off += 1
            info["anim_start_time"] = pk.u32(payload, off); off += 4

        info["move_time"] = pk.u32(payload, off); off += 4

        if flags & SPLINEFLAG_PARABOLIC:
            info["jump_gravity"] = pk.f32(payload, off); off += 4
            info["jump_start_time"] = pk.u32(payload, off); off += 4

        point_count = pk.u32(payload, off); off += 4
        points = []
        if flags & SPLINEFLAG_MASK_CATMULLROM:
            for _ in range(point_count):
                points.append((pk.f32(payload, off), pk.f32(payload, off + 4), pk.f32(payload, off + 8)))
                off += 12
            destination = points[-1] if points else pos
        elif point_count > 0:
            destination = (pk.f32(payload, off), pk.f32(payload, off + 4), pk.f32(payload, off + 8)); off += 12
            points.append(destination)
            mid = tuple((pos[i] + destination[i]) / 2 for i in range(3))
            for _ in range(point_count - 1):
                delta = _unpack_xyz_delta(pk.u32(payload, off)); off += 4
                points.append(tuple(mid[i] - delta[i] for i in range(3)))
        else:
            destination = pos

        info["points"] = points
        info["destination"] = destination

    if off != len(payload):
        raise ValueError(f"parsed {off} of {len(payload)} bytes ({len(payload) - off} leftover)")
    return info


def _parse_values_update(data: bytes, off: int) -> tuple[dict, int]:
    """A VALUES_UPDATE: uint8 mask_block_count, then mask_block_count
    little-endian uint32 mask words *all together*, then one raw uint32 per
    set bit (ascending field index) *all together* — the values are NOT
    interleaved per mask word. UpdateMaskPacketBuilder::AppendToPacket
    (UpdateMask.h) writes only the header+mask; Object::BuildValuesUpdate
    (Object.cpp) builds the per-field values into a separate ByteBuffer first
    and appends it after the whole mask.

    Returns {field_index: raw_uint32}. Values are the raw wire slot — float
    fields need reinterpreting and GUID fields span two slots; see
    agent/update_fields.py::decode_fields for that mapping.
    """
    mask_block_count = data[off]; off += 1
    set_bits = []
    for word_index in range(mask_block_count):
        word = pk.u32(data, off); off += 4
        base = word_index * 32
        for bit in range(32):
            if word & (1 << bit):
                set_bits.append(base + bit)
    fields = {}
    for index in set_bits:
        fields[index] = pk.u32(data, off); off += 4
    return fields, off
