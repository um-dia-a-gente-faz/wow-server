"""UM-93: chat channels — CMSG_JOIN_CHANNEL / CMSG_MESSAGECHAT(CHAT_MSG_CHANNEL)
golden bytes, SMSG_CHANNEL_NOTIFY parsing, joined-channel tracking and the
channel_say action. Layouts: agent/channels.py's docstring (TrinityCore 3.3.5)."""

import struct
import threading
import time
import unittest
from types import SimpleNamespace

from agent import actions as ac
from agent import channels as ch
from agent import perception as per
from agent import session as se

GENERAL = "General - Eversong Woods"
ME = 0x0000000000000042


def notify(kind: int, channel: str, tail: bytes = b"") -> bytes:
    return bytes([kind]) + channel.encode("utf-8") + b"\x00" + tail


def you_joined(channel: str, channel_id: int, flags: int = 0x18) -> bytes:
    return notify(ch.CHAT_YOU_JOINED_NOTICE, channel, struct.pack("<Bii", flags, channel_id, 0))


def channel_chat(sender_guid: int, channel: str, text: str) -> bytes:
    """SMSG_MESSAGECHAT as Chat::Write builds it for CHAT_MSG_CHANNEL."""
    t = text.encode("utf-8") + b"\x00"
    return (struct.pack("<BiQI", se.CHAT_MSG_CHANNEL, 1, sender_guid, 0)
            + channel.encode("utf-8") + b"\x00"
            + struct.pack("<Q", sender_guid)
            + struct.pack("<I", len(t)) + t + b"\x00")


class JoinPacketTest(unittest.TestCase):
    def test_opcodes(self):
        self.assertEqual((ch.CMSG_JOIN_CHANNEL, ch.CMSG_LEAVE_CHANNEL, ch.SMSG_CHANNEL_NOTIFY),
                         (0x097, 0x098, 0x099))

    def test_general_joins_by_system_id(self):
        self.assertEqual(ch.build_join_channel("General"),
                         b"\x01\x00\x00\x00" b"\x00" b"\x00" b"General\x00" b"\x00")

    def test_system_lookup_is_case_insensitive_and_accepts_full_name(self):
        self.assertEqual(ch.system_channel_id("general"), 1)
        self.assertEqual(ch.system_channel_id(GENERAL), 1)
        self.assertEqual(ch.system_channel_id("LocalDefense"), 22)
        self.assertEqual(ch.system_channel_id("world"), 0)

    def test_custom_channel_joins_by_name_with_id_zero(self):
        self.assertEqual(ch.build_join_channel("world", password="pw"),
                         b"\x00\x00\x00\x00\x00\x00world\x00pw\x00")

    def test_custom_name_rules(self):
        with self.assertRaises(ValueError):
            ch.build_join_channel("1world")
        with self.assertRaises(ValueError):
            ch.build_join_channel("x" * 32)
        with self.assertRaises(ValueError):
            ch.build_join_channel("")

    def test_leave(self):
        self.assertEqual(ch.build_leave_channel(GENERAL),
                         b"\x01\x00\x00\x00" + GENERAL.encode() + b"\x00")


class ChannelSpecTest(unittest.TestCase):
    def test_default_is_general(self):
        self.assertEqual(ch.parse_channel_spec(None), ["General"])
        self.assertEqual(ch.parse_channel_spec("  "), ["General"])

    def test_list_dedup_and_none(self):
        self.assertEqual(ch.parse_channel_spec("General, world ,general,"), ["General", "world"])
        self.assertEqual(ch.parse_channel_spec("none"), [])


class NotifyParseTest(unittest.TestCase):
    def test_you_joined(self):
        d = ch.parse_channel_notify(you_joined(GENERAL, 1, flags=0x18))
        self.assertEqual(d, {"notice": 2, "notice_name": "you_joined", "channel": GENERAL,
                             "flags": 0x18, "channel_id": 1, "instance_id": 0})

    def test_you_left(self):
        d = ch.parse_channel_notify(notify(ch.CHAT_YOU_LEFT_NOTICE, "world", struct.pack("<iB", 0, 1)))
        self.assertEqual((d["notice_name"], d["channel_id"], d["suspended"]), ("you_left", 0, True))

    def test_joined_has_guid(self):
        d = ch.parse_channel_notify(notify(ch.CHAT_JOINED_NOTICE, "world", struct.pack("<Q", ME)))
        self.assertEqual(d["sender_guid"], ME)

    def test_name_tail(self):
        d = ch.parse_channel_notify(notify(0x0B, "world", b"Rubens\x00"))
        self.assertEqual((d["notice_name"], d["sender_name"]), ("channel_owner", "Rubens"))

    def test_mode_change_and_kick(self):
        d = ch.parse_channel_notify(notify(0x0C, "world", struct.pack("<QBB", ME, 0, 2)))
        self.assertEqual((d["old_flags"], d["new_flags"]), (0, 2))
        d = ch.parse_channel_notify(notify(0x12, "world", struct.pack("<QQ", 5, ME)))
        self.assertEqual((d["target_guid"], d["sender_guid"]), (5, ME))

    def test_errors_have_no_tail(self):
        for kind, name in ((0x05, "not_member"), (0x11, "muted"), (0x1B, "invalid_name"),
                           (0x1F, "throttled"), (0x20, "not_in_area")):
            with self.subTest(name=name):
                d = ch.parse_channel_notify(notify(kind, GENERAL))
                self.assertEqual(d["notice_name"], name)
                self.assertIn(kind, ch.ERROR_NOTICES)

    def test_leftover_bytes_raise(self):
        with self.assertRaises(ValueError):
            ch.parse_channel_notify(notify(0x05, GENERAL, b"\x00"))


class WorldChannelsTest(unittest.TestCase):
    def test_join_leave_and_snapshot(self):
        w = per.WorldState()
        w.apply_channel_notify(ch.parse_channel_notify(you_joined(GENERAL, 1)))
        w.apply_channel_notify(ch.parse_channel_notify(you_joined("world", 0, flags=1)))
        self.assertEqual(w.snapshot()["channels"], [GENERAL, "world"])
        w.apply_channel_notify(ch.parse_channel_notify(
            notify(ch.CHAT_YOU_LEFT_NOTICE, "world", struct.pack("<iB", 0, 0))))
        self.assertEqual(sorted(w.get_channels()), [GENERAL])

    def test_zone_change_replaces_system_channel(self):
        # Player::UpdateLocalChannels re-joins General for the new zone
        # without sending you_left for the old one.
        w = per.WorldState()
        w.apply_channel_notify(ch.parse_channel_notify(you_joined(GENERAL, 1)))
        w.apply_channel_notify(ch.parse_channel_notify(you_joined("General - Silvermoon City", 1)))
        self.assertEqual(sorted(w.get_channels()), ["General - Silvermoon City"])

    def test_find_joined_prefix_like_server(self):
        joined = {GENERAL: {}, "world": {}}
        self.assertEqual(ch.find_joined(joined, "general"), GENERAL)
        self.assertEqual(ch.find_joined(joined, "World"), "world")
        self.assertIsNone(ch.find_joined(joined, "trade"))
        self.assertIsNone(ch.find_joined(joined, " "))


def make_session():
    sess = se.WoWSession("127.0.0.1", 8085, "TEST", b"\x00" * 40, 1)
    sess.player_guid = ME
    sess.race = 10
    sess.sent = []
    sess._send_packet = lambda opcode, payload=b"": sess.sent.append((opcode, payload))
    return sess


class SessionNotifyTest(unittest.TestCase):
    def test_dispatch_records_events_and_state(self):
        sess = make_session()
        self.assertTrue(sess._dispatch(ch.SMSG_CHANNEL_NOTIFY, you_joined(GENERAL, 1)))
        self.assertEqual(sess.events[-1]["kind"], "channel_joined")
        self.assertIn(GENERAL, sess.world_state.get_channels())
        sess._dispatch(ch.SMSG_CHANNEL_NOTIFY, notify(0x1F, GENERAL))
        self.assertEqual((sess.events[-1]["kind"], sess.events[-1]["reason"]), ("channel_error", "throttled"))

    def test_malformed_notify_is_parse_error(self):
        sess = make_session()
        with self.assertRaises(per.PerceptionParseError):
            sess._dispatch(ch.SMSG_CHANNEL_NOTIFY, notify(ch.CHAT_YOU_JOINED_NOTICE, GENERAL, b"\x00"))

    def test_incoming_channel_chat_lands_in_inbox(self):
        sess = make_session()
        sess._dispatch(se.SMSG_MESSAGECHAT, channel_chat(7, GENERAL, "hi all"))
        e = sess.chat_inbox[-1]
        self.assertEqual((e["kind"], e["channel"], e["text"], e["sender_guid"]), ("channel", GENERAL, "hi all", 7))

    def test_join_channels_waits_for_you_joined(self):
        sess = make_session()

        def reply(opcode, payload=b""):
            sess.sent.append((opcode, payload))
            name = payload[6:payload.index(0, 6)].decode()
            if name == "General":
                resp = you_joined(GENERAL, 1)
            elif name == "world":
                resp = you_joined("world", 0, flags=1)
            else:
                resp = notify(ch.CHAT_INVALID_NAME_NOTICE, name)
            threading.Timer(0.02, sess._dispatch, (ch.SMSG_CHANNEL_NOTIFY, resp)).start()

        sess._send_packet = reply
        res = sess.join_channels(["General", "world", "bad name"], timeout=1.0)
        self.assertEqual(res, {"General": GENERAL, "world": "world", "bad name": None})
        self.assertEqual([op for op, _ in sess.sent], [ch.CMSG_JOIN_CHANNEL] * 3)

    def test_join_channels_times_out(self):
        sess = make_session()
        self.assertEqual(sess.join_channels(["Trade"], timeout=0.05), {"Trade": None})


def fake_session():
    sent = []
    sess = SimpleNamespace(race=10, player_guid=ME, events=[], chat_inbox=[])
    sess._send_packet = lambda opcode, payload=b"": sent.append((opcode, payload))
    sess._sent = sent
    return sess


def joined_world():
    w = per.WorldState()
    w.apply_channel_notify(ch.parse_channel_notify(you_joined(GENERAL, 1)))
    return w


class ChannelSayTest(unittest.TestCase):
    def action(self, timeout=0.3):
        a = ac.ChannelSayAction()
        a.confirm_timeout = timeout
        a.confirm_interval = 0.01
        return a

    def test_registered(self):
        self.assertIn("channel_say", ac.REGISTRY)
        self.assertEqual(ac.REGISTRY["channel_say"].schema()["parameters"]["required"], ["channel", "message"])

    def test_packet_layout(self):
        self.assertEqual(ac.build_channel_message(ac.LANG_ORCISH, GENERAL, "Hello"),
                         struct.pack("<ii", 0x11, 1) + GENERAL.encode() + b"\x00Hello\x00")

    def test_check_requires_joined_channel(self):
        err = self.action().check(fake_session(), per.WorldState(), channel="General", message="hi")
        self.assertIn("not in channel", err)

    def test_execute_confirms_on_echo(self):
        sess, world = fake_session(), joined_world()

        def echo():
            time.sleep(0.03)
            sess.chat_inbox.append({"kind": "channel", "sender_guid": ME, "channel": GENERAL, "text": "Hello all"})
        threading.Thread(target=echo).start()
        res = self.action().run(sess, world, channel="general", message="Hello all")
        self.assertTrue(res.ok, res.error)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_MESSAGECHAT)
        self.assertEqual(payload, ac.build_channel_message(ac.LANG_ORCISH, GENERAL, "Hello all"))

    def test_execute_fails_on_channel_error(self):
        sess, world = fake_session(), joined_world()

        def err():
            time.sleep(0.03)
            sess.events.append({"kind": "channel_error", "t": time.monotonic(), "reason": "muted"})
        threading.Thread(target=err).start()
        res = self.action().run(sess, world, channel="General", message="hi")
        self.assertFalse(res.ok)
        self.assertIn("muted", res.error)

    def test_execute_without_echo_is_unconfirmed(self):
        res = self.action(timeout=0.05).run(fake_session(), joined_world(), channel="General", message="hi")
        self.assertFalse(res.ok)
        self.assertIn("unconfirmed", res.error)

    def test_rate_limit_and_no_repeats(self):
        sess, world = fake_session(), joined_world()
        a = self.action(timeout=0.01)
        a.run(sess, world, channel="General", message="Hello there!")
        self.assertIn("rate-limited", a.check(sess, world, channel="General", message="something else"))
        a.min_interval = 0
        self.assertIn("already said", a.check(sess, world, channel="General", message="hello   THERE"))
        self.assertIsNone(a.check(sess, world, channel="General", message="something else"))
        self.assertEqual(len(sess._sent), 1)


if __name__ == "__main__":
    unittest.main()
