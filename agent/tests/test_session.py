"""Tests for agent.session framing, update-object GUIDs and the recv safety net.

No network and no crypto: WoWSession runs against an in-memory fake socket.
"""

import os
import socket
import struct
import tempfile
import unittest
import zlib

from agent import packets as pk
from agent import perception as per
from agent import session as se
from agent.tests.test_packets import tc_server_header


class FakeSocket:
    """Serves a fixed byte stream in chunks; EOF (b'') when exhausted."""

    def __init__(self, data: bytes, chunk: int = 7, timeout_at: int | None = None):
        self.data = data
        self.pos = 0
        self.chunk = chunk
        self.timeout_at = timeout_at  # stream offset at which recv times out
        self.timeout = None
        self.sent = b''

    def recv(self, n):
        if self.timeout_at is not None and self.pos >= self.timeout_at:
            raise socket.timeout("timed out")
        end = min(self.pos + n, self.pos + self.chunk, len(self.data))
        if self.timeout_at is not None:
            end = min(end, self.timeout_at)
        out = self.data[self.pos:end]
        self.pos = end
        return out

    def settimeout(self, t):
        self.timeout = t

    def gettimeout(self):
        return self.timeout

    def sendall(self, b):
        self.sent += b

    def close(self):
        pass


def server_packet(opcode: int, payload: bytes) -> bytes:
    return tc_server_header(len(payload) + 2, opcode) + payload


def update_object(*blocks: bytes) -> bytes:
    return struct.pack('<I', len(blocks)) + b''.join(blocks)


def object_block(update_type: int, guid: int, body: bytes = b'\xDE\xAD\xBE\xEF') -> bytes:
    return bytes([update_type]) + pk.pack_packed_guid(guid) + body


def out_of_range_block(*guids: int) -> bytes:
    return (bytes([se.UPDATETYPE_OUT_OF_RANGE_OBJECTS]) + struct.pack('<I', len(guids))
            + b''.join(pk.pack_packed_guid(g) for g in guids))


def compressed(payload: bytes) -> bytes:
    return struct.pack('<I', len(payload)) + zlib.compress(payload)


def make_session(stream: bytes = b'', **kw) -> se.WoWSession:
    sess = se.WoWSession('127.0.0.1', 8085, 'TEST', b'\x00' * 40, 1, **kw)
    sess.sock = FakeSocket(stream)
    return sess


CREATURE = 0xF130000123000456


class RecvPacketTest(unittest.TestCase):
    def test_small_packet(self):
        sess = make_session(server_packet(se.SMSG_PONG, b'\x01\x02\x03\x04'))
        self.assertEqual(sess._recv_packet(), (se.SMSG_PONG, b'\x01\x02\x03\x04'))

    def test_empty_payload(self):
        sess = make_session(server_packet(se.SMSG_LOGOUT_COMPLETE, b''))
        self.assertEqual(sess._recv_packet(), (se.SMSG_LOGOUT_COMPLETE, b''))

    def test_large_packet_then_small(self):
        big = bytes(range(256)) * 200  # 51200 B, size > 0x7FFF
        stream = (server_packet(se.SMSG_COMPRESSED_UPDATE_OBJECT, big)
                  + server_packet(se.SMSG_PONG, b'\x05\x00\x00\x00'))
        sess = make_session(stream)
        sess.sock.chunk = 4096
        self.assertEqual(sess._recv_packet(), (se.SMSG_COMPRESSED_UPDATE_OBJECT, big))
        self.assertEqual(sess._recv_packet(), (se.SMSG_PONG, b'\x05\x00\x00\x00'))

    def test_timeout_before_packet_is_plain_timeout(self):
        sess = make_session(b'')
        sess.sock.timeout_at = 0
        sess.sock.settimeout(0.5)
        with self.assertRaises(socket.timeout):
            sess._recv_packet()
        self.assertEqual(sess.sock.gettimeout(), 0.5)

    def test_timeout_mid_packet_is_connection_error(self):
        sess = make_session(server_packet(se.SMSG_PONG, b'\x01\x02\x03\x04'))
        sess.sock.timeout_at = 5
        sess.sock.settimeout(0.5)
        with self.assertRaises(ConnectionError):
            sess._recv_packet()
        self.assertEqual(sess.sock.gettimeout(), 0.5)


class ParseUpdateObjectTest(unittest.TestCase):
    def test_guid_is_little_endian_int_key(self):
        sess = make_session()
        sess._parse_update_object(update_object(object_block(se.UPDATETYPE_CREATE_OBJECT2, CREATURE)))
        self.assertEqual(list(sess.world_state.get_objects()), [CREATURE])

    def test_out_of_range_block_is_skipped(self):
        sess = make_session()
        sess._parse_update_object(update_object(
            out_of_range_block(0x10, 0xF130000000000099),
            object_block(se.UPDATETYPE_VALUES, 2)))
        self.assertEqual(list(sess.world_state.get_objects()), [2])

    def test_truncated_raises_parse_error(self):
        sess = make_session()
        for data in (b'\x01\x00', update_object(b'\x02\xFF\x01'), update_object(out_of_range_block(5))[:-1]):
            with self.subTest(data=data.hex()):
                with self.assertRaises(per.PerceptionParseError):
                    sess._parse_update_object(data)

    def test_unknown_update_type_raises_parse_error(self):
        sess = make_session()
        with self.assertRaises(per.PerceptionParseError):
            sess._parse_update_object(update_object(object_block(9, 2)))

    def test_compressed_inflates_and_dumps(self):
        data = update_object(object_block(se.UPDATETYPE_CREATE_OBJECT, CREATURE))
        with tempfile.TemporaryDirectory() as d:
            dump_dir = os.path.join(d, 'dumps')
            sess = make_session(dump_packets_dir=dump_dir)
            sess._dispatch(se.SMSG_COMPRESSED_UPDATE_OBJECT, compressed(data))
            sess._dispatch(se.SMSG_UPDATE_OBJECT, data)
            names = sorted(os.listdir(dump_dir))
            self.assertEqual(len(names), 2)
            self.assertTrue(any(n.endswith('_0x01f6.bin') for n in names))
            self.assertTrue(any(n.endswith('_0x00a9.bin') for n in names))
            for n in names:
                with open(os.path.join(dump_dir, n), 'rb') as f:
                    self.assertEqual(f.read(), data)
        self.assertIn(CREATURE, sess.world_state.get_objects())


class WorldStateTest(unittest.TestCase):
    def test_set_my_guid_does_not_insert(self):
        ws = per.WorldState()
        ws.set_my_guid(2)
        self.assertEqual(ws.my_guid, 2)
        self.assertEqual(ws.get_objects(), {})
        ws.record_guid(2, se.UPDATETYPE_CREATE_OBJECT2)
        self.assertEqual(list(ws.get_objects()), [2])


class LoginTest(unittest.TestCase):
    def test_login_stores_player_guid(self):
        verify_world = struct.pack('<iffff', 530, 9487.0, -7279.0, 14.3, 0.0)
        sess = make_session(server_packet(se.SMSG_LOGIN_VERIFY_WORLD, verify_world))
        sess._recv_loop = lambda: None  # keep the test single-threaded
        sess.login_character(2)
        sess._recv_thread.join()
        self.assertEqual(sess.player_guid, 2)
        self.assertEqual(sess.world_state.my_guid, 2)
        self.assertEqual(sess.player_position[0], 530)


class RecvLoopSafetyNetTest(unittest.TestCase):
    def test_bad_packets_are_dropped_and_loop_continues(self):
        good = update_object(object_block(se.UPDATETYPE_CREATE_OBJECT2, CREATURE))
        stream = (server_packet(se.SMSG_COMPRESSED_UPDATE_OBJECT, b'\x10\x00\x00\x00garbage')
                  + server_packet(se.SMSG_UPDATE_OBJECT, b'\x01\x00\x00\x00\x02')
                  + server_packet(se.SMSG_UPDATE_OBJECT, b'\x01\x00\x00\x00\x02')
                  + server_packet(se.SMSG_UPDATE_OBJECT, good))
        sess = make_session(stream)
        sess._running = True
        with self.assertLogs('agent.session', level='WARNING') as logs:
            sess._recv_loop()  # runs until the fake socket hits EOF
        self.assertEqual(sess.dropped_packets, 3)
        self.assertIn(CREATURE, sess.world_state.get_objects())
        self.assertFalse(sess._running)
        dropped = [m for m in logs.output if 'dropped' in m]
        self.assertEqual(len(dropped), 2)  # second 0x0a9 error is rate-limited
        self.assertTrue(any('connection lost' in m for m in logs.output))
        self.assertTrue(any('still running' in m for m in logs.output))

    def test_logout_complete_stops_loop_quietly(self):
        sess = make_session(server_packet(se.SMSG_LOGOUT_COMPLETE, b''))
        sess._running = True
        with self.assertNoLogs('agent.session', level='WARNING'):
            sess._recv_loop()
        self.assertFalse(sess._running)


class ErrorThrottleTest(unittest.TestCase):
    def test_once_per_key_per_interval_with_counter(self):
        now = [0.0]
        t = se._ErrorThrottle(30.0, clock=lambda: now[0])
        self.assertEqual(t.check(0xA9), (True, 0))
        self.assertEqual(t.check(0xA9), (False, 0))
        self.assertEqual(t.check(0x1F6), (True, 0))
        now[0] = 29.9
        self.assertEqual(t.check(0xA9), (False, 0))
        now[0] = 30.0
        self.assertEqual(t.check(0xA9), (True, 2))
        self.assertEqual(t.check(0xA9), (False, 0))


if __name__ == '__main__':
    unittest.main()
