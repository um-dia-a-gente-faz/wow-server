"""End-to-end: the agent's real auth + world client code against tools/world-mock.

Covers the #253 acceptance: login, update objects, a quest turn-in action and
the server's confirmation, all over real localhost sockets."""

import collections
import inspect
import logging
import os
import struct
import zlib
import pathlib
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock as umock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

from agent import actions as ac  # noqa: E402
from agent import opcodes as op  # noqa: E402
from agent import packets as pk  # noqa: E402
from agent import quests as qu  # noqa: E402
from agent import update_fields as uf  # noqa: E402
from agent.update_object import parse_update_object  # noqa: E402
from agent.login import authenticate_and_login  # noqa: E402
from agent.auth import AuthRejected, auth_logon  # noqa: E402


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class WorldMockTest(unittest.TestCase):
    def setUp(self):
        self.mock = server.WorldMock()
        self.addCleanup(self.mock.close)
        self.cfg = SimpleNamespace(wow_host="127.0.0.1", wow_auth_port=self.mock.auth_port,
                                   account=server.DEFAULT_ACCOUNT, password=server.DEFAULT_PASSWORD,
                                   verbose_packets=False, dump_packets_dir="")

    def test_wrong_password_is_rejected(self):
        with self.assertRaises(AuthRejected) as cm:
            auth_logon("127.0.0.1", self.mock.auth_port, server.DEFAULT_ACCOUNT, "hunter2x")
        self.assertEqual(cm.exception.code, 4)
        self.assertNotIn("hunter2x", str(cm.exception))

    def test_unknown_account_is_rejected_with_the_challenge_result(self):
        with self.assertRaises(AuthRejected) as cm:
            auth_logon("127.0.0.1", self.mock.auth_port, "NOSUCH", "x")
        self.assertEqual(cm.exception.code, 4)

    def test_login_then_quest_turn_in(self):
        sess, chars = authenticate_and_login(self.cfg, logging.getLogger("test"))
        self.addCleanup(lambda: sess.sock and sess.sock.close())
        self.assertEqual([c["name"] for c in chars], ["Luaprata", "Dawnrunner", "Jevrun"])
        sess.login_character(chars[0]["guid"])
        world = sess.world_state
        try:
            # update objects: own player, the questgiver, and the quest in the log
            self.assertTrue(wait_for(lambda: world.get_object(server.QUESTGIVER_GUID) is not None
                                     and world.build_quest_log()), "login burst not applied")
            self.assertEqual(sess.player_position[0], server.MAP_ID)
            self.assertEqual(world.build_quest_log()[0]["quest_id"], server.QUEST_ID)
            # the tick asked for the quest text and the mock answered with the captured fixture
            self.assertTrue(wait_for(lambda: self.mock.packets_received(op.CMSG_QUEST_QUERY)))

            npc = server.QUESTGIVER_GUID
            res = ac.REGISTRY["complete_quest"].run(sess, world, npc_guid=npc, quest_id=server.QUEST_ID)
            self.assertTrue(res.ok, res.error)
            self.assertTrue(wait_for(lambda: (world.get_ui_state() or {}).get("kind") == "quest_offer_reward"))
            self.assertEqual(len(world.get_ui_state()["reward_choice_items"]), 2)

            res = ac.REGISTRY["turn_in_quest"].run(sess, world, npc_guid=npc, quest_id=server.QUEST_ID,
                                                   reward_choice=1)
            self.assertTrue(res.ok, res.error)
            # confirmation comes from the server, not from the send
            self.assertTrue(wait_for(lambda: any(e["kind"] == "quest_turned_in" for e in sess.events)))
            self.assertTrue(wait_for(lambda: not world.build_quest_log()))
            self.assertEqual(self.mock.packets_received(op.CMSG_QUESTGIVER_CHOOSE_REWARD),
                             [qu.build_questgiver_choose_reward(npc, server.QUEST_ID, 1)])
            self.assertEqual(sess.dropped_packets, 0)
        finally:
            sess.logout()


class StreamTest(unittest.TestCase):
    def test_stream_sends_update_object_movement_and_chat_after_login(self):
        mock = server.WorldMock(stream_hz=100)
        self.addCleanup(mock.close)
        cfg = SimpleNamespace(wow_host="127.0.0.1", wow_auth_port=mock.auth_port, account=server.DEFAULT_ACCOUNT,
                              password=server.DEFAULT_PASSWORD, verbose_packets=False, dump_packets_dir="")
        sess, chars = authenticate_and_login(cfg, logging.getLogger("test"))
        self.addCleanup(lambda: sess.sock and sess.sock.close())
        seen = collections.Counter()
        real = sess._dispatch

        def counting(opcode, payload):
            seen[opcode] += 1
            return real(opcode, payload)
        sess._dispatch = counting
        sess.login_character(chars[0]["guid"])
        try:
            world = sess.world_state
            self.assertTrue(wait_for(lambda: len(sess.chat_inbox) >= 10), "chat not streamed")
            self.assertTrue(wait_for(lambda: seen[op.SMSG_MONSTER_MOVE] >= 10), "movement not streamed")
            self.assertTrue(wait_for(lambda: seen[op.SMSG_COMPRESSED_UPDATE_OBJECT] >= 10))
            # CREATE blocks cycle through a bounded pool; every one of them was applied
            self.assertTrue(wait_for(lambda: world.get_object(server.STREAM_UNIT_BASE + 5) is not None))
            self.assertTrue(wait_for(lambda: sess.chat_inbox[-1]["text"].startswith("stream ")))
            self.assertEqual(sess.dropped_packets, 0)   # monster move + chat parsed cleanly
        finally:
            sess.logout()

    def test_stream_payloads_are_byte_exact_trinitycore_layouts(self):
        """Golden bytes for tick n=3, written from the TrinityCore 3.3.5 layouts (cited in
        server.stream_packets), not from the agent's parsers: exact length, exact bytes."""
        (uop, update), (mop, move), (cop, chat) = server.stream_packets(3)
        # MonsterMove::Write: packed guid 0xF130000000010003 (mask 0xC5 + 4 bytes), u8 0, xyz, u32 id, u8 Face=1
        x, y, z, _o = server.SELF_POS
        self.assertEqual((mop, move), (op.SMSG_MONSTER_MOVE,
                         bytes([0xC5, 0x03, 0x01, 0x30, 0xF1, 0]) + struct.pack('<3fIB', x, y, z, 3, 1)))
        self.assertEqual(len(move), 23)
        # Chat::Write CHAT_MSG_SAY: type, lang, sender, 0, target, len, "stream 3\0", tag
        self.assertEqual((cop, chat), (op.SMSG_MESSAGECHAT,
                         b'\x01' + struct.pack('<iQIQI', 0, 0x1003, 0, 0, 9) + b'stream 3\0' + b'\0'))
        self.assertEqual(len(chat), 39)
        # a unit create is > 100 bytes, so UpdateData::BuildPacket sends it compressed
        self.assertEqual(uop, op.SMSG_COMPRESSED_UPDATE_OBJECT)
        size = struct.unpack_from('<I', update)[0]
        raw = zlib.decompress(update[4:])
        self.assertEqual((len(raw), size), (124, 124))
        movement = struct.pack('<HIHI4fI', 0x60, 0, 0, 1000, x + 3, y, z, server.SELF_POS[3], 0) \
            + struct.pack('<9f', 2.5, 7.0, 4.5, 4.72, 2.5, 7.0, 4.5, 3.14, 3.14)
        head = struct.pack('<IB', 1, 3) + bytes([0xC5, 0x03, 0x01, 0x30, 0xF1]) + b'\x03'   # count, CREATE_OBJECT2, guid, TYPEID_UNIT
        self.assertEqual(raw[:len(head) + 68], head + movement)
        # values: 1+3*4 mask bytes, then 8 uint32 (fields 2,3,4,level,health,maxhealth,faction,display)
        self.assertEqual(len(raw), len(head) + 68 + 1 + 3 * 4 + 8 * 4)

    def test_stream_pool_bounds_or_grows_the_unit_guids(self):
        """#486: the default pool cycles 50 unit guids; pool=0 gives every tick its own."""
        def unit_guids(pool, ticks=200):
            return {pk.unpack_packed_guid(server.stream_packets(n, pool=pool)[1][1], 0)[0] for n in range(ticks)}
        self.assertEqual(len(unit_guids(server.STREAM_POOL)), 50)
        self.assertEqual(len(unit_guids(0)), 200)
        self.assertEqual(pk.unpack_packed_guid(server.stream_packets(7, pool=0)[1][1], 0)[0],
                         server.STREAM_UNIT_BASE + 7)

    def test_stream_pool_takes_n_for_entry_and_chat_sender_too(self):
        """#486: pool=0 uses n itself for the unit guid, OBJECT_FIELD_ENTRY and the chat sender;
        the default pool cycles all three on 50. Read back from the packets, not from the code."""
        def identity(pool, n):
            (_, update), (_, move), (_, chat) = server.stream_packets(n, pool=pool)
            block = parse_update_object(zlib.decompress(update[4:]))[0]
            return (pk.unpack_packed_guid(move, 0)[0], block.guid, block.fields[uf.OBJECT_FIELD_ENTRY],
                    struct.unpack_from('<BiQ', chat, 0)[2])
        for n in (57, 120):
            with self.subTest(pool=0, n=n):
                self.assertEqual(identity(0, n), (server.STREAM_UNIT_BASE + n, server.STREAM_UNIT_BASE + n,
                                                  server.STREAM_UNIT_ENTRY + n, server.STREAM_PLAYER_BASE + n))
            with self.subTest(pool=server.STREAM_POOL, n=n):
                k = n % server.STREAM_POOL
                self.assertEqual(identity(server.STREAM_POOL, n), (server.STREAM_UNIT_BASE + k, server.STREAM_UNIT_BASE + k,
                                                                   server.STREAM_UNIT_ENTRY + k, server.STREAM_PLAYER_BASE + k))

    def test_default_pool_bytes_are_the_same_as_without_the_argument(self):
        """#486: the default path must not change guid, entry or chat sender (packets stay byte-equal)."""
        for n in range(100):
            self.assertEqual(server.stream_packets(n), server.stream_packets(n, pool=server.STREAM_POOL))

    def test_mock_passes_its_stream_pool_to_every_tick(self):
        mock = server.WorldMock(stream_pool=0)
        self.addCleanup(mock.close)
        self.assertEqual(mock.stream_pool, 0)
        sent = []
        mock._stream(lambda opcode, payload: sent.append((opcode, payload)), 57)   # 57 >= 50: pools differ
        self.assertEqual(sent, server.stream_packets(57, pool=0))
        self.assertNotEqual(sent, server.stream_packets(57))   # a stream that ignores the pool fails here

    def test_main_reads_the_stream_pool_from_the_environment(self):
        for env, want in (({"MOCK_STREAM_POOL": "0"}, 0), ({}, server.STREAM_POOL)):
            with self.subTest(env=env), umock.patch.dict(os.environ):
                os.environ.pop("MOCK_STREAM_POOL", None)
                os.environ.update(env)
                with umock.patch.object(server, "WorldMock") as world, \
                        umock.patch.object(server.threading, "Event"), \
                        umock.patch.object(server.logging, "basicConfig"):
                    server.main()
                self.assertEqual(world.call_args.kwargs["stream_pool"], want)

    def test_no_stream_by_default(self):
        params = inspect.signature(server.WorldMock).parameters
        self.assertEqual(params["stream_hz"].default, 0.0)
        self.assertEqual(params["stream_pool"].default, server.STREAM_POOL)


if __name__ == "__main__":
    unittest.main()
