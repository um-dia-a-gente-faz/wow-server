"""Death and corpse packets (UM-43), plus the health>0 -> 0 death-event detection."""

import struct
import time

from .. import packets as pk
from ..router import ROUTER

# Death/resurrection (UM-43). Verified against TrinityCore branch `3.3.5`:
#   src/server/game/Server/Protocol/Opcodes.h
#   src/server/game/Server/Packets/MiscPackets.h/.cpp (RepopRequest,
#     ReclaimCorpse, CorpseReclaimDelay, DeathReleaseLoc)
#   src/server/game/Server/Packets/QueryPackets.h/.cpp (CorpseLocation —
#     MSG_CORPSE_QUERY is the same opcode both ways: an empty client request,
#     a populated server response)
#   src/server/game/Handlers/MiscHandler.cpp (HandleRepopRequest,
#     HandleReclaimCorpse), src/server/game/Handlers/NPCHandler.cpp
#     (HandleSpiritHealerActivateOpcode)
#   src/server/game/Entities/Player/Player.h (enum PlayerFlags —
#     PLAYER_FLAGS_GHOST = 0x10; CORPSE_RECLAIM_RADIUS = 39)
from ..opcodes import (
    CMSG_REPOP_REQUEST,  # noqa: F401  (tests reach it through this module)
    CMSG_RECLAIM_CORPSE,  # noqa: F401  (tests reach it through this module)
    MSG_CORPSE_QUERY,
    CMSG_SPIRIT_HEALER_ACTIVATE,  # noqa: F401  (tests reach it through this module)
    SMSG_CORPSE_RECLAIM_DELAY,
    SMSG_DEATH_RELEASE_LOC,
)


def check_death_transition(ctx, me):
    """Emit a 'death' event on the health>0 -> 0 transition (UM-43).
    There is no SMSG_PLAYER_DEAD in TrinityCore 3.3.5 — death is only
    observable via this VALUES update (UNIT_FIELD_HEALTH -> 0), per
    Player::Kill (Player.cpp). Re-arms once health is next seen > 0
    (after a resurrect), so a later death emits again."""
    if me.health is None:
        return
    if me.health == 0:
        if ctx.state._was_alive:
            ctx.state._was_alive = False
            ctx.state.record_event("death", killer_guid=infer_killer_guid(ctx),
                                position=ctx.state.player_position)
    elif me.health > 0:
        ctx.state._was_alive = True


def infer_killer_guid(ctx):
    """Best-effort killer GUID for the 'death' event: the most recent
    combat event (in agent.events, newest last) whose victim was us.
    SMSG_ATTACKERSTATEUPDATE/SMSG_PARTYKILLLOG (UM-39) are the only
    signals available — TrinityCore doesn't otherwise tell the client
    who landed the killing blow. None if nothing matches (e.g. died to
    fall damage/environment, or the killing packet hasn't arrived yet)."""
    for e in reversed(ctx.state.events):
        if e.get("kind") == "party_kill" and e.get("victim_guid") == ctx.state.player_guid:
            return e.get("killer_guid")
        if e.get("kind") == "attacker_state_update" and e.get("victim_guid") == ctx.state.player_guid:
            return e.get("attacker_guid")
    return None


def handle_corpse_reclaim_delay(ctx, payload: bytes):
    """SMSG_CORPSE_RECLAIM_DELAY (Player::SendCorpseReclaimDelay,
    Player.cpp): uint32 Remaining, milliseconds until CMSG_RECLAIM_CORPSE
    will be accepted (server enforces this too; kept here so
    reclaim_corpse's check() can fail fast instead of round-tripping)."""
    remaining_ms = pk.u32(payload, 0)
    ctx.state.corpse_reclaim_ready_at = time.monotonic() + remaining_ms / 1000.0


def handle_death_release_loc(ctx, payload: bytes):
    """SMSG_DEATH_RELEASE_LOC (WorldPackets::Misc::DeathReleaseLoc,
    MiscPackets.cpp): int32 MapID, float x, y, z. Player::ResurrectPlayer
    also sends this opcode with MapID=-1 as a "clear the release marker"
    signal (no real position follows it in that case) — skip storing
    that sentinel rather than mistake it for a graveyard position."""
    map_id = struct.unpack_from('<i', payload, 0)[0]
    if map_id < 0:
        return
    x, y, z = struct.unpack_from('<3f', payload, 4)
    ctx.state.graveyard_position = (map_id, x, y, z)


def handle_corpse_query_response(ctx, payload: bytes):
    """MSG_CORPSE_QUERY's server->client shape (WorldPackets::Query::
    CorpseLocation::Write, QueryPackets.cpp): uint8 Valid; if valid,
    int32 MapID, float x, y, z, int32 ActualMapID, uint32 Transport.
    ActualMapID (not MapID) is what a client paths to — for a corpse in
    an instance's entrance map TrinityCore substitutes the reachable
    entrance map/position there, per WorldSession::HandleQueryCorpseLocation
    (QueryHandler.cpp). Transport offsets are unused — the agent doesn't
    ride transports yet (same limitation as agent/movement.py)."""
    valid = payload[0]
    if not valid:
        ctx.state.corpse_position = None
        return
    x, y, z = struct.unpack_from('<3f', payload, 5)
    actual_map_id = struct.unpack_from('<i', payload, 17)[0]
    ctx.state.corpse_position = (actual_map_id, x, y, z)


ROUTER.register_all({
    SMSG_CORPSE_RECLAIM_DELAY: handle_corpse_reclaim_delay,
    SMSG_DEATH_RELEASE_LOC: handle_death_release_loc,
    MSG_CORPSE_QUERY: handle_corpse_query_response,
})
