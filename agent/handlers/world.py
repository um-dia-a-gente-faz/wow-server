"""World-state packets: object create/update/destroy, movement broadcasts, name queries."""

import struct
import zlib

from .. import names as nm
from .. import packets as pk
from .. import perception as per
from .. import update_fields as uo_fields
from .. import update_object as uo
from ..router import ROUTER
from ..update_object import (
    UPDATETYPE_OUT_OF_RANGE_OBJECTS,
    UPDATETYPE_NEAR_OBJECTS,
)
from . import death

from ..opcodes import (
    CMSG_NAME_QUERY,
    SMSG_NAME_QUERY_RESPONSE,
    CMSG_GAMEOBJECT_QUERY,
    SMSG_GAMEOBJECT_QUERY_RESPONSE,
    CMSG_CREATURE_QUERY,
    SMSG_CREATURE_QUERY_RESPONSE,
    SMSG_UPDATE_OBJECT,
    SMSG_DESTROY_OBJECT,
    MSG_MOVE_START_FORWARD,
    MSG_MOVE_START_BACKWARD,
    MSG_MOVE_STOP,
    MSG_MOVE_START_STRAFE_LEFT,
    MSG_MOVE_START_STRAFE_RIGHT,
    MSG_MOVE_STOP_STRAFE,
    MSG_MOVE_JUMP,
    MSG_MOVE_START_TURN_LEFT,
    MSG_MOVE_START_TURN_RIGHT,
    MSG_MOVE_STOP_TURN,
    MSG_MOVE_SET_RUN_MODE,
    MSG_MOVE_SET_WALK_MODE,
    MSG_MOVE_FALL_LAND,
    MSG_MOVE_START_SWIM,
    MSG_MOVE_STOP_SWIM,
    MSG_MOVE_SET_FACING,
    SMSG_MONSTER_MOVE,
    MSG_MOVE_HEARTBEAT,
    SMSG_COMPRESSED_UPDATE_OBJECT,
)

# MSG_MOVE_* broadcasts of another unit's movement (Opcodes.cpp:
# &WorldSession::HandleMovementOpcodes, WorldPackets::Movement::MoveUpdate) —
# packed guid + MovementInfo, no speeds/spline (agent.update_object.
# parse_movement_info). Excludes MSG_MOVE_TELEPORT/TELEPORT_ACK (different,
# ack-specific payload; UM-38) and cheat/rare opcodes not sent by a normal
# client.

MSG_MOVE_OPCODES = frozenset((
    MSG_MOVE_START_FORWARD, MSG_MOVE_START_BACKWARD, MSG_MOVE_STOP,
    MSG_MOVE_START_STRAFE_LEFT, MSG_MOVE_START_STRAFE_RIGHT, MSG_MOVE_STOP_STRAFE,
    MSG_MOVE_JUMP, MSG_MOVE_START_TURN_LEFT, MSG_MOVE_START_TURN_RIGHT, MSG_MOVE_STOP_TURN,
    MSG_MOVE_SET_RUN_MODE, MSG_MOVE_SET_WALK_MODE, MSG_MOVE_FALL_LAND,
    MSG_MOVE_START_SWIM, MSG_MOVE_STOP_SWIM, MSG_MOVE_SET_FACING, MSG_MOVE_HEARTBEAT,
))

# Name resolution (UM-35)


def handle_verify_world(ctx, payload):
    off = 0
    map_id = struct.unpack_from('<i', payload, off)[0]; off += 4
    px = struct.unpack_from('<f', payload, off)[0]; off += 4
    py = struct.unpack_from('<f', payload, off)[0]; off += 4
    pz = struct.unpack_from('<f', payload, off)[0]; off += 4
    orient = struct.unpack_from('<f', payload, off)[0]
    ctx.state.player_position = (map_id, px, py, pz, orient)
    ctx.state.world_state.set_my_map(map_id)


def handle_destroy_object(ctx, payload: bytes):
    # Object::DestroyForPlayer (Object.cpp): uint64 guid, uint8 onDeath.
    guid = pk.u64(payload, 0)
    ctx.state.world_state.remove_guids([guid])


def send_name_queries(ctx):
    """UM-35: send whatever agent.perception.WorldState.names has queued,
    up to its per-second budget. Called once per recv-loop tick (like
    the keepalive below), so new objects get their names resolved
    within a tick or two of showing up."""
    for kind, key, sample_guid in ctx.state.world_state.names.drain():
        if kind == "player":
            ctx.send(CMSG_NAME_QUERY, nm.build_name_query(key))
        elif kind == "creature":
            ctx.send(CMSG_CREATURE_QUERY, nm.build_creature_query(key, sample_guid))
        elif kind == "gameobject":
            ctx.send(CMSG_GAMEOBJECT_QUERY, nm.build_gameobject_query(key, sample_guid))


def handle_name_query_response(ctx, payload: bytes):
    try:
        data = nm.parse_name_query_response(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_NAME_QUERY_RESPONSE ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_name_query_response(data)


def handle_creature_query_response(ctx, payload: bytes):
    try:
        data = nm.parse_creature_query_response(payload)
    except (IndexError, struct.error, ValueError) as e:
        raise per.PerceptionParseError(f"malformed SMSG_CREATURE_QUERY_RESPONSE ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_creature_query_response(data)


def handle_gameobject_query_response(ctx, payload: bytes):
    try:
        data = nm.parse_gameobject_query_response(payload)
    except (IndexError, struct.error, ValueError) as e:
        raise per.PerceptionParseError(f"malformed SMSG_GAMEOBJECT_QUERY_RESPONSE ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_gameobject_query_response(data)


def handle_monster_move(ctx, payload: bytes):
    """SMSG_MONSTER_MOVE (0x0DD): an NPC's new destination/path (UM-64).
    Starts or replaces that object's spline-interpolation state in
    world_state — see agent/update_object.py::parse_monster_move and
    agent/perception/world.py::WorldState.apply_monster_move."""
    ctx.dump(SMSG_MONSTER_MOVE, payload)
    try:
        info = uo.parse_monster_move(payload)
    except (IndexError, struct.error, ValueError) as e:
        raise per.PerceptionParseError(
            f"malformed SMSG_MONSTER_MOVE payload ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_monster_move(info)


def handle_move_broadcast(ctx, opcode: int, payload: bytes):
    """A MSG_MOVE_* broadcast of another unit's movement (UM-64): packed
    guid + MovementInfo, no speeds/spline (agent.update_object.
    parse_movement_info) — see MSG_MOVE_OPCODES for which ones this
    covers and why."""
    ctx.dump(opcode, payload)
    try:
        guid, off = pk.unpack_packed_guid(payload, 0)
        move_info, off = uo.parse_movement_info(payload, off)
        if off != len(payload):
            raise ValueError(f"parsed {off} of {len(payload)} bytes ({len(payload) - off} leftover)")
    except (IndexError, struct.error, ValueError) as e:
        raise per.PerceptionParseError(
            f"malformed {opcode:#05x} payload ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_movement_info(guid, move_info)


def handle_update_object(ctx, opcode: int, payload: bytes):
    if opcode == SMSG_COMPRESSED_UPDATE_OBJECT:
        unc_size = pk.u32(payload, 0)
        data = zlib.decompress(payload[4:])
        if len(data) != unc_size:
            raise per.PerceptionParseError(
                f"inflated to {len(data)} B, header said {unc_size} B")
    else:
        data = payload
    ctx.dump(opcode, data)
    parse_update_object(ctx, data)


def parse_update_object(ctx, data: bytes):
    """Parse an (inflated) SMSG_UPDATE_OBJECT payload into world state.

    Full block framing + movement parsing lives in agent/update_object.py
    (pure function, no I/O); field mapping in agent/update_fields.py. This
    wires the result into WorldState (create/merge/remove) and, for
    blocks about our own player, mirrors position and a few stats onto
    the session directly for cheap access without going through
    world_state.

    Raises PerceptionParseError on truncated or malformed data — the
    caller's per-packet safety net (_dispatch_guarded) drops just this
    packet and keeps the connection. Anything recorded from earlier
    packets is kept.
    """
    try:
        blocks = uo.parse_update_object(data)
    except (IndexError, struct.error, ValueError) as e:
        raise per.PerceptionParseError(
            f"malformed update-object payload ({len(data)} B): {e}") from e
    for block in blocks:
        if block.update_type in (UPDATETYPE_OUT_OF_RANGE_OBJECTS, UPDATETYPE_NEAR_OBJECTS):
            ctx.state.world_state.remove_guids(block.guids)
            continue
        ctx.state.world_state.update_object(block)
        if block.guid == ctx.state.player_guid:
            sync_self_from_block(ctx, block)


def sync_self_from_block(ctx, block):
    """Mirror our own object's position/stats from world_state onto the
    session for cheap direct access (session.player_position, .level, ...)."""
    movement = block.movement or {}
    if "x" in movement:
        map_id = ctx.state.player_position[0] if ctx.state.player_position else ctx.state.world_state.my_map
        ctx.state.player_position = (map_id, movement["x"], movement["y"], movement["z"], movement.get("o", 0.0))
    me = ctx.state.world_state.get_my_object()
    if me is not None:
        if me.level is not None:
            ctx.state.level = me.level
        xp = me.raw_fields.get(uo_fields.PLAYER_XP)
        if xp is not None:
            ctx.state.xp = xp
        next_xp = me.raw_fields.get(uo_fields.PLAYER_NEXT_LEVEL_XP)
        if next_xp is not None:
            ctx.state.next_level_xp = next_xp
        coinage = me.raw_fields.get(uo_fields.PLAYER_FIELD_COINAGE)
        coinage = me.raw_fields.get(uo_fields.PLAYER_FIELD_COINAGE)
        if coinage is None:
            # Zero-value fields aren't sent over the wire, so a coinage of
            # 0 copper looks identical to "field absent". Once the self
            # object exists we know the field would be present for any
            # nonzero value, so treat "missing" as 0 instead of leaving
            # ctx.state.coinage as None.
            coinage = 0
        if ctx.state.coinage is not None and coinage != ctx.state.coinage:
            ctx.state.record_event("money_changed", old=ctx.state.coinage, new=coinage,
                                delta=coinage - ctx.state.coinage)
        ctx.state.coinage = coinage
        death.check_death_transition(ctx, me)


def _update_object_for(opcode):
    def handler(ctx, payload):
        handle_update_object(ctx, opcode, payload)
    return handler


def _move_broadcast_for(opcode):
    def handler(ctx, payload):
        handle_move_broadcast(ctx, opcode, payload)
    return handler


ROUTER.register_all({
    SMSG_UPDATE_OBJECT: _update_object_for(SMSG_UPDATE_OBJECT),
    SMSG_COMPRESSED_UPDATE_OBJECT: _update_object_for(SMSG_COMPRESSED_UPDATE_OBJECT),
    SMSG_DESTROY_OBJECT: handle_destroy_object,
    SMSG_NAME_QUERY_RESPONSE: handle_name_query_response,
    SMSG_CREATURE_QUERY_RESPONSE: handle_creature_query_response,
    SMSG_GAMEOBJECT_QUERY_RESPONSE: handle_gameobject_query_response,
    SMSG_MONSTER_MOVE: handle_monster_move,
})
ROUTER.register_all({op: _move_broadcast_for(op) for op in MSG_MOVE_OPCODES})
ROUTER.on_tick(send_name_queries)
