"""#751: SMSG_CLIENTCACHE_VERSION (0x4AB), one uint32 LE, TrinityCore 3.3.5
ClientConfigPackets.cpp ClientCacheVersion::Write. Sent by WorldSession::InitializeSessionCallback
after SMSG_AUTH_RESPONSE and SMSG_ADDON_INFO, before SMSG_TUTORIAL_FLAGS."""

import json
import os
import struct
import tempfile
import unittest

from agent import opcodes
from agent import session as se
from agent.tests.builders import make_session, server_packet
from agent.wire import PacketTruncated


def put_cache(path, cache_id):
    with open(path, "w") as f:
        json.dump({"schema": 2, "cache_id": cache_id, "creatures": {"1": {"name": "Old"}}, "gameobjects": {}}, f)


class ParserTest(unittest.TestCase):
    def test_opcode_value(self):
        self.assertEqual(opcodes.SMSG_CLIENTCACHE_VERSION, 0x4AB)

    def test_four_bytes_little_endian(self):
        self.assertEqual(se.parse_clientcache_version(b"\x78\x56\x34\x12"), 0x12345678)

    def test_short_payloads_are_none(self):
        for payload in (b"", b"\x01", b"\x01\x02\x03"):
            self.assertIsNone(se.parse_clientcache_version(payload))


class HandlerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sess = make_session()
        self.sess.world_state.names.cache_path = os.path.join(tmp.name, "names.json")

    def test_defaults_to_none(self):
        self.assertIsNone(self.sess.client_cache_version)

    def test_handler_stores_the_value_and_tells_the_name_cache(self):
        self.assertTrue(self.sess._dispatch(opcodes.SMSG_CLIENTCACHE_VERSION, struct.pack("<I", 44)))
        self.assertEqual(self.sess.client_cache_version, 44)
        self.assertEqual(self.sess.world_state.names.server_cache_id, 44)

    def test_short_payload_is_counted_as_truncated_and_stores_nothing(self):
        counted = []
        self.sess.router._count = lambda kind, opcode: counted.append(kind)
        with self.assertRaises(PacketTruncated):
            self.sess._dispatch(opcodes.SMSG_CLIENTCACHE_VERSION, b"\x01\x02")
        self.assertIn("truncated", counted)
        self.assertIsNone(self.sess.client_cache_version)
        self.assertIsNone(self.sess.world_state.names.server_cache_id)


class ArrivalOrderTest(unittest.TestCase):
    """The packet arrives in the pre-login drain, before login_character loads the cache; a late
    one (after the load) is handled the same."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "names.json")
        put_cache(self.path, 5)

    def session(self, stream=b""):
        sess = make_session(stream)
        sess.world_state.names.cache_path = self.path
        return sess

    def test_drain_stops_at_tutorial_flags_and_handles_the_cache_version_before_it(self):
        stream = (server_packet(opcodes.SMSG_ADDON_INFO, b"")
                  + server_packet(opcodes.SMSG_CLIENTCACHE_VERSION, struct.pack("<I", 6))
                  + server_packet(opcodes.SMSG_TUTORIAL_FLAGS, b"\x00" * 32))
        sess = self.session(stream)
        sess._drain_pre_login()
        self.assertEqual(sess.client_cache_version, 6)
        sess.world_state.names.load()
        self.assertEqual(sess.world_state.names.creatures, {})   # known before the load: not used

    def test_packet_after_the_load_discards_what_the_load_read(self):
        sess = self.session()
        sess.world_state.names.load()
        self.assertEqual(sess.world_state.names.creatures, {1: {"name": "Old"}})
        sess._dispatch(opcodes.SMSG_CLIENTCACHE_VERSION, struct.pack("<I", 6))
        self.assertEqual(sess.world_state.names.creatures, {})


if __name__ == "__main__":
    unittest.main()
