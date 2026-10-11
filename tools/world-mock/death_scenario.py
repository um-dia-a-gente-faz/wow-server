"""world-mock scenario "death with a corpse" (#321, parent #309).

DeathMock is a WorldMock whose character is dead on arrival: after the login burst the own
player gets a VALUES block with health 0. Then, like TrinityCore:

  CMSG_REPOP_REQUEST          -> ghost: health 1, PLAYER_FLAGS_GHOST, SMSG_DEATH_RELEASE_LOC
                                 (WorldSession::HandleRepopRequest -> BuildPlayerRepop/RepopAtGraveyard)
  MSG_CORPSE_QUERY            -> SMSG-side MSG_CORPSE_QUERY, valid corpse (HandleQueryCorpseLocation)
  CMSG_RECLAIM_CORPSE (guid)  -> alive again iff guid is the corpse (HandleReclaimCorpse; the mock does
                                 not model the 39 yd radius, the ghost-time delay or the repeat-death timer)
  CMSG_SPIRIT_HEALER_ACTIVATE -> alive again (HandleSpiritHealerActivateOpcode; range not modelled)

`corpse_perceived=False` never sends the corpse object, so the agent must fall back to the spirit
healer. `reclaim_delay_ms` sends SMSG_CORPSE_RECLAIM_DELAY after the release (Player::SendCorpseReclaimDelay).

Layouts are written here from TrinityCore 3.3.5 (MiscPackets.cpp CorpseReclaimDelay/DeathReleaseLoc::Write,
QueryPackets.cpp CorpseLocation::Write, UpdateFields.h, UnitDefines.h, Opcodes.h), with the field indices
and opcode numbers repeated as literals so a mistake in agent/update_fields.py or agent/opcodes.py cannot
make this mock agree with it. The CREATE/VALUES block framing reuses agent/tests/builders.py.
"""

import pathlib
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import server  # noqa: E402

from agent import update_object as uo  # noqa: E402
from agent.tests import builders as bd  # noqa: E402

# Opcodes.h
CMSG_REPOP_REQUEST = 0x15A
CMSG_RECLAIM_CORPSE = 0x1D2
MSG_CORPSE_QUERY = 0x216
CMSG_SPIRIT_HEALER_ACTIVATE = 0x21C
SMSG_CORPSE_RECLAIM_DELAY = 0x269
SMSG_DEATH_RELEASE_LOC = 0x378
SMSG_UPDATE_OBJECT = 0x0A9
# UpdateFields.h: OBJECT_END = 6, UNIT_END = OBJECT_END + 0x8E
OBJECT_FIELD_ENTRY = 3
UNIT_FIELD_HEALTH = 6 + 0x12
UNIT_FIELD_MAXHEALTH = 6 + 0x1A
UNIT_FIELD_LEVEL = 6 + 0x30
UNIT_NPC_FLAGS = 6 + 0x4C
PLAYER_FLAGS = 6 + 0x8E + 0x02
PLAYER_FLAGS_GHOST = 0x10                # Player.h
UNIT_NPC_FLAG_SPIRITHEALER = 0x4000      # UnitDefines.h

CORPSE_GUID = 0xF101000000000001         # HighGuid::Corpse
HEALER_GUID = 0xF130000000007001         # HighGuid::Unit
HEALER_ENTRY = 6491                      # Spirit Healer
HEALER_OFFSET_YD = 10.0
MAX_HEALTH = 100


class DeathMock(server.WorldMock):
    def __init__(self, *args, corpse_perceived=True, reclaim_delay_ms=0, **kw):
        super().__init__(*args, **kw)
        self.corpse_perceived = corpse_perceived
        self.reclaim_delay_ms = reclaim_delay_ms
        self.state = "alive"          # alive -> dead -> ghost -> alive
        self.resurrected_by = None    # "corpse" | "spirit_healer"

    def _self_values(self, send, health, flags):
        send(SMSG_UPDATE_OBJECT, bd.update_object(bd.values_block(
            server.SELF_GUID, bd.values_body({UNIT_FIELD_HEALTH: health, PLAYER_FLAGS: flags}))))

    def _enter_world(self, send):
        super()._enter_world(send)
        x, y, z, o = server.SELF_POS
        send(SMSG_UPDATE_OBJECT, bd.update_object(bd.object_block(
            uo.UPDATETYPE_CREATE_OBJECT2, HEALER_GUID, uo.TYPEID_UNIT,
            bd.stationary_movement(x + HEALER_OFFSET_YD, y, z, o),
            bd.values_body({OBJECT_FIELD_ENTRY: HEALER_ENTRY, UNIT_FIELD_LEVEL: 60,
                            UNIT_FIELD_HEALTH: MAX_HEALTH, UNIT_FIELD_MAXHEALTH: MAX_HEALTH,
                            UNIT_NPC_FLAGS: UNIT_NPC_FLAG_SPIRITHEALER}))))
        if self.corpse_perceived:
            send(SMSG_UPDATE_OBJECT, bd.update_object(bd.object_block(
                uo.UPDATETYPE_CREATE_OBJECT2, CORPSE_GUID, uo.TYPEID_CORPSE,
                bd.stationary_movement(x, y, z, o))))
        self.state = "dead"
        self._self_values(send, 0, 0)

    def _on_packet(self, send, opcode, payload):
        x, y, z, _o = server.SELF_POS
        if opcode == CMSG_REPOP_REQUEST:
            if self.state == "dead":     # HandleRepopRequest returns if alive or already a ghost
                self.state = "ghost"
                self._self_values(send, 1, PLAYER_FLAGS_GHOST)
                send(SMSG_DEATH_RELEASE_LOC, struct.pack('<i3f', server.MAP_ID, x + 50.0, y, z))
                if self.reclaim_delay_ms:
                    send(SMSG_CORPSE_RECLAIM_DELAY, struct.pack('<I', self.reclaim_delay_ms))
        elif opcode == MSG_CORPSE_QUERY:
            if self.state == "ghost":
                send(MSG_CORPSE_QUERY, struct.pack('<Bi3fiI', 1, server.MAP_ID, x, y, z, server.MAP_ID, 0))
            else:
                send(MSG_CORPSE_QUERY, b'\0')
        elif opcode == CMSG_RECLAIM_CORPSE:
            if self.state == "ghost" and self.corpse_perceived and payload == struct.pack('<Q', CORPSE_GUID):
                self._resurrect(send, "corpse")
        elif opcode == CMSG_SPIRIT_HEALER_ACTIVATE:
            if self.state == "ghost" and payload == struct.pack('<Q', HEALER_GUID):
                self._resurrect(send, "spirit_healer")
        else:
            super()._on_packet(send, opcode, payload)

    def _resurrect(self, send, how):
        self.state, self.resurrected_by = "alive", how
        self._self_values(send, MAX_HEALTH // 2, 0)      # ResurrectPlayer(0.5)
