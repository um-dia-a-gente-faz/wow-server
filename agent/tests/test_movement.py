"""Unit tests for agent.movement: the MovementInfo wire writer used by
agent.actions.FaceAction (and, later, UM-38's straight-line movement)."""

import struct
import unittest
from types import SimpleNamespace

from agent import movement as mv
from agent import packets as pk
from agent import update_object as uo


class BuildMovementInfoTest(unittest.TestCase):
    def test_basic_shape_matches_packed_guid_plus_fields(self):
        body = mv.build_movement_info(0xF13000000000042, 1.0, 2.0, 3.0, 0.5)
        guid, off = pk.unpack_packed_guid(body, 0)
        self.assertEqual(guid, 0xF13000000000042)
        move_flags, move_flags2, client_time = struct.unpack_from('<IHI', body, off)
        off += 4 + 2 + 4
        x, y, z, o = struct.unpack_from('<4f', body, off)
        off += 16
        fall_time = struct.unpack_from('<I', body, off)[0]
        off += 4
        self.assertEqual(move_flags, 0)
        self.assertEqual(move_flags2, 0)
        self.assertEqual((x, y, z, o), (1.0, 2.0, 3.0, 0.5))
        self.assertEqual(fall_time, 0)
        self.assertEqual(off, len(body))  # nothing else written for the plain case

    def test_client_time_is_nonzero_and_fits_uint32(self):
        body = mv.build_movement_info(1, 0, 0, 0, 0)
        _, off = pk.unpack_packed_guid(body, 0)
        off += 4 + 2
        client_time = struct.unpack_from('<I', body, off)[0]
        self.assertGreater(client_time, 0)
        self.assertLess(client_time, 2 ** 32)

    def test_pitch_written_when_swimming(self):
        body = mv.build_movement_info(1, 0, 0, 0, 0, move_flags=uo.MOVEMENTFLAG_SWIMMING, pitch=0.75)
        _, off = pk.unpack_packed_guid(body, 0)
        off += 4 + 2 + 4 + 16  # flags, flags2, time, x/y/z/o
        pitch = struct.unpack_from('<f', body, off)[0]
        self.assertAlmostEqual(pitch, 0.75)

    def test_pitch_not_written_when_not_applicable(self):
        with_pitch = mv.build_movement_info(1, 0, 0, 0, 0, move_flags=uo.MOVEMENTFLAG_SWIMMING)
        without_pitch = mv.build_movement_info(1, 0, 0, 0, 0)
        self.assertEqual(len(with_pitch), len(without_pitch) + 4)

    def test_spline_elevation_written_when_flagged(self):
        body = mv.build_movement_info(1, 0, 0, 0, 0, move_flags=uo.MOVEMENTFLAG_SPLINE_ELEVATION,
                                       spline_elevation=12.5)
        elevation = struct.unpack_from('<f', body, len(body) - 4)[0]
        self.assertAlmostEqual(elevation, 12.5)

    def test_ontransport_not_supported(self):
        with self.assertRaises(NotImplementedError):
            mv.build_movement_info(1, 0, 0, 0, 0, move_flags=uo.MOVEMENTFLAG_ONTRANSPORT)

    def test_falling_not_supported(self):
        with self.assertRaises(NotImplementedError):
            mv.build_movement_info(1, 0, 0, 0, 0, move_flags=uo.MOVEMENTFLAG_FALLING)


class SendSetFacingTest(unittest.TestCase):
    def test_sends_msg_move_set_facing_with_own_guid_and_orientation(self):
        sent = []
        sess = SimpleNamespace(player_guid=0xF130000000000099,
                                _send_packet=lambda opcode, payload=b'': sent.append((opcode, payload)))
        mv.send_set_facing(sess, 10.0, 20.0, 30.0, 1.57)
        self.assertEqual(len(sent), 1)
        opcode, payload = sent[0]
        self.assertEqual(opcode, mv.MSG_MOVE_SET_FACING)
        guid, off = pk.unpack_packed_guid(payload, 0)
        self.assertEqual(guid, 0xF130000000000099)
        x, y, z, o = struct.unpack_from('<4f', payload, off + 4 + 2 + 4)
        self.assertEqual((x, y, z), (10.0, 20.0, 30.0))
        self.assertAlmostEqual(o, 1.57, places=5)


if __name__ == '__main__':
    unittest.main()
