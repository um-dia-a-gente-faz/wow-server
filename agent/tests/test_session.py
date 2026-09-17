"""Tests for agent.session framing, update-object GUIDs and the recv safety net.

No network and no crypto: WoWSession runs against an in-memory fake socket.
"""

import os
import socket
import struct
import tempfile
import unittest
import uuid
import zlib

from agent import names as nm
from agent import loot as lo
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


def values_body(field_values: dict) -> bytes:
    """uint8 mask_block_count, mask words, one uint32 per set bit ascending —
    see agent/tests/test_update_object_parser.py for the same helper."""
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
    # A "found" creature/gameobject query response saves the on-disk name
    # cache — never let that touch the real ~/.cache/wow-agent/names.json.
    sess.world_state.names.cache_path = os.path.join(
        tempfile.gettempdir(), f"wow-agent-test-names-{uuid.uuid4().hex}.json")
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


def _cstr(s: str) -> bytes:
    return s.encode('utf-8') + b'\x00'


def name_query_response_payload(guid: int, name: str) -> bytes:
    return (pk.pack_packed_guid(guid) + bytes([0]) + _cstr(name) + _cstr("")
            + bytes([0, 0, 0, 0]))  # race, sex, class, has_declined_names


def creature_query_response_payload(entry: int, name: str) -> bytes:
    return (struct.pack('<I', entry) + _cstr(name) + bytes([0, 0, 0]) + _cstr("") + _cstr("")
            + struct.pack('<4I', 0, 0, 0, 0)
            + struct.pack(f'<{nm.MAX_KILL_CREDIT}I', *([0] * nm.MAX_KILL_CREDIT))
            + struct.pack(f'<{nm.MAX_CREATURE_MODELS}I', *([0] * nm.MAX_CREATURE_MODELS))
            + struct.pack('<2f', 1.0, 1.0) + bytes([0])
            + struct.pack(f'<{nm.MAX_CREATURE_QUEST_ITEMS}I', *([0] * nm.MAX_CREATURE_QUEST_ITEMS))
            + struct.pack('<I', 0))


def gameobject_query_response_payload(entry: int, name: str) -> bytes:
    return (struct.pack('<I', entry) + struct.pack('<II', 0, 0) + _cstr(name) + bytes([0, 0, 0])
            + _cstr("") + _cstr("") + _cstr("")
            + struct.pack(f'<{nm.MAX_GAMEOBJECT_DATA}I', *([0] * nm.MAX_GAMEOBJECT_DATA))
            + struct.pack('<f', 1.0)
            + struct.pack(f'<{nm.MAX_GAMEOBJECT_QUEST_ITEMS}I', *([0] * nm.MAX_GAMEOBJECT_QUEST_ITEMS)))


class NameQueryTest(unittest.TestCase):
    """UM-35: dispatch wiring for the three query-response opcodes, and the
    recv-loop's budgeted drain-and-send of pending queries."""

    def test_creature_query_response_dispatch(self):
        sess = make_session()
        sess._parse_update_object(update_object(object_block(se.UPDATETYPE_CREATE_OBJECT2, CREATURE,
                                                               values=values_body({0x03: 17213}))))
        sess._dispatch(se.SMSG_CREATURE_QUERY_RESPONSE, creature_query_response_payload(17213, "Broom"))
        self.assertEqual(sess.world_state.get_object(CREATURE).name, "Broom")

    def test_gameobject_query_response_dispatch(self):
        sess = make_session()
        sess._parse_update_object(update_object(object_block(se.UPDATETYPE_CREATE_OBJECT2, CREATURE,
                                                               object_type=uo.TYPEID_GAMEOBJECT,
                                                               values=values_body({0x03: 181646}))))
        sess._dispatch(se.SMSG_GAMEOBJECT_QUERY_RESPONSE, gameobject_query_response_payload(181646, "Ship"))
        self.assertEqual(sess.world_state.get_object(CREATURE).name, "Ship")

    def test_name_query_response_dispatch(self):
        sess = make_session()
        sess._parse_update_object(update_object(object_block(se.UPDATETYPE_CREATE_OBJECT2, 7,
                                                               object_type=uo.TYPEID_PLAYER,
                                                               values=values_body({0x03: 0}))))
        sess._dispatch(se.SMSG_NAME_QUERY_RESPONSE, name_query_response_payload(7, "Rubens"))
        self.assertEqual(sess.world_state.get_object(7).name, "Rubens")

    def test_send_name_queries_drains_and_sends_creature_query(self):
        sess = make_session()
        sess._parse_update_object(update_object(object_block(se.UPDATETYPE_CREATE_OBJECT2, CREATURE,
                                                               values=values_body({0x03: 17213}))))
        sess._send_name_queries()
        # the sent bytes must contain the entry+guid payload somewhere in a CMSG_CREATURE_QUERY frame
        expected_payload = nm.build_creature_query(17213, CREATURE)
        self.assertIn(expected_payload, sess.sock.sent)

    def test_send_name_queries_is_empty_when_nothing_pending(self):
        sess = make_session()
        sess._send_name_queries()
        self.assertEqual(sess.sock.sent, b'')


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


UNIT_GUID = 0xF130000000000042      # HighGuid::Unit (0xF130) — not a player or pet
PET_GUID = 0xF140000000000042       # HighGuid::Pet
PLAYER_GUID = 0x0000000000000007    # HighGuid::Player (top 16 bits zero)


def monster_chat_payload(slash_cmd: int, text: str, sender_name: str,
                          sender_guid: int = 1, target_guid: int = 0,
                          target_name: str | None = None) -> bytes:
    """WorldPackets::Chat::Chat::Write's CHAT_KINDS_MONSTER branch."""
    payload = struct.pack('<Bi', slash_cmd, 1)
    payload += struct.pack('<Q', sender_guid)
    payload += struct.pack('<I', 0)  # flags
    payload += len_string(sender_name)
    payload += struct.pack('<Q', target_guid)
    if target_name is not None:
        payload += len_string(target_name)
    payload += len_string(text)
    payload += b'\x00'  # chat_tag
    return payload


def whisper_foreign_payload(text: str, sender_name: str, sender_guid: int = 1,
                             target_guid: int = 0) -> bytes:
    payload = struct.pack('<Bi', se.CHAT_MSG_WHISPER_FOREIGN, 1)
    payload += struct.pack('<Q', sender_guid)
    payload += struct.pack('<I', 0)
    payload += len_string(sender_name)
    payload += struct.pack('<Q', target_guid)
    payload += len_string(text)
    payload += b'\x00'
    return payload


def bg_system_payload(text: str, sender_guid: int = 1, target_guid: int = 0,
                       target_name: str | None = None) -> bytes:
    payload = struct.pack('<Bi', se.CHAT_MSG_BG_SYSTEM_NEUTRAL, 1)
    payload += struct.pack('<Q', sender_guid)
    payload += struct.pack('<I', 0)
    payload += struct.pack('<Q', target_guid)
    if target_name is not None:
        payload += len_string(target_name)
    payload += len_string(text)
    payload += b'\x00'
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

    def test_monster_say_no_target(self):
        sess = make_session()
        payload = monster_chat_payload(se.CHAT_MSG_MONSTER_SAY,
                                        "Remain strong. Lor'themar will lead you to power and glory!",
                                        sender_name="Silvermoon City Guardian")
        sess._handle_messagechat(se.SMSG_MESSAGECHAT, payload)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry["kind"], "monster_say")
        self.assertEqual(entry["sender_name"], "Silvermoon City Guardian")
        self.assertEqual(entry["text"], "Remain strong. Lor'themar will lead you to power and glory!")
        self.assertNotIn("target_name", entry)

    def test_monster_whisper_with_unit_target_reads_target_name(self):
        sess = make_session()
        payload = monster_chat_payload(se.CHAT_MSG_MONSTER_WHISPER, "Heel!", sender_name="Hound Master",
                                        target_guid=UNIT_GUID, target_name="Wolf")
        sess._handle_messagechat(se.SMSG_MESSAGECHAT, payload)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry["kind"], "monster_whisper")
        self.assertEqual(entry["target_name"], "Wolf")

    def test_monster_chat_with_player_target_has_no_target_name(self):
        sess = make_session()
        payload = monster_chat_payload(se.CHAT_MSG_MONSTER_WHISPER, "hi", sender_name="Guard",
                                        target_guid=PLAYER_GUID)
        sess._handle_messagechat(se.SMSG_MESSAGECHAT, payload)
        self.assertNotIn("target_name", sess.chat_inbox[0])

    def test_monster_chat_with_pet_target_has_no_target_name(self):
        sess = make_session()
        payload = monster_chat_payload(se.CHAT_MSG_MONSTER_SAY, "grr", sender_name="Beast",
                                        target_guid=PET_GUID)
        sess._handle_messagechat(se.SMSG_MESSAGECHAT, payload)
        self.assertNotIn("target_name", sess.chat_inbox[0])

    def test_raid_boss_emote(self):
        sess = make_session()
        payload = monster_chat_payload(se.CHAT_MSG_RAID_BOSS_EMOTE, "roars!", sender_name="Boss")
        sess._handle_messagechat(se.SMSG_MESSAGECHAT, payload)
        self.assertEqual(sess.chat_inbox[0]["kind"], "raid_boss_emote")

    def test_whisper_foreign(self):
        sess = make_session()
        payload = whisper_foreign_payload("psst", sender_name="SomeoneOnAnotherRealm")
        sess._handle_messagechat(se.SMSG_MESSAGECHAT, payload)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry["kind"], "whisper_foreign")
        self.assertEqual(entry["sender_name"], "SomeoneOnAnotherRealm")

    def test_bg_system_with_unit_target_reads_target_name(self):
        sess = make_session()
        payload = bg_system_payload("The flag has been captured!", target_guid=UNIT_GUID, target_name="Flag")
        sess._handle_messagechat(se.SMSG_MESSAGECHAT, payload)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry["kind"], "bg_system_neutral")
        self.assertEqual(entry["target_name"], "Flag")

    def test_bg_system_with_player_target_has_no_target_name(self):
        sess = make_session()
        payload = bg_system_payload("Welcome!", target_guid=PLAYER_GUID)
        sess._handle_messagechat(se.SMSG_MESSAGECHAT, payload)
        self.assertNotIn("target_name", sess.chat_inbox[0])

    def test_party_leader_and_raid_leader_kinds_are_named(self):
        sess = make_session()
        sess._handle_messagechat(se.SMSG_GM_MESSAGECHAT,
                                  messagechat_payload(se.CHAT_MSG_PARTY_LEADER, "let's go", sender_name="Rubens"))
        self.assertEqual(sess.chat_inbox[0]["kind"], "party_leader")
        sess._handle_messagechat(se.SMSG_GM_MESSAGECHAT,
                                  messagechat_payload(se.CHAT_MSG_RAID_LEADER, "pull", sender_name="Rubens"))
        self.assertEqual(sess.chat_inbox[1]["kind"], "raid_leader")

    def test_no_unnamed_kinds_left_in_chatmsg_enum(self):
        # Every ChatMsg value from 0x00 to 0x33 (SharedDefines.h) must have a
        # name — anything falling back to "type_<n>" here is a gap.
        for value in list(range(0x34)) + [0xFF]:
            self.assertIn(value, se.CHAT_KIND_NAMES, f"unnamed ChatMsg {value:#04x}")

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

    def test_login_sends_set_active_mover(self):
        # UM-36: without CMSG_SET_ACTIVE_MOVER, WorldSession::
        # ValidateAndGetUnitBeingMoved silently drops every MSG_MOVE_* we
        # send (found live-verifying the face action) — a real client sends
        # this right after login, so we must too.
        verify_world = struct.pack('<iffff', 530, 9487.0, -7279.0, 14.3, 0.0)
        sess = make_session(server_packet(se.SMSG_LOGIN_VERIFY_WORLD, verify_world))
        sess._recv_loop = lambda: None
        sess.login_character(7)
        sess._recv_thread.join()
        # Unencrypted here (no crypt set up), so the packet's raw bytes —
        # header (size, opcode) + uint64 guid payload — appear as-is.
        expected = struct.pack('>H', 4 + 8) + struct.pack('<I', se.CMSG_SET_ACTIVE_MOVER) + struct.pack('<Q', 7)
        self.assertIn(expected, sess.sock.sent)


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


class LootDispatchTest(unittest.TestCase):
    """UM-42: SMSG_LOOT_RESPONSE / _RELEASE_RESPONSE / _REMOVED /
    _MONEY_NOTIFY / SMSG_ITEM_PUSH_RESULT / SMSG_INVENTORY_CHANGE_FAILURE /
    SMSG_ITEM_QUERY_SINGLE_RESPONSE dispatch wiring."""

    def test_loot_response_sets_session_loot_and_records_event(self):
        sess = make_session()
        payload = struct.pack('<Q', 5) + bytes([lo.LOOT_CORPSE]) + struct.pack('<IB', 10, 0)
        sess._dispatch(se.SMSG_LOOT_RESPONSE, payload)
        self.assertEqual(sess.loot["guid"], 5)
        self.assertTrue(sess.loot["success"])
        self.assertEqual(sess.events[-1]["kind"], "loot_response")

    def test_loot_release_response_clears_matching_loot(self):
        sess = make_session()
        sess.loot = {"guid": 5, "success": True, "items": []}
        sess._dispatch(se.SMSG_LOOT_RELEASE_RESPONSE, struct.pack('<Q', 5) + bytes([1]))
        self.assertIsNone(sess.loot)

    def test_loot_release_response_ignores_mismatched_guid(self):
        sess = make_session()
        sess.loot = {"guid": 5, "success": True, "items": []}
        sess._dispatch(se.SMSG_LOOT_RELEASE_RESPONSE, struct.pack('<Q', 999) + bytes([1]))
        self.assertIsNotNone(sess.loot)

    def test_loot_removed_drops_item_from_session_loot(self):
        sess = make_session()
        sess.loot = {"guid": 5, "success": True, "items": [{"slot": 0, "entry": 1}, {"slot": 1, "entry": 2}]}
        sess._dispatch(se.SMSG_LOOT_REMOVED, bytes([0]))
        self.assertEqual(sess.loot["items"], [{"slot": 1, "entry": 2}])

    def test_loot_money_notify_records_event(self):
        sess = make_session()
        sess.loot = {"guid": 5, "success": True, "items": [], "coins": 12}
        sess._dispatch(se.SMSG_LOOT_MONEY_NOTIFY, struct.pack('<I', 12) + bytes([1]))
        self.assertEqual(sess.loot["coins"], 0)
        self.assertEqual(sess.events[-1]["kind"], "loot_money")
        self.assertEqual(sess.events[-1]["money"], 12)

    def test_item_push_result_records_item_received_event(self):
        sess = make_session()
        payload = (struct.pack('<Q', 1) + struct.pack('<III', 1, 0, 1) + bytes([0])
                   + struct.pack('<I', 23) + struct.pack('<I', 159) + struct.pack('<I', 0)
                   + struct.pack('<i', -1) + struct.pack('<II', 1, 1))
        sess._dispatch(se.SMSG_ITEM_PUSH_RESULT, payload)
        self.assertEqual(sess.events[-1]["kind"], "item_received")
        self.assertEqual(sess.events[-1]["entry"], 159)

    def test_inventory_change_failure_records_event(self):
        sess = make_session()
        payload = (bytes([lo.EQUIP_ERR_INV_FULL]) + struct.pack('<Q', 0) + struct.pack('<Q', 0)
                   + bytes([0]) + struct.pack('<i', 0))
        sess._dispatch(se.SMSG_INVENTORY_CHANGE_FAILURE, payload)
        self.assertEqual(sess.events[-1]["kind"], "inventory_change_failure")
        self.assertFalse(sess.events[-1]["ok"])

    def test_item_query_response_populates_world_state_cache(self):
        sess = make_session()
        payload = struct.pack('<I', 999999 | 0x80000000)  # "not found" (short payload, valid)
        sess._dispatch(se.SMSG_ITEM_QUERY_SINGLE_RESPONSE, payload)
        self.assertIn(999999, sess.world_state.items.items)
        self.assertIsNone(sess.world_state.items.items[999999])

    def test_money_changed_event_on_coinage_delta(self):
        sess = make_session()
        sess.player_guid = CREATURE
        sess.world_state.set_my_guid(CREATURE)
        block = uo.UpdateBlock(update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=CREATURE,
                                object_type=uo.TYPEID_PLAYER,
                                movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION,
                                          "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0},
                                fields={})
        sess.world_state.update_object(block)
        sess._sync_self_from_block(block)
        self.assertIsNone(sess.coinage)

        from agent import update_fields as uf
        block2 = uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=CREATURE,
                                 fields={uf.PLAYER_FIELD_COINAGE: 100})
        sess.world_state.update_object(block2)
        sess._sync_self_from_block(block2)
        self.assertEqual(sess.coinage, 100)
        self.assertEqual(list(sess.events), [])  # first-ever coinage isn't a "change"

        block3 = uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=CREATURE,
                                 fields={uf.PLAYER_FIELD_COINAGE: 150})
        sess.world_state.update_object(block3)
        sess._sync_self_from_block(block3)
        self.assertEqual(sess.coinage, 150)
        self.assertEqual(sess.events[-1], {**sess.events[-1], "kind": "money_changed",
                                            "old": 100, "new": 150, "delta": 50})


if __name__ == '__main__':
    unittest.main()
