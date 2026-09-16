#!/usr/bin/env python3
"""Pure parser for SMSG_UPDATE_OBJECT / SMSG_COMPRESSED_UPDATE_OBJECT payloads
(WoW 3.3.5a, build 12340).

No I/O, no WorldState — agent/session.py wires the result into WorldState
(UM-34). Field mapping of VALUES_UPDATE (uint32 slot -> named unit/object
field) is UM-33; here it's only skipped/captured as a raw byte range.

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


class UnhandledMovementFlags(Exception):
    """A movement block used a conditional path this parser doesn't (yet)
    handle correctly — currently just MOVEMENTFLAG_SPLINE_ENABLED (spline
    data, Object.cpp's WriteCreateObjectSplineDataBlock isn't implemented
    here). Raised instead of guessing and silently corrupting the offset for
    every block after it; agent/session.py's per-packet safety net (UM-30)
    drops the whole packet and logs this, rate-limited, without killing the
    connection."""

    def __init__(self, move_flags: int):
        self.move_flags = move_flags
        super().__init__(f"unhandled movement flags {move_flags:#010x}")


@dataclass
class UpdateBlock:
    update_type: int
    guid: int = 0
    object_type: int | None = None             # TYPEID_*; only CREATE_OBJECT[2]
    movement: dict | None = None                # only MOVEMENT / CREATE_OBJECT[2]
    values_raw: tuple[int, int] | None = None   # (start, end) byte range of the VALUES_UPDATE within `data`; only VALUES / CREATE_OBJECT[2]
    guids: list = field(default_factory=list)   # only OUT_OF_RANGE_OBJECTS / NEAR_OBJECTS


def parse_update_object(data: bytes) -> list[UpdateBlock]:
    """Parse an already-inflated SMSG_UPDATE_OBJECT payload into UpdateBlocks.

    Layout (UpdateData.cpp::BuildPacket, UpdateData.h):
        uint32 block_count
        block*: uint8 update_type, ...  (shape depends on update_type)

    Raises IndexError/struct.error on truncated data, ValueError on an
    update_type outside 0-5, UnhandledMovementFlags on an unimplemented
    movement conditional — the caller (agent/session.py) turns the first two
    into PerceptionParseError and lets the third propagate as itself.
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
            start = off
            off = _skip_values_update(data, off)
            blocks.append(UpdateBlock(update_type=update_type, guid=guid, values_raw=(start, off)))
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
            start = off
            off = _skip_values_update(data, off)
            blocks.append(UpdateBlock(update_type=update_type, guid=guid, object_type=object_type,
                                       movement=movement, values_raw=(start, off)))
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
        # Unit::BuildMovementPacket (Unit.cpp)
        move_flags = pk.u32(data, off); off += 4
        move_flags2 = pk.u16(data, off); off += 2
        off += 4  # time (uint32) — server tick count, not needed by callers yet
        x = pk.f32(data, off); off += 4
        y = pk.f32(data, off); off += 4
        z = pk.f32(data, off); off += 4
        o = pk.f32(data, off); off += 4
        info.update(move_flags=move_flags, move_flags2=move_flags2, x=x, y=y, z=z, o=o)

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

        info["speeds"] = struct.unpack_from('<9f', data, off); off += 4 * 9

        if move_flags & MOVEMENTFLAG_SPLINE_ENABLED:
            # WorldPackets::Movement::CommonMovement::WriteCreateObjectSplineDataBlock
            # not implemented — the offset of anything after this block in
            # the same packet is now unknowable.
            raise UnhandledMovementFlags(move_flags)

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


def _skip_values_update(data: bytes, off: int) -> int:
    """Skip a VALUES_UPDATE without mapping fields (UM-33 does that):
    uint8 mask_block_count, mask_block_count little-endian uint32 mask words,
    then one uint32 per set bit in ascending bit order.
    (UpdateMask.h::UpdateMaskPacketBuilder::AppendToPacket, Object.cpp::BuildValuesUpdate)
    """
    mask_block_count = data[off]; off += 1
    popcount = 0
    for _ in range(mask_block_count):
        word = pk.u32(data, off); off += 4
        popcount += bin(word).count('1')
    off += 4 * popcount
    return off
