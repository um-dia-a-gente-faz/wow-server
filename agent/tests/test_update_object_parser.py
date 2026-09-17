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

    def test_living_spline_enabled_parses_spline_block(self):
        spline = (struct.pack('<I', uo.SPLINEFLAG_FINAL_POINT)   # spline_flags
                   + struct.pack('<3f', 9.0, 9.0, 9.0)            # final_point
                   + struct.pack('<i', 111)                       # time_passed
                   + struct.pack('<I', 2222)                      # duration
                   + struct.pack('<I', 777)                       # spline_id
                   + struct.pack('<2f', 1.0, 1.0)                 # duration mods (unused)
                   + struct.pack('<f', 0.0)                       # vertical_acceleration
                   + struct.pack('<I', 0)                         # effect_start_time
                   + struct.pack('<I', 2)                         # point_count
                   + struct.pack('<3f', 1.0, 2.0, 3.0)
                   + struct.pack('<3f', 4.0, 5.0, 6.0)
                   + bytes([0])                                   # mode = linear
                   + struct.pack('<3f', 4.0, 5.0, 6.0))            # destination
        body = (struct.pack('<H', uo.UPDATEFLAG_LIVING)
                 + struct.pack('<I', uo.MOVEMENTFLAG_SPLINE_ENABLED)
                 + struct.pack('<H', 0)
                 + struct.pack('<I', 1)
                 + struct.pack('<4f', 0.0, 0.0, 0.0, 0.0)
                 + struct.pack('<I', 0)
                 + struct.pack('<9f', *([0.0] * 9))
                 + spline)
        info, off = uo._parse_movement_update(body, 0)
        self.assertEqual(off, len(body))
        s = info['spline']
        self.assertEqual(s['spline_flags'], uo.SPLINEFLAG_FINAL_POINT)
        self.assertEqual(s['final_point'], (9.0, 9.0, 9.0))
        self.assertEqual(s['time_passed'], 111)
        self.assertEqual(s['duration'], 2222)
        self.assertEqual(s['spline_id'], 777)
        self.assertEqual(s['points'], [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])
        self.assertEqual(s['mode'], 0)
        self.assertEqual(s['destination'], (4.0, 5.0, 6.0))

    def test_living_spline_final_target_uses_raw_guid(self):
        target_guid = 0xF530000000000009
        spline = (struct.pack('<I', uo.SPLINEFLAG_FINAL_TARGET)
                   + struct.pack('<Q', target_guid)
                   + struct.pack('<i', 0) + struct.pack('<I', 0) + struct.pack('<I', 0)
                   + struct.pack('<2f', 1.0, 1.0) + struct.pack('<f', 0.0) + struct.pack('<I', 0)
                   + struct.pack('<I', 0)   # point_count = 0
                   + bytes([1])              # mode = catmullrom
                   + struct.pack('<3f', 0.0, 0.0, 0.0))
        body = (struct.pack('<H', uo.UPDATEFLAG_LIVING)
                 + struct.pack('<I', uo.MOVEMENTFLAG_SPLINE_ENABLED)
                 + struct.pack('<H', 0)
                 + struct.pack('<I', 1)
                 + struct.pack('<4f', 0.0, 0.0, 0.0, 0.0)
                 + struct.pack('<I', 0)
                 + struct.pack('<9f', *([0.0] * 9))
                 + spline)
        info, off = uo._parse_movement_update(body, 0)
        self.assertEqual(off, len(body))
        self.assertEqual(info['spline']['final_target_guid'], target_guid)
        self.assertNotIn('final_point', info['spline'])

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


def pack_xyz_delta(dx: float, dy: float, dz: float) -> bytes:
    """Inverse of agent.update_object._unpack_xyz_delta, built the same way
    ByteBuffer::appendPackXYZ does — for building MSG_MONSTER_MOVE test
    fixtures with packed intermediate waypoints."""
    packed = int(dx / 0.25) & 0x7FF
    packed |= (int(dy / 0.25) & 0x7FF) << 11
    packed |= (int(dz / 0.25) & 0x3FF) << 22
    return struct.pack('<I', packed)


class MovementInfoTest(unittest.TestCase):
    """MSG_MOVE_* broadcasts: packed guid + MovementInfo, no speeds/spline —
    agent.session dispatches these straight to parse_movement_info."""

    def test_movement_info_basic(self):
        body = (struct.pack('<I', 0)       # move_flags
                 + struct.pack('<H', 0)    # move_flags2
                 + struct.pack('<I', 555)  # time
                 + struct.pack('<4f', 1.0, 2.0, 3.0, 0.5)
                 + struct.pack('<I', 0))   # fall_time
        info, off = uo.parse_movement_info(body, 0)
        self.assertEqual(off, len(body))
        self.assertEqual((info['x'], info['y'], info['z'], info['o']), (1.0, 2.0, 3.0, 0.5))
        self.assertNotIn('pitch', info)

    def test_movement_info_has_no_speeds_or_spline(self):
        # Even with SPLINE_ENABLED set, MovementInfo itself carries no spline
        # data (that's SMSG_MONSTER_MOVE's job) — parsing must stop right
        # after the flag-gated fields, not look for speeds.
        body = (struct.pack('<I', uo.MOVEMENTFLAG_SPLINE_ENABLED)
                 + struct.pack('<H', 0)
                 + struct.pack('<I', 1)
                 + struct.pack('<4f', 0.0, 0.0, 0.0, 0.0)
                 + struct.pack('<I', 0))
        info, off = uo.parse_movement_info(body, 0)
        self.assertEqual(off, len(body))


class MonsterMoveParseTest(unittest.TestCase):
    def test_stop_has_no_trailing_spline_data(self):
        payload = (pk.pack_packed_guid(0x1) + bytes([0])
                   + struct.pack('<3f', 1.0, 2.0, 3.0)
                   + struct.pack('<I', 42)          # spline_id
                   + bytes([uo.MONSTER_MOVE_STOP]))
        info = uo.parse_monster_move(payload)
        self.assertEqual(info['move_type'], uo.MONSTER_MOVE_STOP)
        self.assertEqual(info['pos'], (1.0, 2.0, 3.0))
        self.assertNotIn('destination', info)

    def test_normal_linear_with_packed_waypoint(self):
        pos = (0.0, 0.0, 0.0)
        destination = (10.0, 0.0, 0.0)
        mid = (5.0, 0.0, 0.0)
        waypoint = (5.0, 2.0, 0.0)  # delta from mid = (0, 2, 0)
        delta = tuple(mid[i] - waypoint[i] for i in range(3))
        payload = (pk.pack_packed_guid(0x1) + bytes([0])
                   + struct.pack('<3f', *pos)
                   + struct.pack('<I', 42)
                   + bytes([uo.MONSTER_MOVE_NORMAL])
                   + struct.pack('<I', 0)            # flags (no facing/anim/parabolic/catmullrom)
                   + struct.pack('<I', 3000)         # move_time
                   + struct.pack('<I', 2)            # point_count: 1 raw + 1 packed
                   + struct.pack('<3f', *destination)
                   + pack_xyz_delta(*delta))
        info = uo.parse_monster_move(payload)
        self.assertEqual(info['move_type'], uo.MONSTER_MOVE_NORMAL)
        self.assertEqual(info['move_time'], 3000)
        self.assertEqual(info['destination'], destination)
        self.assertEqual(len(info['points']), 2)
        self.assertEqual(info['points'][0], destination)
        wx, wy, wz = info['points'][1]
        self.assertAlmostEqual(wx, waypoint[0], places=1)
        self.assertAlmostEqual(wy, waypoint[1], places=1)
        self.assertAlmostEqual(wz, waypoint[2], places=1)

    def test_facing_target_uses_raw_guid(self):
        target_guid = 0xF530000000000042
        payload = (pk.pack_packed_guid(0x1) + bytes([0])
                   + struct.pack('<3f', 0.0, 0.0, 0.0)
                   + struct.pack('<I', 1)
                   + bytes([uo.MONSTER_MOVE_FACING_TARGET])
                   + struct.pack('<Q', target_guid)
                   + struct.pack('<I', 0)   # flags
                   + struct.pack('<I', 100)  # move_time
                   + struct.pack('<I', 0))   # point_count = 0
        info = uo.parse_monster_move(payload)
        self.assertEqual(info['face_guid'], target_guid)
        self.assertEqual(info['destination'], (0.0, 0.0, 0.0))  # falls back to pos

    def test_catmullrom_points_are_all_raw(self):
        pts = [(0.0, 0.0, 0.0), (1.0, 1.0, 0.0), (2.0, 2.0, 0.0)]
        payload = (pk.pack_packed_guid(0x1) + bytes([0])
                   + struct.pack('<3f', 0.0, 0.0, 0.0)
                   + struct.pack('<I', 1)
                   + bytes([uo.MONSTER_MOVE_NORMAL])
                   + struct.pack('<I', uo.SPLINEFLAG_CATMULLROM)
                   + struct.pack('<I', 1000)
                   + struct.pack('<I', len(pts))
                   + b''.join(struct.pack('<3f', *p) for p in pts))
        info = uo.parse_monster_move(payload)
        self.assertEqual(info['points'], pts)
        self.assertEqual(info['destination'], pts[-1])

    def test_animation_and_parabolic_flags_consume_extra_fields(self):
        payload = (pk.pack_packed_guid(0x1) + bytes([0])
                   + struct.pack('<3f', 0.0, 0.0, 0.0)
                   + struct.pack('<I', 1)
                   + bytes([uo.MONSTER_MOVE_NORMAL])
                   + struct.pack('<I', uo.SPLINEFLAG_ANIMATION | uo.SPLINEFLAG_PARABOLIC)
                   + bytes([3]) + struct.pack('<I', 10)    # AnimTierTransition
                   + struct.pack('<I', 500)                 # move_time
                   + struct.pack('<f', -9.8) + struct.pack('<I', 20)  # JumpExtraData
                   + struct.pack('<I', 0))                  # point_count = 0
        info = uo.parse_monster_move(payload)
        self.assertEqual(info['anim_tier'], 3)
        self.assertEqual(info['anim_start_time'], 10)
        self.assertAlmostEqual(info['jump_gravity'], -9.8, places=4)
        self.assertEqual(info['jump_start_time'], 20)

    def test_leftover_bytes_raise(self):
        payload = (pk.pack_packed_guid(0x1) + bytes([0])
                   + struct.pack('<3f', 0.0, 0.0, 0.0)
                   + struct.pack('<I', 1)
                   + bytes([uo.MONSTER_MOVE_STOP])
                   + b'\xFF')
        with self.assertRaises(ValueError):
            uo.parse_monster_move(payload)


if __name__ == '__main__':
    unittest.main()
