"""gh-666: the server writes names and text as UTF-8 (ByteBuffer::operator<<(string_view) appends
the bytes and a NUL, no transcoding), so every NUL-terminated string must decode as UTF-8."""

import struct
import unittest
from types import SimpleNamespace

from agent import loot as lo
from agent import names as nm
from agent import packets as pk
from agent.handlers import chat as hchat
from agent.tests import test_chat_relay, test_loot, test_names
from agent.wire import Reader

THREE_BYTE = "Ship €"  # U+20AC is 3 bytes in UTF-8


class CstringDecodeTest(unittest.TestCase):
    def test_reader_decodes_utf8_and_consumes_bytes_not_characters(self):
        r = Reader(test_names.cstring("João") + test_names.cstring(THREE_BYTE) + bytes([7]), 0)
        self.assertEqual(r.cstring(), "João")
        self.assertEqual(r.cstring(), THREE_BYTE)
        self.assertEqual(r.u8(), 7)
        r.done()

    def test_reader_invalid_utf8_is_replaced_not_raised(self):
        r = Reader(b"Jo\xe3o\x00\x05", 0)
        self.assertEqual(r.cstring(), "Jo�o")
        self.assertEqual(r.u8(), 5)

    def test_reader_ascii_unchanged(self):
        self.assertEqual(Reader(b"Rubens\x00", 0).cstring(), "Rubens")

    def test_reader_explicit_encoding_still_wins(self):
        self.assertEqual(Reader("João".encode("latin-1") + b"\x00", 0).cstring("latin-1"), "João")

    def test_packets_cstring(self):
        data = b"\xff" + test_names.cstring("Ça va") + b"\x09"
        text, off = pk.cstring(data, 1)
        self.assertEqual((text, data[off]), ("Ça va", 9))
        self.assertEqual(pk.cstring(b"Jo\xe3o\x00", 0), ("Jo�o", 5))


class ParserTest(unittest.TestCase):
    def test_player_name_query(self):
        payload = (pk.pack_packed_guid(7) + bytes([0]) + test_names.cstring("João") + test_names.cstring("Ça va")
                   + bytes([2, 0, 1]) + bytes([0]))
        info = nm.parse_name_query_response(payload)
        self.assertEqual((info["name"], info["realm"]), ("João", "Ça va"))
        self.assertEqual((info["race"], info["sex"], info["class_"]), (2, 0, 1))

    def test_creature_query(self):
        payload = struct.pack('<I', 5) + test_names.creature_response_body(
            name="Guarda da Ça", subname="Líder €", creature_type=8, classification=3)
        info = nm.parse_creature_query_response(payload)
        self.assertEqual((info["name"], info["subname"]), ("Guarda da Ça", "Líder €"))
        self.assertEqual(info["rank"], "worldboss")

    def test_gameobject_query(self):
        payload = struct.pack('<I', 5) + test_names.gameobject_response_body(name=THREE_BYTE, icon="Íc", size=2.0)
        info = nm.parse_gameobject_query_response(payload)
        self.assertEqual((info["name"], info["icon_name"]), (THREE_BYTE, "Íc"))
        self.assertEqual(info["size"], 2.0)

    def test_item_query(self):
        payload = test_loot.ParseItemQueryResponseTest()._build(name="Espada de João", stackable=20)
        info = lo.parse_item_query_response(payload)
        self.assertEqual(info["name"], "Espada de João")
        self.assertEqual(info["stackable"], 20)

    def test_chat_channel_name_and_text(self):
        ctx = SimpleNamespace(state=SimpleNamespace(chat_inbox=[], chat_relay=None, world_state=None))
        payload = test_chat_relay.messagechat_payload(hchat.CHAT_MSG_CHANNEL, "Ça va, João", channel="Comércio")
        hchat.handle_messagechat(ctx, hchat.SMSG_MESSAGECHAT, payload)
        entry = ctx.state.chat_inbox[-1]
        self.assertEqual((entry["channel"], entry["text"]), ("Comércio", "Ça va, João"))


if __name__ == "__main__":
    unittest.main()
