"""Unit tests for agent.update_object against synthetic (hand-built) bytes.

Complements agent/tests/test_update_object.py's fixture-based tests: the real
fixtures don't exercise every UPDATEFLAG/MOVEMENTFLAG conditional path (e.g.
ONTRANSPORT, VEHICLE, ROTATION, multi-word value masks), so this file builds
those wire shapes by hand from the cited layout in agent/update_object.py.
"""

import struct
import unittest

from agent import packets as pk
from agent import update_object as uo


def block_header(block_count: int, *block_bytes: bytes) -> bytes:
    return struct.pack('<I', block_count) + b''.join(block_bytes)


def values_body(field_values: dict) -> bytes:
    """uint8 mask_block_count, mask words, one uint32 per set bit ascending."""
    if not field_values:
        return b'\x00'
    max_bit = max(field_values)
    block_count = max_bit // 32 + 1
    words = [0] * block_count
    for idx in field_values:
        words[idx // 32] |= 1 << (idx % 32)
    out = bytes([block_count]) + b''.join(struct.pack('<I', w) for w in words)
    for idx in sorted(field_values):
        out += struct.pack('<I', field_values[idx])
    return out


class MovementFramingTest(unittest.TestCase):
    def test_stationary_position(self):
        body = (struct.pack('<H', uo.UPDATEFLAG_STATIONARY_POSITION)
                 + struct.pack('<4f', 1.0, 2.0, 3.0, 0.5))
        info, off = uo._parse_movement_update(body, 0)
        self.assertEqual(off, len(body))
        self.assertEqual((info['x'], info['y'], info['z'], info['o']), (1.0, 2.0, 3.0, 0.5))

    def test_position_not_on_transport(self):
        # packed guid 0 (a lone 0x00 mask byte), x,y,z, x,y,z again, o, o again.
        body = (struct.pack('<H', uo.UPDATEFLAG_POSITION)
                 + b'\x00'
                 + struct.pack('<3f', 10.0, 20.0, 30.0)
                 + struct.pack('<3f', 10.0, 20.0, 30.0)
                 + struct.pack('<f', 1.5)
                 + struct.pack('<f', 1.5))
        info, off = uo._parse_movement_update(body, 0)
        self.assertEqual(off, len(body))
        self.assertEqual((info['x'], info['y'], info['z'], info['o']), (10.0, 20.0, 30.0, 1.5))
        self.assertEqual(info['transport_guid'], 0)

    def test_living_basic_no_optional_fields(self):
        body = (struct.pack('<H', uo.UPDATEFLAG_LIVING)
                 + struct.pack('<I', 0)      # move_flags = 0
                 + struct.pack('<H', 0)      # move_flags2 = 0
                 + struct.pack('<I', 12345)  # time
                 + struct.pack('<4f', 1.0, 2.0, 3.0, 0.0)  # x,y,z,o
                 + struct.pack('<I', 0)      # fall_time
                 + struct.pack('<9f', *range(9)))  # speeds
        info, off = uo._parse_movement_update(body, 0)
        self.assertEqual(off, len(body))
        self.assertEqual(info['move_flags'], 0)
        self.assertEqual((info['x'], info['y'], info['z']), (1.0, 2.0, 3.0))
        self.assertEqual(info['speeds'], tuple(float(i) for i in range(9)))
        self.assertNotIn('pitch', info)

    def test_living_ontransport_with_interpolation(self):
        transport_guid = 0xF33000000000000A
        body = (struct.pack('<H', uo.UPDATEFLAG_LIVING)
                 + struct.pack('<I', uo.MOVEMENTFLAG_ONTRANSPORT)
                 + struct.pack('<H', uo.MOVEMENTFLAG2_INTERPOLATED_MOVEMENT)
                 + struct.pack('<I', 1)
                 + struct.pack('<4f', 1.0, 2.0, 3.0, 0.0)
                 + pk.pack_packed_guid(transport_guid)
                 + struct.pack('<4f', 0.1, 0.2, 0.3, 0.4)  # transport x,y,z,o
                 + struct.pack('<I', 999)   # transport.time
                 + struct.pack('<b', 2)     # transport.seat
                 + struct.pack('<I', 111)   # transport.time2 (interpolated)
                 + struct.pack('<I', 0)     # fall_time
                 + struct.pack('<9f', *([0.0] * 9)))
        info, off = uo._parse_movement_update(body, 0)
        self.assertEqual(off, len(body))
        self.assertEqual(info['transport_guid'], transport_guid)
        for actual, expected in zip((info['tx'], info['ty'], info['tz'], info['to']), (0.1, 0.2, 0.3, 0.4)):
            self.assertAlmostEqual(actual, expected, places=5)

    def test_living_swimming_has_pitch(self):
        body = (struct.pack('<H', uo.UPDATEFLAG_LIVING)
                 + struct.pack('<I', uo.MOVEMENTFLAG_SWIMMING)
                 + struct.pack('<H', 0)
                 + struct.pack('<I', 1)
                 + struct.pack('<4f', 0.0, 0.0, 0.0, 0.0)
                 + struct.pack('<f', 0.75)   # pitch
                 + struct.pack('<I', 0)      # fall_time
                 + struct.pack('<9f', *([0.0] * 9)))
        info, off = uo._parse_movement_update(body, 0)
        self.assertEqual(off, len(body))
        self.assertAlmostEqual(info['pitch'], 0.75)

    def test_living_falling_has_jump_fields(self):
        body = (struct.pack('<H', uo.UPDATEFLAG_LIVING)
                 + struct.pack('<I', uo.MOVEMENTFLAG_FALLING)
                 + struct.pack('<H', 0)
                 + struct.pack('<I', 1)
                 + struct.pack('<4f', 0.0, 0.0, 0.0, 0.0)
                 + struct.pack('<I', 500)    # fall_time
                 + struct.pack('<4f', -5.0, 0.1, 0.2, 3.0)  # zspeed,sinAngle,cosAngle,xyspeed
                 + struct.pack('<9f', *([0.0] * 9)))
        info, off = uo._parse_movement_update(body, 0)
        self.assertEqual(off, len(body))

    def test_living_spline_elevation_consumed(self):
        body = (struct.pack('<H', uo.UPDATEFLAG_LIVING)
                 + struct.pack('<I', uo.MOVEMENTFLAG_SPLINE_ELEVATION)
                 + struct.pack('<H', 0)
                 + struct.pack('<I', 1)
                 + struct.pack('<4f', 0.0, 0.0, 0.0, 0.0)
                 + struct.pack('<I', 0)      # fall_time
                 + struct.pack('<f', 12.5)   # splineElevation
                 + struct.pack('<9f', *([0.0] * 9)))
        info, off = uo._parse_movement_update(body, 0)
        self.assertEqual(off, len(body))

    def test_living_spline_enabled_raises(self):
        body = (struct.pack('<H', uo.UPDATEFLAG_LIVING)
                 + struct.pack('<I', uo.MOVEMENTFLAG_SPLINE_ENABLED)
                 + struct.pack('<H', 0)
                 + struct.pack('<I', 1)
                 + struct.pack('<4f', 0.0, 0.0, 0.0, 0.0)
                 + struct.pack('<I', 0)
                 + struct.pack('<9f', *([0.0] * 9)))
        with self.assertRaises(uo.UnhandledMovementFlags) as ctx:
            uo._parse_movement_update(body, 0)
        self.assertEqual(ctx.exception.move_flags, uo.MOVEMENTFLAG_SPLINE_ENABLED)

    def test_trailing_flags_all_set(self):
        target_guid = 0xF530000000000003
        body = (struct.pack('<H', uo.UPDATEFLAG_STATIONARY_POSITION | uo.UPDATEFLAG_UNKNOWN
                             | uo.UPDATEFLAG_LOWGUID | uo.UPDATEFLAG_HAS_TARGET
                             | uo.UPDATEFLAG_TRANSPORT | uo.UPDATEFLAG_VEHICLE | uo.UPDATEFLAG_ROTATION)
                 + struct.pack('<4f', 1.0, 1.0, 1.0, 1.0)  # stationary position
                 + struct.pack('<I', 0)                     # UNKNOWN
                 + struct.pack('<I', 0)                     # LOWGUID
                 + pk.pack_packed_guid(target_guid)          # HAS_TARGET
                 + struct.pack('<I', 0)                      # TRANSPORT path_timer
                 + struct.pack('<I', 0) + struct.pack('<f', 0.0)  # VEHICLE id + facing
                 + struct.pack('<q', 0))                     # ROTATION
        info, off = uo._parse_movement_update(body, 0)
        self.assertEqual(off, len(body))
        self.assertEqual(info['target_guid'], target_guid)


class ValuesParseTest(unittest.TestCase):
    def test_single_word_mask(self):
        body = values_body({0x03: 6368, 0x18: 1})
        fields, off = uo._parse_values_update(body, 0)
        self.assertEqual(off, len(body))
        self.assertEqual(fields, {0x03: 6368, 0x18: 1})

    def test_multi_word_mask(self):
        # Field 0x25 (37) lives in the second mask word.
        body = values_body({0x03: 100, 0x25: 42})
        fields, off = uo._parse_values_update(body, 0)
        self.assertEqual(off, len(body))
        self.assertEqual(fields, {0x03: 100, 0x25: 42})

    def test_no_fields_set(self):
        body = values_body({})
        self.assertEqual(body, b'\x00')
        fields, off = uo._parse_values_update(body, 0)
        self.assertEqual(off, 1)
        self.assertEqual(fields, {})


class ParseUpdateObjectTest(unittest.TestCase):
    def test_create_object_full_block(self):
        movement = struct.pack('<H', uo.UPDATEFLAG_STATIONARY_POSITION) + struct.pack('<4f', 1, 2, 3, 0)
        values = values_body({0x03: 999})
        block = (bytes([uo.UPDATETYPE_CREATE_OBJECT]) + pk.pack_packed_guid(5)
                 + bytes([uo.TYPEID_GAMEOBJECT]) + movement + values)
        data = block_header(1, block)
        blocks = uo.parse_update_object(data)
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertEqual(b.update_type, uo.UPDATETYPE_CREATE_OBJECT)
        self.assertEqual(b.guid, 5)
        self.assertEqual(b.object_type, uo.TYPEID_GAMEOBJECT)
        self.assertEqual((b.movement['x'], b.movement['y'], b.movement['z']), (1, 2, 3))
        self.assertEqual(b.fields, {0x03: 999})

    def test_out_of_range_and_near_objects(self):
        oor = bytes([uo.UPDATETYPE_OUT_OF_RANGE_OBJECTS]) + struct.pack('<I', 2) + \
            pk.pack_packed_guid(1) + pk.pack_packed_guid(2)
        near = bytes([uo.UPDATETYPE_NEAR_OBJECTS]) + struct.pack('<I', 1) + pk.pack_packed_guid(3)
        data = block_header(2, oor, near)
        blocks = uo.parse_update_object(data)
        self.assertEqual(blocks[0].guids, [1, 2])
        self.assertEqual(blocks[1].guids, [3])

    def test_multiple_blocks_offsets_dont_desync(self):
        values = values_body({})
        movement = struct.pack('<H', uo.UPDATEFLAG_STATIONARY_POSITION) + struct.pack('<4f', 0, 0, 0, 0)
        b1 = bytes([uo.UPDATETYPE_CREATE_OBJECT]) + pk.pack_packed_guid(1) + bytes([uo.TYPEID_UNIT]) + movement + values
        b2 = bytes([uo.UPDATETYPE_VALUES]) + pk.pack_packed_guid(2) + values_body({0x18: 50})
        b3 = bytes([uo.UPDATETYPE_CREATE_OBJECT2]) + pk.pack_packed_guid(3) + bytes([uo.TYPEID_PLAYER]) + movement + values
        data = block_header(3, b1, b2, b3)
        blocks = uo.parse_update_object(data)
        self.assertEqual([b.guid for b in blocks], [1, 2, 3])
        self.assertEqual([b.update_type for b in blocks],
                          [uo.UPDATETYPE_CREATE_OBJECT, uo.UPDATETYPE_VALUES, uo.UPDATETYPE_CREATE_OBJECT2])

    def test_leftover_bytes_raise(self):
        data = block_header(0) + b'\xFF'  # zero blocks but one trailing byte
        with self.assertRaises(ValueError):
            uo.parse_update_object(data)

    def test_unknown_update_type_raises(self):
        data = block_header(1, bytes([6]) + pk.pack_packed_guid(1))
        with self.assertRaises(ValueError):
            uo.parse_update_object(data)


if __name__ == '__main__':
    unittest.main()
