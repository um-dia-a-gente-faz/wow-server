"""world_mock scenario "death with a corpse" (#321, parent #309).

DeathMock is a WorldMock whose character is dead on arrival: after the login burst the own
player gets a VALUES block with health 0. Then, like TrinityCore:

  CMSG_REPOP_REQUEST          -> ghost: health 1, PLAYER_FLAGS_GHOST, SMSG_DEATH_RELEASE_LOC
                                 (WorldSession::HandleRepopRequest -> BuildPlayerRepop/RepopAtGraveyard)
  MSG_CORPSE_QUERY            -> SMSG-side MSG_CORPSE_QUERY, valid corpse (HandleQueryCorpseLocation)
  CMSG_RECLAIM_CORPSE (guid)  -> alive again iff guid is the corpse. The GUID check is the mock's own and
                                 stricter than the server: HandleReclaimCorpse ignores the packet's GUID and
                                 checks only alive/ghost, the corpse, the delay and the 39 yd radius (not modelled here)
  CMSG_SPIRIT_HEALER_ACTIVATE -> alive again (HandleSpiritHealerActivateOpcode; range not modelled)

`corpse_perceived=False` never sends the corpse object, so the agent must fall back to the spirit
healer. `reclaim_delay_ms` sends SMSG_CORPSE_RECLAIM_DELAY after the release (Player::SendCorpseReclaimDelay).

Layouts are written here from TrinityCore 3.3.5 (MiscPackets.cpp CorpseReclaimDelay/DeathReleaseLoc::Write,
QueryPackets.cpp CorpseLocation::Write, UpdateFields.h, UnitDefines.h, Opcodes.h), with the field indices
and opcode numbers repeated as literals so a mistake in agent/update_fields.py or agent/opcodes.py cannot
make this mock agree with it. The CREATE/VALUES block framing reuses agent/tests/builders.py.
"""

import struct
import threading
import time

from tools.world_mock import server  # noqa: E402

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
STEP_TIMEOUT_S = 10.0                    # #521: a wrong parse fails in seconds, not after a hang


def _not_reached(step: str) -> AssertionError:
    return AssertionError(f"death scenario: step '{step}' not reached within {STEP_TIMEOUT_S:.0f}s")


def wait_step(step: str, pred) -> None:
    """Poll `pred` until it is true, at most STEP_TIMEOUT_S; otherwise fail naming the step."""
    end = time.monotonic() + STEP_TIMEOUT_S
    while not pred():
        if time.monotonic() >= end:
            raise _not_reached(step)
        time.sleep(0.02)


def run_step(step: str, fn):
    """Run a synchronous step (an action's run(), a login) in a worker thread and return its value.
    A step that does not return within STEP_TIMEOUT_S fails naming the step; its thread is left
    to finish on its own (daemon), and the scenario's cleanups close the socket underneath it."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 (re-raised in the caller's thread below)
            box["error"] = e

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(STEP_TIMEOUT_S)
    if worker.is_alive():
        raise _not_reached(step)
    if "error" in box:
        raise box["error"]
    return box["value"]


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
            # The GUID check below is the mock's own, stricter than the server: HandleReclaimCorpse
            # never reads the packet's GUID. The mock still requires the corpse GUID so a wrong one fails here.
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
