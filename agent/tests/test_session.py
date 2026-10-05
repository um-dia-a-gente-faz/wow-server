"""Tests for agent.session framing, update-object GUIDs and the recv safety net.

No network and no crypto: WoWSession runs against an in-memory fake socket.
"""

import os
import pathlib
import socket
import struct
import tempfile
import time
import unittest
import uuid
import zlib

from agent import mail as mail_mod
from agent import names as nm
from agent import npc as npc_mod
from agent import loot as lo
from agent import packets as pk
from agent import perception as per
from agent import session as se
from agent.handlers import npc as hnpc
from agent.handlers import chat as hchat
from agent.handlers import death as hdeath
from agent.handlers import world as hworld
from agent import state as state_mod
from agent import trade as tr
from agent import update_fields as uf
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
    return bytes([uo.UPDATETYPE_VALUES]) + pk.pack_packed_guid(guid) + values


def out_of_range_block(*guids: int) -> bytes:
    return (bytes([hworld.UPDATETYPE_OUT_OF_RANGE_OBJECTS]) + struct.pack('<I', len(guids))
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
        stream = (server_packet(hworld.SMSG_COMPRESSED_UPDATE_OBJECT, big)
                  + server_packet(se.SMSG_PONG, b'\x05\x00\x00\x00'))
        sess = make_session(stream)
        sess.sock.chunk = 4096
        self.assertEqual(sess._recv_packet(), (hworld.SMSG_COMPRESSED_UPDATE_OBJECT, big))
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
        hworld.parse_update_object(sess.ctx, update_object(object_block(uo.UPDATETYPE_CREATE_OBJECT2, CREATURE)))
        self.assertEqual(list(sess.world_state.get_objects()), [CREATURE])

    def test_out_of_range_block_only_removes_listed_guids(self):
        sess = make_session()
        hworld.parse_update_object(sess.ctx, update_object(object_block(uo.UPDATETYPE_CREATE_OBJECT2, 2)))
        hworld.parse_update_object(sess.ctx, update_object(
            out_of_range_block(0x10, 0xF130000000000099),
            values_block(2)))
        self.assertEqual(list(sess.world_state.get_objects()), [2])

    def test_values_for_unknown_guid_is_ignored_not_created(self):
        sess = make_session()
        hworld.parse_update_object(sess.ctx, update_object(values_block(2)))
        self.assertEqual(list(sess.world_state.get_objects()), [])
        self.assertEqual(sess.world_state.unknown_field_updates, 1)

    def test_truncated_raises_parse_error(self):
        sess = make_session()
        for data in (b'\x01\x00', update_object(b'\x02\xFF\x01'), update_object(out_of_range_block(5))[:-1]):
            with self.subTest(data=data.hex()):
                with self.assertRaises(per.PerceptionParseError):
                    hworld.parse_update_object(sess.ctx, data)

    def test_unknown_update_type_raises_parse_error(self):
        sess = make_session()
        with self.assertRaises(per.PerceptionParseError):
            hworld.parse_update_object(sess.ctx, update_object(object_block(9, 2)))

    def test_compressed_inflates_and_dumps(self):
        data = update_object(object_block(uo.UPDATETYPE_CREATE_OBJECT, CREATURE))
        with tempfile.TemporaryDirectory() as d:
            dump_dir = os.path.join(d, 'dumps')
            sess = make_session(dump_packets_dir=dump_dir)
            sess._dispatch(hworld.SMSG_COMPRESSED_UPDATE_OBJECT, compressed(data))
            sess._dispatch(hworld.SMSG_UPDATE_OBJECT, data)
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
        hworld.parse_update_object(sess.ctx, update_object(object_block(uo.UPDATETYPE_CREATE_OBJECT2, CREATURE)))
        sess._dispatch(hworld.SMSG_MONSTER_MOVE, monster_move_payload(CREATURE))
        obj = sess.world_state.get_object(CREATURE)
        self.assertIsNotNone(obj.spline)
        self.assertEqual(obj.spline["destination"], (10.0, 2.0, 3.0))
        self.assertEqual(obj.position[1:4], (1.0, 2.0, 3.0))

    def test_monster_move_stop_clears_spline(self):
        sess = make_session()
        hworld.parse_update_object(sess.ctx, update_object(object_block(uo.UPDATETYPE_CREATE_OBJECT2, CREATURE)))
        sess._dispatch(hworld.SMSG_MONSTER_MOVE, monster_move_payload(CREATURE))
        sess._dispatch(hworld.SMSG_MONSTER_MOVE, monster_move_payload(CREATURE, move_type=uo.MONSTER_MOVE_STOP))
        self.assertIsNone(sess.world_state.get_object(CREATURE).spline)

    def test_monster_move_for_unknown_guid_is_ignored_and_counted(self):
        sess = make_session()
        sess._dispatch(hworld.SMSG_MONSTER_MOVE, monster_move_payload(0xDEAD))
        self.assertIsNone(sess.world_state.get_object(0xDEAD))
        self.assertEqual(sess.world_state.unknown_field_updates, 1)

    def test_heartbeat_updates_known_object_position(self):
        sess = make_session()
        hworld.parse_update_object(sess.ctx, update_object(object_block(uo.UPDATETYPE_CREATE_OBJECT2, CREATURE)))
        sess._dispatch(hworld.MSG_MOVE_HEARTBEAT, heartbeat_payload(CREATURE))
        obj = sess.world_state.get_object(CREATURE)
        self.assertEqual((obj.position[1], obj.position[2], obj.position[3]), (5.0, 6.0, 7.0))

    def test_move_broadcast_for_unknown_guid_is_ignored_and_counted(self):
        sess = make_session()
        sess._dispatch(hworld.MSG_MOVE_HEARTBEAT, heartbeat_payload(0xDEAD))
        self.assertIsNone(sess.world_state.get_object(0xDEAD))
        self.assertEqual(sess.world_state.unknown_field_updates, 1)

    def test_monster_move_and_heartbeat_are_dumped(self):
        with tempfile.TemporaryDirectory() as d:
            dump_dir = os.path.join(d, 'dumps')
            sess = make_session(dump_packets_dir=dump_dir)
            hworld.parse_update_object(sess.ctx, update_object(object_block(uo.UPDATETYPE_CREATE_OBJECT2, CREATURE)))
            sess._dispatch(hworld.SMSG_MONSTER_MOVE, monster_move_payload(CREATURE))
            sess._dispatch(hworld.MSG_MOVE_HEARTBEAT, heartbeat_payload(CREATURE))
            names = os.listdir(dump_dir)
            self.assertTrue(any(n.endswith(f'_{hworld.SMSG_MONSTER_MOVE:#06x}.bin') for n in names))
            self.assertTrue(any(n.endswith(f'_{hworld.MSG_MOVE_HEARTBEAT:#06x}.bin') for n in names))


class WorldStateTest(unittest.TestCase):
    def test_set_my_guid_does_not_insert(self):
        ws = per.WorldState()
        ws.set_my_guid(2)
        self.assertEqual(ws.my_guid, 2)
        self.assertEqual(ws.get_objects(), {})
        ws.record_guid(2, uo.UPDATETYPE_CREATE_OBJECT2)
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
        hworld.parse_update_object(sess.ctx, update_object(object_block(uo.UPDATETYPE_CREATE_OBJECT2, CREATURE,
                                                               values=values_body({0x03: 17213}))))
        sess._dispatch(hworld.SMSG_CREATURE_QUERY_RESPONSE, creature_query_response_payload(17213, "Broom"))
        self.assertEqual(sess.world_state.get_object(CREATURE).name, "Broom")

    def test_gameobject_query_response_dispatch(self):
        sess = make_session()
        hworld.parse_update_object(sess.ctx, update_object(object_block(uo.UPDATETYPE_CREATE_OBJECT2, CREATURE,
                                                               object_type=uo.TYPEID_GAMEOBJECT,
                                                               values=values_body({0x03: 181646}))))
        sess._dispatch(hworld.SMSG_GAMEOBJECT_QUERY_RESPONSE, gameobject_query_response_payload(181646, "Ship"))
        self.assertEqual(sess.world_state.get_object(CREATURE).name, "Ship")

    def test_name_query_response_dispatch(self):
        sess = make_session()
        hworld.parse_update_object(sess.ctx, update_object(object_block(uo.UPDATETYPE_CREATE_OBJECT2, 7,
                                                               object_type=uo.TYPEID_PLAYER,
                                                               values=values_body({0x03: 0}))))
        sess._dispatch(hworld.SMSG_NAME_QUERY_RESPONSE, name_query_response_payload(7, "Rubens"))
        self.assertEqual(sess.world_state.get_object(7).name, "Rubens")

    def test_send_name_queries_drains_and_sends_creature_query(self):
        sess = make_session()
        hworld.parse_update_object(sess.ctx, update_object(object_block(uo.UPDATETYPE_CREATE_OBJECT2, CREATURE,
                                                               values=values_body({0x03: 17213}))))
        hworld.send_name_queries(sess.ctx)
        # the sent bytes must contain the entry+guid payload somewhere in a CMSG_CREATURE_QUERY frame
        expected_payload = nm.build_creature_query(17213, CREATURE)
        self.assertIn(expected_payload, sess.sock.sent)

    def test_send_name_queries_is_empty_when_nothing_pending(self):
        sess = make_session()
        hworld.send_name_queries(sess.ctx)
        self.assertEqual(sess.sock.sent, b'')


class NpcInteractionDispatchTest(unittest.TestCase):
    """UM-40: dispatch wiring for gossip/vendor/trainer/npc-text opcodes."""

    def test_gossip_message_dispatch_opens_window(self):
        sess = make_session()
        payload = (struct.pack('<Q', 5) + struct.pack('<i', 1) + struct.pack('<i', 999)
                   + struct.pack('<I', 0) + struct.pack('<I', 0))
        sess._dispatch(npc_mod.SMSG_GOSSIP_MESSAGE, payload)
        window = sess.world_state.get_ui_state()
        self.assertEqual(window["kind"], "gossip")
        self.assertEqual(window["npc_guid"], 5)
        # the missing npc text got queued
        self.assertEqual(sess.world_state.npc_texts.drain(), [(999, 5)])

    def test_gossip_complete_dispatch_closes_window(self):
        sess = make_session()
        sess.world_state.ui_state = {"kind": "gossip"}
        sess._dispatch(npc_mod.SMSG_GOSSIP_COMPLETE, b'')
        self.assertIsNone(sess.world_state.get_ui_state())

    def test_list_inventory_dispatch_opens_vendor_window(self):
        sess = make_session()
        payload = struct.pack('<Q', 5) + bytes([0])
        sess._dispatch(npc_mod.SMSG_LIST_INVENTORY, payload)
        self.assertEqual(sess.world_state.get_ui_state()["kind"], "vendor")

    def test_trainer_list_dispatch_opens_trainer_window(self):
        sess = make_session()
        payload = struct.pack('<Q', 5) + struct.pack('<i', 0) + struct.pack('<i', 0) + b'\x00'
        sess._dispatch(npc_mod.SMSG_TRAINER_LIST, payload)
        self.assertEqual(sess.world_state.get_ui_state()["kind"], "trainer")

    def test_npc_text_update_dispatch_backfills_gossip_window(self):
        sess = make_session()
        sess.world_state.apply_gossip_message({"npc_guid": 5, "menu_id": 1, "text_id": 999,
                                                 "options": [], "quests": []})
        option = (struct.pack('<f', 1.0) + b'Hi\x00' + b'\x00' + struct.pack('<i', 0)
                   + struct.pack('<6I', 0, 0, 0, 0, 0, 0))
        payload = struct.pack('<I', 999) + option * npc_mod.MAX_NPC_TEXT_OPTIONS
        sess._dispatch(npc_mod.SMSG_NPC_TEXT_UPDATE, payload)
        self.assertEqual(sess.world_state.get_ui_state()["body_text"], "Hi")

    def test_send_npc_text_queries_drains_and_sends(self):
        sess = make_session()
        sess.world_state.apply_gossip_message({"npc_guid": 5, "menu_id": 1, "text_id": 999,
                                                 "options": [], "quests": []})
        hnpc.send_npc_text_queries(sess.ctx)
        self.assertIn(npc_mod.build_npc_text_query(999, 5), sess.sock.sent)


class MailDispatchTest(unittest.TestCase):
    """UM-60: dispatch wiring for SMSG_SEND_MAIL_RESULT/SMSG_MAIL_LIST_RESULT/
    SMSG_RECEIVED_MAIL."""

    def test_send_mail_result_records_raw_mail_result_event(self):
        sess = make_session()
        payload = struct.pack('<III', 5, mail_mod.MAIL_SEND, mail_mod.MAIL_OK)
        sess._dispatch(mail_mod.SMSG_SEND_MAIL_RESULT, payload)
        results = [e for e in sess.events if e["kind"] == "mail_result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["mail_id"], 5)
        self.assertEqual(results[0]["command_name"], "send")

    def test_mail_list_result_dispatch_fills_pending_mailbox(self):
        sess = make_session()
        sess.world_state.open_mailbox_request(0x1234)
        payload = struct.pack('<iB', 0, 0)
        sess._dispatch(mail_mod.SMSG_MAIL_LIST_RESULT, payload)
        mailbox = sess.world_state.get_mailbox()
        self.assertEqual(mailbox["mailbox_guid"], 0x1234)
        self.assertEqual(mailbox["total_records"], 0)

    def test_received_mail_dispatch_sets_flag_and_records_event(self):
        sess = make_session()
        payload = struct.pack('<f', 0.0)
        sess._dispatch(mail_mod.SMSG_RECEIVED_MAIL, payload)
        self.assertTrue(sess.world_state.snapshot()["has_new_mail"])
        received = [e for e in sess.events if e["kind"] == "mail_received"]
        self.assertEqual(len(received), 1)

class TradeDispatchTest(unittest.TestCase):
    """UM-59: dispatch wiring for SMSG_TRADE_STATUS/SMSG_TRADE_STATUS_EXTENDED
    — parsing lands in world_state.trade, and every status is recorded as a
    raw 'trade_status' event (agent/actions/trade.py's offer_item/offer_gold wait on
    that directly — see agent/trade.py's docstring for why)."""

    def test_begin_trade_opens_request_and_records_events(self):
        sess = make_session()
        payload = struct.pack('<IQ', tr.TRADE_STATUS_BEGIN_TRADE, 0x555)
        sess._dispatch(tr.SMSG_TRADE_STATUS, payload)
        trade_state = sess.world_state.get_trade()
        self.assertEqual(trade_state["phase"], "requested")
        self.assertEqual(trade_state["partner_guid"], 0x555)
        kinds = [e["kind"] for e in sess.events]
        self.assertIn("trade_status", kinds)
        self.assertIn("trade_requested", kinds)

    def test_open_window_dispatch_advances_phase_with_no_named_event(self):
        sess = make_session()
        sess.world_state.start_trade_request(0x555, initiated_by_me=True)
        payload = struct.pack('<II', tr.TRADE_STATUS_OPEN_WINDOW, 0)
        sess._dispatch(tr.SMSG_TRADE_STATUS, payload)
        self.assertEqual(sess.world_state.get_trade()["phase"], "open")
        self.assertEqual([e["kind"] for e in sess.events], ["trade_status"])

    def test_trade_complete_dispatch_clears_state_and_records_summary(self):
        sess = make_session()
        sess.world_state.start_trade_request(0x555, initiated_by_me=True)
        payload = struct.pack('<I', tr.TRADE_STATUS_TRADE_COMPLETE)
        sess._dispatch(tr.SMSG_TRADE_STATUS, payload)
        self.assertIsNone(sess.world_state.get_trade())
        completed = [e for e in sess.events if e["kind"] == "trade_completed"]
        self.assertEqual(len(completed), 1)
        self.assertIn("summary", completed[0])

    def test_trade_status_extended_dispatch_updates_their_offer(self):
        sess = make_session()
        sess.world_state.start_trade_request(0x555, initiated_by_me=True)
        payload = struct.pack('<BIIII', 1, 0, tr.TRADE_SLOT_COUNT, tr.TRADE_SLOT_COUNT, 1234) \
            + struct.pack('<I', 0) + b'\x00' * (tr.TRADE_SLOT_COUNT * (1 + 4 * 18))
        sess._dispatch(tr.SMSG_TRADE_STATUS_EXTENDED, payload)
        self.assertEqual(sess.world_state.get_trade()["their_gold"], 1234)
        changed = [e for e in sess.events if e["kind"] == "trade_offer_changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["gold"], 1234)


def len_string(s: str) -> bytes:
    encoded = s.encode('utf-8') + b'\x00'
    return struct.pack('<I', len(encoded)) + encoded


def messagechat_payload(slash_cmd: int, text: str, sender_guid: int = 1,
                         sender_name: str | None = None, channel: str | None = None) -> bytes:
    """Builds a payload matching WorldPackets::Chat::Chat::Write's default
    branch (ChatPackets.cpp) — see agent/handlers/chat.py::handle_messagechat."""
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
    payload = struct.pack('<Bi', hchat.CHAT_MSG_WHISPER_FOREIGN, 1)
    payload += struct.pack('<Q', sender_guid)
    payload += struct.pack('<I', 0)
    payload += len_string(sender_name)
    payload += struct.pack('<Q', target_guid)
    payload += len_string(text)
    payload += b'\x00'
    return payload


def bg_system_payload(text: str, sender_guid: int = 1, target_guid: int = 0,
                       target_name: str | None = None) -> bytes:
    payload = struct.pack('<Bi', hchat.CHAT_MSG_BG_SYSTEM_NEUTRAL, 1)
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
        sess._dispatch(hchat.SMSG_GM_MESSAGECHAT, payload)
        self.assertEqual(len(sess.chat_inbox), 1)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry, {"kind": "say", "sender_guid": 1, "sender_name": "Rubens",
                                  "channel": None, "text": "hello there"})

    def test_plain_messagechat_no_sender_name(self):
        sess = make_session()
        payload = messagechat_payload(0x07, "psst", sender_guid=2)
        sess._dispatch(hchat.SMSG_MESSAGECHAT, payload)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry["kind"], "whisper")
        self.assertEqual(entry["sender_name"], "")
        self.assertEqual(entry["text"], "psst")

    def test_channel_message_includes_channel_name(self):
        sess = make_session()
        payload = messagechat_payload(0x11, "LFG dungeon", sender_guid=3,
                                       sender_name="Someone", channel="World")
        sess._dispatch(hchat.SMSG_GM_MESSAGECHAT, payload)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry["kind"], "channel")
        self.assertEqual(entry["channel"], "World")

    def test_utf8_accents_round_trip(self):
        sess = make_session()
        payload = messagechat_payload(0x01, "Olá, tudo bem?", sender_name="Rubens")
        sess._dispatch(hchat.SMSG_GM_MESSAGECHAT, payload)
        self.assertEqual(sess.chat_inbox[0]["text"], "Olá, tudo bem?")

    def test_inbox_is_bounded(self):
        sess = make_session()
        for i in range(state_mod.CHAT_INBOX_MAXLEN + 10):
            payload = messagechat_payload(0x01, f"msg {i}", sender_name="X")
            sess._dispatch(hchat.SMSG_GM_MESSAGECHAT, payload)
        self.assertEqual(len(sess.chat_inbox), state_mod.CHAT_INBOX_MAXLEN)
        self.assertEqual(sess.chat_inbox[-1]["text"], f"msg {state_mod.CHAT_INBOX_MAXLEN + 9}")

    def test_monster_say_no_target(self):
        sess = make_session()
        payload = monster_chat_payload(hchat.CHAT_MSG_MONSTER_SAY,
                                        "Remain strong. Lor'themar will lead you to power and glory!",
                                        sender_name="Silvermoon City Guardian")
        sess._dispatch(hchat.SMSG_MESSAGECHAT, payload)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry["kind"], "monster_say")
        self.assertEqual(entry["sender_name"], "Silvermoon City Guardian")
        self.assertEqual(entry["text"], "Remain strong. Lor'themar will lead you to power and glory!")
        self.assertNotIn("target_name", entry)

    def test_monster_whisper_with_unit_target_reads_target_name(self):
        sess = make_session()
        payload = monster_chat_payload(hchat.CHAT_MSG_MONSTER_WHISPER, "Heel!", sender_name="Hound Master",
                                        target_guid=UNIT_GUID, target_name="Wolf")
        sess._dispatch(hchat.SMSG_MESSAGECHAT, payload)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry["kind"], "monster_whisper")
        self.assertEqual(entry["target_name"], "Wolf")

    def test_monster_chat_with_player_target_has_no_target_name(self):
        sess = make_session()
        payload = monster_chat_payload(hchat.CHAT_MSG_MONSTER_WHISPER, "hi", sender_name="Guard",
                                        target_guid=PLAYER_GUID)
        sess._dispatch(hchat.SMSG_MESSAGECHAT, payload)
        self.assertNotIn("target_name", sess.chat_inbox[0])

    def test_monster_chat_with_pet_target_has_no_target_name(self):
        sess = make_session()
        payload = monster_chat_payload(hchat.CHAT_MSG_MONSTER_SAY, "grr", sender_name="Beast",
                                        target_guid=PET_GUID)
        sess._dispatch(hchat.SMSG_MESSAGECHAT, payload)
        self.assertNotIn("target_name", sess.chat_inbox[0])

    def test_raid_boss_emote(self):
        sess = make_session()
        payload = monster_chat_payload(hchat.CHAT_MSG_RAID_BOSS_EMOTE, "roars!", sender_name="Boss")
        sess._dispatch(hchat.SMSG_MESSAGECHAT, payload)
        self.assertEqual(sess.chat_inbox[0]["kind"], "raid_boss_emote")

    def test_whisper_foreign(self):
        sess = make_session()
        payload = whisper_foreign_payload("psst", sender_name="SomeoneOnAnotherRealm")
        sess._dispatch(hchat.SMSG_MESSAGECHAT, payload)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry["kind"], "whisper_foreign")
        self.assertEqual(entry["sender_name"], "SomeoneOnAnotherRealm")

    def test_bg_system_with_unit_target_reads_target_name(self):
        sess = make_session()
        payload = bg_system_payload("The flag has been captured!", target_guid=UNIT_GUID, target_name="Flag")
        sess._dispatch(hchat.SMSG_MESSAGECHAT, payload)
        entry = sess.chat_inbox[0]
        self.assertEqual(entry["kind"], "bg_system_neutral")
        self.assertEqual(entry["target_name"], "Flag")

    def test_bg_system_with_player_target_has_no_target_name(self):
        sess = make_session()
        payload = bg_system_payload("Welcome!", target_guid=PLAYER_GUID)
        sess._dispatch(hchat.SMSG_MESSAGECHAT, payload)
        self.assertNotIn("target_name", sess.chat_inbox[0])

    def test_party_leader_and_raid_leader_kinds_are_named(self):
        sess = make_session()
        sess._dispatch(hchat.SMSG_GM_MESSAGECHAT,
                                  messagechat_payload(hchat.CHAT_MSG_PARTY_LEADER, "let's go", sender_name="Rubens"))
        self.assertEqual(sess.chat_inbox[0]["kind"], "party_leader")
        sess._dispatch(hchat.SMSG_GM_MESSAGECHAT,
                                  messagechat_payload(hchat.CHAT_MSG_RAID_LEADER, "pull", sender_name="Rubens"))
        self.assertEqual(sess.chat_inbox[1]["kind"], "raid_leader")

    def test_no_unnamed_kinds_left_in_chatmsg_enum(self):
        # Every ChatMsg value from 0x00 to 0x33 (SharedDefines.h) must have a
        # name — anything falling back to "type_<n>" here is a gap.
        for value in list(range(0x34)) + [0xFF]:
            self.assertIn(value, hchat.CHAT_KIND_NAMES, f"unnamed ChatMsg {value:#04x}")

    def test_group_invite_sets_pending_invite(self):
        sess = make_session()
        payload = bytes([1]) + b'Rubens\x00'
        sess._dispatch(hchat.SMSG_GROUP_INVITE, payload)
        self.assertEqual(sess.pending_invite, {"inviter_name": "Rubens"})

    def test_chat_player_not_found_records_whisper_failed_event(self):
        sess = make_session()
        sess._dispatch(hchat.SMSG_CHAT_PLAYER_NOT_FOUND, b'Nobody\x00')
        self.assertEqual(sess.events[-1]["kind"], "whisper_failed")
        self.assertEqual(sess.events[-1]["target_name"], "Nobody")

    def test_party_command_result_records_group_invite_failed_on_failure(self):
        sess = make_session()
        # uint32 operation (0=invite), cstring member, uint32 result (5=already_in_group), uint32 val
        payload = struct.pack('<I', 0) + b'Rubens\x00' + struct.pack('<II', 5, 0)
        sess._dispatch(hchat.SMSG_PARTY_COMMAND_RESULT, payload)
        self.assertEqual(sess.events[-1]["kind"], "group_invite_failed")
        self.assertEqual(sess.events[-1]["target_name"], "Rubens")
        self.assertEqual(sess.events[-1]["result"], 5)
        self.assertEqual(sess.events[-1]["result_name"], "already_in_group")

    def test_party_command_result_ignored_on_success(self):
        sess = make_session()
        # result 0 == ERR_PARTY_RESULT_OK — no event should be recorded.
        payload = struct.pack('<I', 0) + b'Rubens\x00' + struct.pack('<II', 0, 0)
        sess._dispatch(hchat.SMSG_PARTY_COMMAND_RESULT, payload)
        self.assertEqual(len(sess.events), 0)

    def test_chat_player_not_found_dispatch(self):
        sess = make_session()
        self.assertTrue(sess._dispatch(hchat.SMSG_CHAT_PLAYER_NOT_FOUND, b'Nobody\x00'))
        self.assertEqual(sess.events[-1]["kind"], "whisper_failed")

    def test_party_command_result_dispatch(self):
        sess = make_session()
        payload = struct.pack('<I', 0) + b'Rubens\x00' + struct.pack('<II', 1, 0)
        self.assertTrue(sess._dispatch(hchat.SMSG_PARTY_COMMAND_RESULT, payload))
        self.assertEqual(sess.events[-1]["kind"], "group_invite_failed")
        self.assertEqual(sess.events[-1]["result_name"], "bad_player_name")


class CreateCharacterTest(unittest.TestCase):
    def test_sends_expected_payload_and_returns_on_success(self):
        sess = make_session(server_packet(se.SMSG_CHAR_CREATE, bytes([se.CHAR_CREATE_SUCCESS])))
        code = sess.create_character('Testelf', race=10, class_=2, gender=1)  # blood elf paladin, female
        expected_payload = b'Testelf\x00' + bytes([10, 2, 1, 0, 0, 0, 0, 0]) + b'\x00'
        expected = struct.pack('>H', len(expected_payload) + 4) + struct.pack('<I', se.CMSG_CHAR_CREATE) + expected_payload
        self.assertEqual(sess.sock.sent, expected)
        self.assertEqual(code, se.CHAR_CREATE_SUCCESS)

    def test_skips_packets_sent_before_the_answer(self):
        # Live 2026-10-03: SMSG_POWER_UPDATE (0x480) arrives before SMSG_CHAR_CREATE.
        power_update = server_packet(0x480, b'\x01\x07\x00' + struct.pack('<I', 100))
        sess = make_session(power_update * 3
                            + server_packet(se.SMSG_CHAR_CREATE, bytes([se.CHAR_CREATE_SUCCESS])))
        self.assertEqual(sess.create_character('Testelf', race=10, class_=2),
                         se.CHAR_CREATE_SUCCESS)

    def test_raises_when_the_answer_never_comes(self):
        junk = server_packet(0x480, b'\x00') * (se.CHAR_CREATE_MAX_SKIPPED_PACKETS + 1)
        sess = make_session(junk + server_packet(se.SMSG_CHAR_CREATE, bytes([se.CHAR_CREATE_SUCCESS])))
        with self.assertRaises(RuntimeError):
            sess.create_character('Testelf', race=10, class_=2)

    def test_raises_on_non_success_code(self):
        sess = make_session(server_packet(se.SMSG_CHAR_CREATE, bytes([se.CHAR_CREATE_NAME_IN_USE])))
        with self.assertRaises(RuntimeError):
            sess.create_character('Taken', race=1, class_=1)


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
        good = update_object(object_block(uo.UPDATETYPE_CREATE_OBJECT2, CREATURE))
        stream = (server_packet(hworld.SMSG_COMPRESSED_UPDATE_OBJECT, b'\x10\x00\x00\x00garbage')
                  + server_packet(hworld.SMSG_UPDATE_OBJECT, b'\x01\x00\x00\x00\x02')
                  + server_packet(hworld.SMSG_UPDATE_OBJECT, b'\x01\x00\x00\x00\x02')
                  + server_packet(hworld.SMSG_UPDATE_OBJECT, good))
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


class DeathAndCorpseHandlerTest(unittest.TestCase):
    """UM-43: death event detection, and the SMSG_CORPSE_RECLAIM_DELAY/
    SMSG_DEATH_RELEASE_LOC/MSG_CORPSE_QUERY handlers."""

    def test_death_event_on_health_zero_transition(self):
        sess = make_session()
        sess.player_guid = CREATURE
        sess.world_state.set_my_guid(CREATURE)
        alive = object_block(uo.UPDATETYPE_CREATE_OBJECT2, CREATURE, object_type=uo.TYPEID_PLAYER,
                              values=values_body({uf.UNIT_FIELD_HEALTH: 100}))
        hworld.parse_update_object(sess.ctx, update_object(alive))
        self.assertEqual(len(sess.events), 0)

        dead = values_block(CREATURE, values=values_body({uf.UNIT_FIELD_HEALTH: 0}))
        hworld.parse_update_object(sess.ctx, update_object(dead))
        self.assertEqual(sess.events[-1]["kind"], "death")
        self.assertIsNone(sess.events[-1]["killer_guid"])

    def test_death_event_only_fires_once_per_transition(self):
        sess = make_session()
        sess.player_guid = CREATURE
        sess.world_state.set_my_guid(CREATURE)
        hworld.parse_update_object(sess.ctx, update_object(
            object_block(uo.UPDATETYPE_CREATE_OBJECT2, CREATURE, object_type=uo.TYPEID_PLAYER,
                         values=values_body({uf.UNIT_FIELD_HEALTH: 0}))))
        hworld.parse_update_object(sess.ctx, update_object(
            values_block(CREATURE, values=values_body({uf.UNIT_FIELD_HEALTH: 0}))))
        deaths = [e for e in sess.events if e["kind"] == "death"]
        self.assertEqual(len(deaths), 1)

    def test_death_event_infers_killer_from_attacker_state_update(self):
        sess = make_session()
        sess.player_guid = CREATURE
        sess.world_state.set_my_guid(CREATURE)
        hworld.parse_update_object(sess.ctx, update_object(
            object_block(uo.UPDATETYPE_CREATE_OBJECT2, CREATURE, object_type=uo.TYPEID_PLAYER,
                         values=values_body({uf.UNIT_FIELD_HEALTH: 100}))))
        sess._record_event("attacker_state_update", attacker_guid=0xBEEF, victim_guid=CREATURE)
        hworld.parse_update_object(sess.ctx, update_object(
            values_block(CREATURE, values=values_body({uf.UNIT_FIELD_HEALTH: 0}))))
        self.assertEqual(sess.events[-1]["killer_guid"], 0xBEEF)

    def test_corpse_reclaim_delay_sets_ready_at(self):
        sess = make_session()
        before = time.monotonic()
        sess._dispatch(hdeath.SMSG_CORPSE_RECLAIM_DELAY, struct.pack('<I', 30000))
        self.assertGreater(sess.corpse_reclaim_ready_at, before + 29)
        self.assertLess(sess.corpse_reclaim_ready_at, before + 31)

    def test_death_release_loc_stores_graveyard_position(self):
        sess = make_session()
        payload = struct.pack('<i3f', 0, 1.0, 2.0, 3.0)
        sess._dispatch(hdeath.SMSG_DEATH_RELEASE_LOC, payload)
        self.assertEqual(sess.graveyard_position, (0, 1.0, 2.0, 3.0))

    def test_death_release_loc_ignores_clear_sentinel(self):
        sess = make_session()
        sess.graveyard_position = (0, 1.0, 2.0, 3.0)
        payload = struct.pack('<i3f', -1, 0.0, 0.0, 0.0)
        sess._dispatch(hdeath.SMSG_DEATH_RELEASE_LOC, payload)
        self.assertEqual(sess.graveyard_position, (0, 1.0, 2.0, 3.0))  # unchanged

    def test_corpse_query_response_valid(self):
        sess = make_session()
        payload = struct.pack('<Bi3fiI', 1, 0, 10.0, 20.0, 30.0, 0, 0)
        sess._dispatch(hdeath.MSG_CORPSE_QUERY, payload)
        self.assertEqual(sess.corpse_position, (0, 10.0, 20.0, 30.0))

    def test_corpse_query_response_invalid_clears_position(self):
        sess = make_session()
        sess.corpse_position = (0, 1.0, 2.0, 3.0)
        sess._dispatch(hdeath.MSG_CORPSE_QUERY, struct.pack('<B', 0))
        self.assertIsNone(sess.corpse_position)

    def test_dispatch_routes_new_opcodes(self):
        sess = make_session()
        self.assertTrue(sess._dispatch(hdeath.SMSG_CORPSE_RECLAIM_DELAY, struct.pack('<I', 1000)))
        self.assertIsNotNone(sess.corpse_reclaim_ready_at)
        self.assertTrue(sess._dispatch(hdeath.SMSG_DEATH_RELEASE_LOC, struct.pack('<i3f', 0, 1.0, 2.0, 3.0)))
        self.assertEqual(sess.graveyard_position, (0, 1.0, 2.0, 3.0))
        self.assertTrue(sess._dispatch(hdeath.MSG_CORPSE_QUERY, struct.pack('<Bi3fiI', 1, 0, 1.0, 2.0, 3.0, 0, 0)))
        self.assertEqual(sess.corpse_position, (0, 1.0, 2.0, 3.0))


class ReconnectFlagTest(unittest.TestCase):
    def test_logout_marks_requested_before_anything_else(self):
        sess = make_session()
        sess.logout()
        self.assertTrue(sess._logout_requested)

    def test_unrequested_logout_complete_sets_unexpected_disconnect(self):
        sess = make_session(server_packet(se.SMSG_LOGOUT_COMPLETE, b''))
        sess._running = True
        sess._recv_loop()
        self.assertTrue(sess.unexpected_disconnect)

    def test_requested_logout_complete_does_not_set_unexpected_disconnect(self):
        sess = make_session(server_packet(se.SMSG_LOGOUT_COMPLETE, b''))
        sess._logout_requested = True
        sess._running = True
        sess._recv_loop()
        self.assertFalse(sess.unexpected_disconnect)

    def test_recv_thread_death_sets_unexpected_disconnect(self):
        sess = make_session()  # empty stream -> immediate ConnectionError (EOF)
        sess._running = True
        sess._recv_loop()
        self.assertTrue(sess.unexpected_disconnect)
class LootDispatchTest(unittest.TestCase):
    """UM-42: SMSG_LOOT_RESPONSE / _RELEASE_RESPONSE / _REMOVED /
    _MONEY_NOTIFY / SMSG_ITEM_PUSH_RESULT / SMSG_INVENTORY_CHANGE_FAILURE /
    SMSG_ITEM_QUERY_SINGLE_RESPONSE dispatch wiring."""

    def test_loot_response_sets_session_loot_and_records_event(self):
        sess = make_session()
        payload = struct.pack('<Q', 5) + bytes([lo.LOOT_CORPSE]) + struct.pack('<IB', 10, 0)
        sess._dispatch(lo.SMSG_LOOT_RESPONSE, payload)
        self.assertEqual(sess.loot["guid"], 5)
        self.assertTrue(sess.loot["success"])
        self.assertEqual(sess.events[-1]["kind"], "loot_response")

    def test_loot_release_response_clears_matching_loot(self):
        sess = make_session()
        sess.loot = {"guid": 5, "success": True, "items": []}
        sess._dispatch(lo.SMSG_LOOT_RELEASE_RESPONSE, struct.pack('<Q', 5) + bytes([1]))
        self.assertIsNone(sess.loot)

    def test_loot_release_response_ignores_mismatched_guid(self):
        sess = make_session()
        sess.loot = {"guid": 5, "success": True, "items": []}
        sess._dispatch(lo.SMSG_LOOT_RELEASE_RESPONSE, struct.pack('<Q', 999) + bytes([1]))
        self.assertIsNotNone(sess.loot)

    def test_loot_removed_drops_item_from_session_loot(self):
        sess = make_session()
        sess.loot = {"guid": 5, "success": True, "items": [{"slot": 0, "entry": 1}, {"slot": 1, "entry": 2}]}
        sess._dispatch(lo.SMSG_LOOT_REMOVED, bytes([0]))
        self.assertEqual(sess.loot["items"], [{"slot": 1, "entry": 2}])

    def test_loot_money_notify_records_event(self):
        sess = make_session()
        sess.loot = {"guid": 5, "success": True, "items": [], "coins": 12}
        sess._dispatch(lo.SMSG_LOOT_MONEY_NOTIFY, struct.pack('<I', 12) + bytes([1]))
        self.assertEqual(sess.loot["coins"], 0)
        self.assertEqual(sess.events[-1]["kind"], "loot_money")
        self.assertEqual(sess.events[-1]["money"], 12)

    def test_item_push_result_records_item_received_event(self):
        sess = make_session()
        payload = (struct.pack('<Q', 1) + struct.pack('<III', 1, 0, 1) + bytes([0])
                   + struct.pack('<I', 23) + struct.pack('<I', 159) + struct.pack('<I', 0)
                   + struct.pack('<i', -1) + struct.pack('<II', 1, 1))
        sess._dispatch(lo.SMSG_ITEM_PUSH_RESULT, payload)
        self.assertEqual(sess.events[-1]["kind"], "item_received")
        self.assertEqual(sess.events[-1]["entry"], 159)

    def test_inventory_change_failure_records_event(self):
        sess = make_session()
        payload = (bytes([lo.EQUIP_ERR_INV_FULL]) + struct.pack('<Q', 0) + struct.pack('<Q', 0)
                   + bytes([0]) + struct.pack('<i', 0))
        sess._dispatch(lo.SMSG_INVENTORY_CHANGE_FAILURE, payload)
        self.assertEqual(sess.events[-1]["kind"], "inventory_change_failure")
        self.assertFalse(sess.events[-1]["ok"])

    def test_item_query_response_populates_world_state_cache(self):
        sess = make_session()
        payload = struct.pack('<I', 999999 | 0x80000000)  # "not found" (short payload, valid)
        sess._dispatch(lo.SMSG_ITEM_QUERY_SINGLE_RESPONSE, payload)
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
        hworld.sync_self_from_block(sess.ctx, block)
        # Zero-value fields aren't sent over the wire, so a missing coinage field
        # once the self object exists defaults to 0 rather than staying None.
        self.assertEqual(sess.coinage, 0)
        self.assertEqual(list(sess.events), [])  # defaulting to 0 isn't a "change"

        from agent import update_fields as uf
        block2 = uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=CREATURE,
                                 fields={uf.PLAYER_FIELD_COINAGE: 100})
        sess.world_state.update_object(block2)
        hworld.sync_self_from_block(sess.ctx, block2)
        self.assertEqual(sess.coinage, 100)
        self.assertEqual(sess.events[-1], {**sess.events[-1], "kind": "money_changed",
                                            "old": 0, "new": 100, "delta": 100})

        block3 = uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=CREATURE,
                                 fields={uf.PLAYER_FIELD_COINAGE: 150})
        sess.world_state.update_object(block3)
        hworld.sync_self_from_block(sess.ctx, block3)
        self.assertEqual(sess.coinage, 150)
        self.assertEqual(sess.events[-1], {**sess.events[-1], "kind": "money_changed",
                                            "old": 100, "new": 150, "delta": 50})


class CharEnumRealPayloadTest(unittest.TestCase):
    """gh-199: enum_characters() against a real 3-character SMSG_CHAR_ENUM.

    The per-character tail after the character flags is 224 bytes
    (customizeFlags uint32 + firstLogin uint8 + pet 3 x uint32 + 23 equipment
    slots of uint32 displayId / uint8 inventoryType / uint32 enchantVisual),
    not the 108 the parser used to skip, so every character after the first
    started 47 bytes early: on an account holding 2+ characters the agent
    could not find the character it was asked to play and logged into the
    first one instead.
    """

    FIXTURE = (pathlib.Path(__file__).parent / "fixtures" / "char_enum"
               / "three_characters.bin")

    def _chars(self):
        payload = self.FIXTURE.read_bytes()
        sess = make_session(server_packet(se.SMSG_CHAR_ENUM, payload))
        return sess.enum_characters()

    def test_every_character_after_the_first_parses(self):
        chars = self._chars()
        self.assertEqual([c["name"] for c in chars],
                         ["Luaprata", "Dawnrunner", "Jevrun"])
        self.assertEqual([c["guid"] for c in chars], [2, 7, 8])
        self.assertEqual([c["level"] for c in chars], [1, 1, 1])
        self.assertEqual([(c["race"], c["class_"]) for c in chars], [(10, 2)] * 3)

    def test_last_character_map_and_position(self):
        last = self._chars()[-1]
        # Sunstrider Isle start area, Eversong Woods (map 530).
        self.assertEqual(last["map"], 530)
        self.assertAlmostEqual(last["x"], 10349.6, places=1)
        self.assertAlmostEqual(last["y"], -6357.3, places=1)

    def test_first_character_still_parses(self):
        # The old parser got record 1 right; this guards the head half too.
        first = self._chars()[0]
        self.assertEqual(first["name"], "Luaprata")
        self.assertEqual(first["map"], 530)
        self.assertAlmostEqual(first["x"], 9918.8, places=1)


if __name__ == '__main__':
    unittest.main()
