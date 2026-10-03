"""UM-47: chat relay — event shape, cross-agent dedupe keys, late name
resolution, bounded queue, and the session hook that feeds it.

Synthetic payloads here are built the same way as in test_session.py
(WorldPackets::Chat::Chat::Write, ChatPackets.cpp); LiveCaptureTests at the
bottom replays bytes captured off the live realm instead.
"""

import json
import os
import pathlib
import struct
import tempfile
import unittest
import uuid
from types import SimpleNamespace

from agent import chat_relay as cr
from agent import session as se

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "chat"


def make_session() -> se.WoWSession:
    sess = se.WoWSession("127.0.0.1", 8085, "TEST", b"\x00" * 40, 1)
    sess.world_state.names.cache_path = os.path.join(
        tempfile.gettempdir(), f"wow-agent-test-names-{uuid.uuid4().hex}.json")
    return sess


def len_string(s: str) -> bytes:
    encoded = s.encode("utf-8") + b"\x00"
    return struct.pack("<I", len(encoded)) + encoded


def messagechat_payload(slash_cmd: int, text: str, sender_guid: int = 1,
                        channel: str | None = None) -> bytes:
    """Chat::Write's default branch on the plain (non-GM) opcode: no sender
    name at all, just the GUID — which is why the relay resolves names.

    target_guid mirrors sender_guid, as TrinityCore actually sends it
    (Chat::Initialize(..., this, this, ...)); the live fixtures show the
    same thing.
    """
    payload = struct.pack("<BiQI", slash_cmd, 1, sender_guid, 0)
    if channel is not None:
        payload += channel.encode("utf-8") + b"\x00"
    payload += struct.pack("<Q", sender_guid)
    return payload + len_string(text) + b"\x00"


def monster_chat_payload(slash_cmd: int, text: str, sender_name: str,
                         sender_guid: int = 1) -> bytes:
    """Chat::Write's CHAT_KINDS_MONSTER branch — an NPC's name is on the wire."""
    payload = struct.pack("<BiQI", slash_cmd, 1, sender_guid, 0)
    payload += len_string(sender_name) + struct.pack("<Q", 0)
    return payload + len_string(text) + b"\x00"


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"published": 1}'


class FakeOpener:
    """Stands in for urllib.request.urlopen."""

    def __init__(self, error=None):
        self.requests = []
        self.error = error

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse()

    def bodies(self):
        return [json.loads(request.data.decode("utf-8")) for request, _ in self.requests]

    def events(self):
        return [event for body in self.bodies() for event in body["events"]]


def make_relay(**kwargs):
    kwargs.setdefault("url", "http://feed:9500")
    kwargs.setdefault("agent_name", "Farstrider")
    opener = kwargs.pop("opener", None) or FakeOpener()
    relay = cr.ChatRelay(opener=opener, sleep=lambda _s: None, **kwargs)
    return relay, opener


def publish_all(relay):
    """Drain the queue synchronously — the worker thread is not started in
    tests, so ordering and counters stay deterministic."""
    while not relay._queue.empty():
        relay.publish(*relay._queue.get_nowait())


class EventShapeTests(unittest.TestCase):
    def test_say_event_has_every_documented_field(self):
        event = cr.build_event({"kind": "say", "sender_guid": 7, "sender_name": "",
                                "channel": None, "text": "hello there"},
                               sender_name="Rubens", source="Farstrider",
                               at="2026-09-25T10:00:00.000Z")
        self.assertEqual(event, {
            "at": "2026-09-25T10:00:00.000Z", "kind": "say", "sender": "Rubens",
            "sender_guid": 7, "channel": None, "target": None,
            "text": "hello there", "source": "Farstrider",
            "dedupe_key": event["dedupe_key"],
        })

    def test_channel_and_target_are_carried(self):
        event = cr.build_event({"kind": "channel", "sender_guid": 9, "sender_name": "Alice",
                                "channel": "General - Eversong Woods", "text": "LFG"},
                               target="Bob")
        self.assertEqual(event["channel"], "General - Eversong Woods")
        self.assertEqual(event["target"], "Bob")

    def test_text_is_carried_verbatim_but_length_capped(self):
        # Untrusted text: the relay never sanitizes, it only bounds the size.
        raw = "<script>alert(1)</script> " + "x" * cr.MAX_TEXT_LEN
        event = cr.build_event({"kind": "say", "sender_guid": 1, "text": raw})
        self.assertTrue(event["text"].startswith("<script>alert(1)</script>"))
        self.assertEqual(len(event["text"]), cr.MAX_TEXT_LEN)

    def test_system_message_without_sender(self):
        event = cr.build_event({"kind": "system", "sender_guid": 0, "sender_name": "",
                                "channel": None, "text": "Server restarting"})
        self.assertIsNone(event["sender"])
        self.assertIsNone(event["sender_guid"])


class DedupeKeyTests(unittest.TestCase):
    def test_two_listeners_of_the_same_message_agree(self):
        entry = {"kind": "say", "sender_guid": 7, "sender_name": "", "channel": None,
                 "text": "Olá pessoal"}
        # One agent has resolved the speaker's name, the other hasn't yet.
        heard_by_a = cr.build_event(entry, sender_name="Rubens", source="Farstrider")
        heard_by_b = cr.build_event(entry, source="Shadowblade")
        self.assertEqual(heard_by_a["dedupe_key"], heard_by_b["dedupe_key"])

    def test_different_text_differs(self):
        a = cr.build_event({"kind": "say", "sender_guid": 7, "text": "hi"})
        b = cr.build_event({"kind": "say", "sender_guid": 7, "text": "hi!"})
        self.assertNotEqual(a["dedupe_key"], b["dedupe_key"])

    def test_same_text_in_different_channels_differs(self):
        a = cr.build_event({"kind": "channel", "sender_guid": 7, "channel": "General", "text": "hi"})
        b = cr.build_event({"kind": "channel", "sender_guid": 7, "channel": "world", "text": "hi"})
        self.assertNotEqual(a["dedupe_key"], b["dedupe_key"])

    def test_guidless_senders_fall_back_to_the_name(self):
        a = cr.build_event({"kind": "monster_say", "sender_guid": 0,
                            "sender_name": "Magistrix Erona", "text": "Welcome"})
        b = cr.build_event({"kind": "monster_say", "sender_guid": 0,
                            "sender_name": "Shara Sunwing", "text": "Welcome"})
        self.assertNotEqual(a["dedupe_key"], b["dedupe_key"])


class PostingTests(unittest.TestCase):
    def test_ingest_path_is_appended_once(self):
        self.assertEqual(cr.ChatRelay("http://feed:9500").url, "http://feed:9500/api/chat/ingest")
        self.assertEqual(cr.ChatRelay("http://feed:9500/").url, "http://feed:9500/api/chat/ingest")
        self.assertEqual(cr.ChatRelay("http://feed:9500/api/chat/ingest").url,
                         "http://feed:9500/api/chat/ingest")

    def test_empty_url_disables_the_relay(self):
        relay, opener = make_relay(url="")
        self.assertFalse(relay.enabled)
        relay.submit({"kind": "say", "text": "hi", "sender_guid": 1})
        relay.start()  # no thread, no error
        self.assertEqual(opener.requests, [])

    def test_post_body_is_utf8_json_with_a_bearer_token(self):
        relay, opener = make_relay(token="s3cret")
        relay.submit({"kind": "say", "sender_guid": 7, "sender_name": "Rubens",
                      "text": "Olá, tudo bem?"})
        publish_all(relay)

        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "http://feed:9500/api/chat/ingest")
        self.assertEqual(request.get_header("Authorization"), "Bearer s3cret")
        self.assertEqual(timeout, cr.DEFAULT_TIMEOUT_S)
        self.assertEqual(opener.events()[0]["text"], "Olá, tudo bem?")
        self.assertEqual(relay.stats()["sent"], 1)

    def test_full_queue_drops_instead_of_blocking(self):
        relay, opener = make_relay(queue_size=2)
        for i in range(5):
            relay.submit({"kind": "say", "sender_guid": 1, "text": f"msg {i}"})
        self.assertEqual(relay.stats(), {"sent": 0, "dropped": 3, "failed": 0, "queued": 2})

        publish_all(relay)
        self.assertEqual([e["text"] for e in opener.events()], ["msg 0", "msg 1"])

    def test_post_failure_is_counted_not_raised(self):
        relay, _ = make_relay(opener=FakeOpener(error=OSError("connection refused")))
        relay.submit({"kind": "say", "sender_guid": 1, "text": "hi"})
        # _run()'s try/except is what the worker thread uses; drive it here.
        item = relay._queue.get_nowait()
        with self.assertRaises(OSError):
            relay.publish(*item)
        self.assertEqual(relay.stats()["sent"], 0)


class NameResolutionTests(unittest.TestCase):
    def test_waits_for_the_name_cache_then_sends_the_name(self):
        answers = [None, None, "Rubens"]
        relay, opener = make_relay()
        relay.submit({"kind": "say", "sender_guid": 7, "sender_name": "", "text": "hi"},
                     resolve_name=lambda guid: answers.pop(0))
        publish_all(relay)
        self.assertEqual(opener.events()[0]["sender"], "Rubens")
        self.assertEqual(answers, [])

    def test_gives_up_after_name_wait_and_still_publishes(self):
        ticks = iter([0.0, 0.0, 1.0, 2.5])  # queued_at, then three checks
        relay, opener = make_relay(name_wait_s=2.0, clock=lambda: next(ticks))
        relay.submit({"kind": "say", "sender_guid": 7, "sender_name": "", "text": "hi"},
                     resolve_name=lambda guid: None)
        publish_all(relay)
        event = opener.events()[0]
        self.assertIsNone(event["sender"])
        self.assertEqual(event["sender_guid"], 7)

    def test_whisper_inform_is_rewritten_as_this_agent_to_the_addressee(self):
        # The echo of a whisper the agent sent. On the wire the "sender" is
        # the person it went to (Player::Whisper passes the target twice).
        relay, opener = make_relay()
        relay.submit({"kind": "whisper_inform", "sender_guid": 9, "sender_name": "",
                      "text": "on my way"},
                     resolve_name=lambda guid: {9: "Rubens"}[guid])
        publish_all(relay)
        event = opener.events()[0]
        self.assertEqual((event["sender"], event["target"]), ("Farstrider", "Rubens"))


class SessionHookTests(unittest.TestCase):
    """The session hands every parsed entry to the relay — same bytes the
    parser tests use, so a layout change breaks both."""

    def make_wired_session(self):
        sess = make_session()
        relay, opener = make_relay()
        sess.chat_relay = relay
        sess.world_state.names.players[7] = "Rubens"
        return sess, relay, opener

    def test_say_reaches_the_relay_with_a_resolved_speaker(self):
        sess, relay, opener = self.make_wired_session()
        sess._dispatch(se.SMSG_MESSAGECHAT,
                       messagechat_payload(se.CHAT_MSG_SAY, "hello there", sender_guid=7))
        publish_all(relay)

        event = opener.events()[0]
        self.assertEqual((event["kind"], event["sender"], event["text"]),
                         ("say", "Rubens", "hello there"))
        self.assertEqual(event["source"], "Farstrider")

    def test_non_ascii_say_survives_the_whole_path(self):
        sess, relay, opener = self.make_wired_session()
        sess._dispatch(se.SMSG_MESSAGECHAT,
                       messagechat_payload(se.CHAT_MSG_SAY, "Olá! Precisa de ajuda, irmão?",
                                            sender_guid=7))
        publish_all(relay)

        request, _ = opener.requests[0]
        self.assertEqual(json.loads(request.data.decode("utf-8"))["events"][0]["text"],
                         "Olá! Precisa de ajuda, irmão?")

    def test_npc_yell_carries_the_name_from_the_packet(self):
        sess, relay, opener = self.make_wired_session()
        sess._dispatch(se.SMSG_MESSAGECHAT,
                       monster_chat_payload(se.CHAT_MSG_MONSTER_YELL, "Intruders!",
                                             "Mana Wyrm", sender_guid=0xF13000000000002A))
        publish_all(relay)
        event = opener.events()[0]
        self.assertEqual((event["kind"], event["sender"]), ("monster_yell", "Mana Wyrm"))

    def test_a_broken_relay_never_breaks_chat(self):
        sess = make_session()
        sess.chat_relay = SimpleNamespace(submit=lambda *a, **k: 1 / 0)
        sess._dispatch(se.SMSG_MESSAGECHAT,
                       messagechat_payload(se.CHAT_MSG_SAY, "still heard", sender_guid=7))
        self.assertEqual(sess.chat_inbox[-1]["text"], "still heard")


class LiveCaptureTests(unittest.TestCase):
    """Bytes captured off the live realm (192.168.1.64) on 2026-09-25 by
    Farstrider (guid 3) and Shadowblade (guid 4) — see
    fixtures/chat/README.md for exactly how they were taken."""

    FARSTRIDER = 3
    SHADOWBLADE = 4

    def load(self, name):
        raw = (FIXTURES / name).read_text().split("#", 1)[0].strip()
        return bytes.fromhex(raw)

    def relay_event(self, name, names=None):
        sess = make_session()
        relay, opener = make_relay()
        sess.chat_relay = relay
        sess.world_state.names.players.update(names or {})
        sess._dispatch(se.SMSG_MESSAGECHAT, self.load(name))
        publish_all(relay)
        return sess.chat_inbox[-1], (opener.events() or [None])[0]

    def as_farstrider(self, name):
        return self.relay_event(name, names={self.FARSTRIDER: "Farstrider",
                                             self.SHADOWBLADE: "Shadowblade"})

    def test_live_say(self):
        entry, event = self.as_farstrider("say.hex")
        self.assertEqual(entry["kind"], "say")
        self.assertEqual(entry["sender_guid"], self.FARSTRIDER)
        self.assertEqual(entry["text"], "UM-47 relay check")
        self.assertEqual((event["kind"], event["sender"], event["text"]),
                         ("say", "Farstrider", "UM-47 relay check"))

    def test_live_say_non_ascii(self):
        entry, event = self.as_farstrider("say_non_ascii.hex")
        self.assertEqual(entry["text"], "Olá! Já estou a caminho — vamos?")
        self.assertEqual(event["text"], "Olá! Já estou a caminho — vamos?")

    def test_live_yell(self):
        entry, event = self.as_farstrider("yell.hex")
        self.assertEqual((entry["kind"], entry["text"]), ("yell", "UM-47 yell check"))
        self.assertEqual((event["kind"], event["sender"]), ("yell", "Farstrider"))

    def test_live_emote(self):
        entry, event = self.as_farstrider("emote.hex")
        self.assertEqual((entry["kind"], entry["text"]), ("emote", "waves at the horizon"))
        self.assertEqual(event["sender"], "Farstrider")

    def test_live_channel(self):
        entry, event = self.as_farstrider("channel.hex")
        self.assertEqual(entry["kind"], "channel")
        self.assertEqual(entry["channel"], "General - Silvermoon City")
        self.assertEqual((event["channel"], event["text"]),
                         ("General - Silvermoon City", "UM-47 channel check"))

    def test_live_whisper_as_the_recipient_sees_it(self):
        # Shadowblade's copy: the whisperer is in sender_guid.
        entry, event = self.as_farstrider("whisper.hex")
        self.assertEqual((entry["kind"], entry["sender_guid"]), ("whisper", self.FARSTRIDER))
        self.assertEqual((event["sender"], event["target"]), ("Farstrider", None))

    def test_live_whisper_inform_names_the_addressee_not_the_speaker(self):
        # Farstrider's echo of the same whisper: sender_guid is Shadowblade,
        # the person it was sent to. The relay flips it back around.
        entry, event = self.as_farstrider("whisper_inform.hex")
        self.assertEqual((entry["kind"], entry["sender_guid"]),
                         ("whisper_inform", self.SHADOWBLADE))
        self.assertEqual((event["sender"], event["target"]),
                         ("Farstrider", "Shadowblade"))

    def test_live_target_guid_is_never_the_listener(self):
        """The reason the parser drops target_guid: TrinityCore fills it
        with Chat::Initialize's `receiver`, which is the speaker itself."""
        for name in ("say.hex", "yell.hex", "emote.hex", "channel.hex",
                     "whisper.hex", "whisper_inform.hex"):
            with self.subTest(name):
                payload = self.load(name)
                entry, _ = self.as_farstrider(name)
                self.assertNotIn("target_guid", entry)
                # Locate it on the wire and prove it duplicates sender_guid.
                sender_guid = struct.unpack_from("<Q", payload, 5)[0]
                off = 17 + (payload.index(b"\x00", 17) + 1 - 17
                            if payload[0] == se.CHAT_MSG_CHANNEL else 0)
                self.assertEqual(struct.unpack_from("<Q", payload, off)[0], sender_guid)


if __name__ == "__main__":
    unittest.main()
