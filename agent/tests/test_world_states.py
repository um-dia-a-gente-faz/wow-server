"""#316: SMSG_INIT_WORLD_STATES sets the session's zone and area ids; the layout is TrinityCore 3.3.5's
WorldStatePackets.cpp InitWorldStates::Write (int32 MapID, int32 zone, int32 area, uint16 count, pairs)."""
import struct
import unittest

from agent import handlers
from agent.opcodes import SMSG_INIT_WORLD_STATES
from agent.router import Context
from agent.state import GameState
from agent.wire import PacketTruncated

MAP_EASTERN_KINGDOMS = 0
ZONE_ELWYNN_FOREST = 12
AREA_GOLDSHIRE = 87


def init_world_states(map_id=MAP_EASTERN_KINGDOMS, zone=ZONE_ELWYNN_FOREST, area=AREA_GOLDSHIRE,
                      pairs=((2264, 0), (2263, 0), (3191, 7))):
    """The payload the server writes: three int32 ids, uint16 count, then int32 pairs."""
    body = struct.pack("<iiiH", map_id, zone, area, len(pairs))
    return body + b"".join(struct.pack("<ii", var, val) for var, val in pairs)


class InitWorldStatesHandlerTest(unittest.TestCase):
    def setUp(self):
        self.state = GameState()
        self.ctx = Context(self.state, None)
        self.router = handlers.build_router()

    def dispatch(self, payload):
        return self.router.dispatch(self.ctx, SMSG_INIT_WORLD_STATES, payload)

    def test_opcode_is_the_trinitycore_value(self):
        self.assertEqual(SMSG_INIT_WORLD_STATES, 0x2C2)   # Opcodes.h in 3.3.5

    def test_zone_and_area_are_read_and_the_pairs_are_ignored(self):
        self.assertTrue(self.dispatch(init_world_states()))
        self.assertEqual(self.state.zone_id, ZONE_ELWYNN_FOREST)
        self.assertEqual(self.state.area_id, AREA_GOLDSHIRE)

    def test_a_later_packet_replaces_the_zone(self):
        self.dispatch(init_world_states(zone=12))
        self.dispatch(init_world_states(zone=40, area=0))   # Westfall, no subarea
        self.assertEqual((self.state.zone_id, self.state.area_id), (40, 0))

    def test_no_packet_leaves_both_ids_unknown(self):
        self.assertIsNone(self.state.zone_id)
        self.assertIsNone(self.state.area_id)

    def test_a_truncated_payload_raises_like_other_short_packets_and_changes_nothing(self):
        short = init_world_states()[:10]   # the map id and zone id are there, the area id is not
        with self.assertRaises(PacketTruncated):
            self.dispatch(short)
        self.assertIsNone(self.state.zone_id)
        self.assertIsNone(self.state.area_id)

    def test_the_handler_is_registered(self):
        self.assertTrue(handlers.build_router().handles(SMSG_INIT_WORLD_STATES))


if __name__ == "__main__":
    unittest.main()
