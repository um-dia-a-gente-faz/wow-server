"""SMSG_INIT_WORLD_STATES (#316): the zone and area the character is in, for the mission transitions.

Verified against TrinityCore branch 3.3.5 @ 9ab2b43e1aa78b2d88c6dd654a875179a34472ab:
  src/server/game/Server/Protocol/Opcodes.h (SMSG_INIT_WORLD_STATES = 0x2C2)
  src/server/game/Server/Packets/WorldStatePackets.h (class InitWorldStates)
  src/server/game/Server/Packets/WorldStatePackets.cpp (InitWorldStates::Write: int32 MapID,
    int32 AreaID = the zone id, int32 SubareaID = the area id; uint16 count; int32 VariableID, int32 Value pairs)
  src/server/game/Entities/Player/Player.cpp (Player::UpdateZone -> SendInitWorldStates(zone, area), on login and zone change)
Only the three ids are read; the world-state pairs are ignored.
"""

from ..opcodes import SMSG_INIT_WORLD_STATES
from ..wire import Reader


def handle_init_world_states(ctx, payload: bytes):
    r = Reader(payload, SMSG_INIT_WORLD_STATES)
    r.i32()                  # MapID
    zone = r.i32()           # the zone id ("area" in #316)
    area = r.i32()           # the subarea id
    ctx.state.zone_id = zone
    ctx.state.area_id = area


def register(router):
    router.register(SMSG_INIT_WORLD_STATES, handle_init_world_states)
