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
from agent import update_object as uo
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


def stationary_movement(x=1.0, y=2.0, z=3.0, o=0.5) -> bytes:
    """A minimal, valid movement sub-block: UPDATEFLAG_STATIONARY_POSITION
    only (uint16 flags + 4 floats) — the smallest real conditional path."""
    return struct.pack('<H', uo.UPDATEFLAG_STATIONARY_POSITION) + struct.pack('<4f', x, y, z, o)


def no_values() -> bytes:
    """A VALUES_UPDATE with zero mask words set (nothing changed)."""
    return b'\x00'


def object_block(update_type: int, guid: int, object_type: int = uo.TYPEID_UNIT,
                  movement: bytes = None, values: bytes = None) -> bytes:
    """A CREATE_OBJECT[2] block body: packed guid, object type, movement, values."""
    if movement is None:
        movement = stationary_movement()
    if values is None:
        values = no_values()
    return bytes([update_type]) + pk.pack_packed_guid(guid) + bytes([object_type]) + movement + values


def values_block(guid: int, values: bytes = None) -> bytes:
    """A VALUES block body: packed guid, values."""
    if values is None:
        values = no_values()
    return bytes([se.UPDATETYPE_VALUES]) + pk.pack_packed_guid(guid) + values


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

    def test_out_of_range_block_only_removes_listed_guids(self):
        sess = make_session()
        sess._parse_update_object(update_object(object_block(se.UPDATETYPE_CREATE_OBJECT2, 2)))
        sess._parse_update_object(update_object(
            out_of_range_block(0x10, 0xF130000000000099),
            values_block(2)))
        self.assertEqual(list(sess.world_state.get_objects()), [2])

    def test_values_for_unknown_guid_is_ignored_not_created(self):
        sess = make_session()
        sess._parse_update_object(update_object(values_block(2)))
        self.assertEqual(list(sess.world_state.get_objects()), [])
        self.assertEqual(sess.world_state.unknown_field_updates, 1)

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


def monster_move_payload(guid: int, move_type: int = uo.MONSTER_MOVE_NORMAL,
                          pos=(1.0, 2.0, 3.0), destination=(10.0, 2.0, 3.0),
                          move_time: int = 2000) -> bytes:
    body = pk.pack_packed_guid(guid) + bytes([0]) + struct.pack('<3f', *pos) + struct.pack('<I', 1)
    body += bytes([move_type])
    if move_type != uo.MONSTER_MOVE_STOP:
        body += (struct.pack('<I', 0)             # flags
                 + struct.pack('<I', move_time)
                 + struct.pack('<I', 1)            # point_count
                 + struct.pack('<3f', *destination))
    return body


def heartbeat_payload(guid: int, x=5.0, y=6.0, z=7.0, o=0.0) -> bytes:
    return (pk.pack_packed_guid(guid)
            + struct.pack('<I', 0) + struct.pack('<H', 0) + struct.pack('<I', 1)
            + struct.pack('<4f', x, y, z, o)
            + struct.pack('<I', 0))


class MovementBroadcastTest(unittest.TestCase):
    """UM-64: SMSG_MONSTER_MOVE and MSG_MOVE_* broadcasts, dispatched straight
    to world_state without going through an UpdateBlock."""

    def test_monster_move_starts_spline_for_known_guid(self):
        sess = make_session()
        sess._parse_update_object(update_object(object_block(se.UPDATETYPE_CREATE_OBJECT2, CREATURE)))
        sess._dispatch(se.SMSG_MONSTER_MOVE, monster_move_payload(CREATURE))
        obj = sess.world_state.get_object(CREATURE)
        self.assertIsNotNone(obj.spline)
        self.assertEqual(obj.spline["destination"], (10.0, 2.0, 3.0))
        self.assertEqual(obj.position[1:4], (1.0, 2.0, 3.0))

    def test_monster_move_stop_clears_spline(self):
        sess = make_session()
        sess._parse_update_object(update_object(object_block(se.UPDATETYPE_CREATE_OBJECT2, CREATURE)))
        sess._dispatch(se.SMSG_MONSTER_MOVE, monster_move_payload(CREATURE))
        sess._dispatch(se.SMSG_MONSTER_MOVE, monster_move_payload(CREATURE, move_type=uo.MONSTER_MOVE_STOP))
        self.assertIsNone(sess.world_state.get_object(CREATURE).spline)

    def test_monster_move_for_unknown_guid_is_ignored_and_counted(self):
        sess = make_session()
        sess._dispatch(se.SMSG_MONSTER_MOVE, monster_move_payload(0xDEAD))
        self.assertIsNone(sess.world_state.get_object(0xDEAD))
        self.assertEqual(sess.world_state.unknown_field_updates, 1)

    def test_heartbeat_updates_known_object_position(self):
        sess = make_session()
        sess._parse_update_object(update_object(object_block(se.UPDATETYPE_CREATE_OBJECT2, CREATURE)))
        sess._dispatch(se.MSG_MOVE_HEARTBEAT, heartbeat_payload(CREATURE))
        obj = sess.world_state.get_object(CREATURE)
        self.assertEqual((obj.position[1], obj.position[2], obj.position[3]), (5.0, 6.0, 7.0))

    def test_move_broadcast_for_unknown_guid_is_ignored_and_counted(self):
        sess = make_session()
        sess._dispatch(se.MSG_MOVE_HEARTBEAT, heartbeat_payload(0xDEAD))
        self.assertIsNone(sess.world_state.get_object(0xDEAD))
        self.assertEqual(sess.world_state.unknown_field_updates, 1)

    def test_monster_move_and_heartbeat_are_dumped(self):
        with tempfile.TemporaryDirectory() as d:
            dump_dir = os.path.join(d, 'dumps')
            sess = make_session(dump_packets_dir=dump_dir)
            sess._parse_update_object(update_object(object_block(se.UPDATETYPE_CREATE_OBJECT2, CREATURE)))
            sess._dispatch(se.SMSG_MONSTER_MOVE, monster_move_payload(CREATURE))
            sess._dispatch(se.MSG_MOVE_HEARTBEAT, heartbeat_payload(CREATURE))
            names = os.listdir(dump_dir)
            self.assertTrue(any(n.endswith(f'_{se.SMSG_MONSTER_MOVE:#06x}.bin') for n in names))
            self.assertTrue(any(n.endswith(f'_{se.MSG_MOVE_HEARTBEAT:#06x}.bin') for n in names))


class WorldStateTest(unittest.TestCase):
    def test_set_my_guid_does_not_insert(self):
        ws = per.WorldState()
        ws.set_my_guid(2)
        self.assertEqual(ws.my_guid, 2)
        self.assertEqual(ws.get_objects(), {})
        ws.record_guid(2, se.UPDATETYPE_CREATE_OBJECT2)
        self.assertEqual(list(ws.get_objects()), [2])


def len_string(s: str) -> bytes:
    encoded = s.encode('utf-8') + b'\x00'
    return struct.pack('<I', len(encoded)) + encoded


def messagechat_payload(slash_cmd: int, text: str, sender_guid: int = 1,
                         sender_name: str | None = None, channel: str | None = None) -> bytes:
    """Builds a payload matching WorldPackets::Chat::Chat::Write's default
    branch (ChatPackets.cpp) — see agent/session.py::_handle_messagechat."""
    payload = struct.pack('<Bi', slash_cmd, se.__dict__.get('LANG_ORCISH', 1))
    payload += struct.pack('<Q', sender_guid)
    payload += struct.pack('<I', 0)  # flags
    if sender_name is not None:
        payload += len_string(sender_name)
    if channel is not None:
        payload += channel.encode('utf-8') + b'\x00'
    payload += struct.pack('<Q', 0)  # target_guid
    payload += len_string(text)
    payload += b'\x00'  # chat_tag
    return payload


class ChatParsingTest(unittest.TestCase):
    def test_gm_messagechat_say(self):
        sess = make_session()
        payload = messagechat_payload(0x01, "hello there", sender_guid=1, sender_name="Rubens")
        sess._handle_messagechat(se.SMSG_GM_MESSAGECHAT, payload)
        self.assertEqual(len(sess.chat_inbox), 1)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry, {"kind": "say", "sender_guid": 1, "sender_name": "Rubens",
                                  "channel": None, "text": "hello there"})

    def test_plain_messagechat_no_sender_name(self):
        sess = make_session()
        payload = messagechat_payload(0x07, "psst", sender_guid=2)
        sess._handle_messagechat(se.SMSG_MESSAGECHAT, payload)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry["kind"], "whisper")
        self.assertEqual(entry["sender_name"], "")
        self.assertEqual(entry["text"], "psst")

    def test_channel_message_includes_channel_name(self):
        sess = make_session()
        payload = messagechat_payload(0x11, "LFG dungeon", sender_guid=3,
                                       sender_name="Someone", channel="World")
        sess._handle_messagechat(se.SMSG_GM_MESSAGECHAT, payload)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry["kind"], "channel")
        self.assertEqual(entry["channel"], "World")

    def test_utf8_accents_round_trip(self):
        sess = make_session()
        payload = messagechat_payload(0x01, "Olá, tudo bem?", sender_name="Rubens")
        sess._handle_messagechat(se.SMSG_GM_MESSAGECHAT, payload)
        self.assertEqual(sess.chat_inbox[0]["text"], "Olá, tudo bem?")

    def test_inbox_is_bounded(self):
        sess = make_session()
        for i in range(se.CHAT_INBOX_MAXLEN + 10):
            payload = messagechat_payload(0x01, f"msg {i}", sender_name="X")
            sess._handle_messagechat(se.SMSG_GM_MESSAGECHAT, payload)
        self.assertEqual(len(sess.chat_inbox), se.CHAT_INBOX_MAXLEN)
        self.assertEqual(sess.chat_inbox[-1]["text"], f"msg {se.CHAT_INBOX_MAXLEN + 9}")

    def test_group_invite_sets_pending_invite(self):
        sess = make_session()
        payload = bytes([1]) + b'Rubens\x00'
        sess._handle_group_invite(payload)
        self.assertEqual(sess.pending_invite, {"inviter_name": "Rubens"})


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
