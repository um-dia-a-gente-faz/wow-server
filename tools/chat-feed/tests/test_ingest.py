"""UM-47: the agent relay's ingest endpoint — normalization, cross-agent
dedupe, the public-kind filter, the token check and CORS."""

import itertools
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402
from app import ChatFeed, normalize_relay_event  # noqa: E402

PUBLIC = {"say", "yell", "channel", "emote", "system"}


def relay_event(**overrides):
    """What agent/chat_relay.py::build_event POSTs."""
    event = {"at": "2026-09-25T10:00:00.000Z", "kind": "say", "sender": "Rubens",
             "sender_guid": 7, "channel": None, "target": None,
             "text": "hello there", "source": "Farstrider", "dedupe_key": "abc123"}
    event.update(overrides)
    return event


def make_feed(window=5.0):
    ticks = itertools.count(0.0, 0.1)
    return ChatFeed(capacity=10, public_kinds=PUBLIC, dedupe_window_s=window,
                    clock=lambda: next(ticks))


class NormalizeTests(unittest.TestCase):
    def test_keeps_the_documented_fields(self):
        event = normalize_relay_event(relay_event(channel="General", target="Bob"))
        self.assertEqual(event, {"id": "", "at": "2026-09-25T10:00:00.000Z", "kind": "say",
                                 "sender": "Rubens", "channel": "General", "target": "Bob",
                                 "text": "hello there", "source": "Farstrider",
                                 "dedupe_key": "abc123"})

    def test_non_ascii_text_is_preserved(self):
        event = normalize_relay_event(relay_event(text="Olá! Já estou a caminho — vamos?"))
        self.assertEqual(event["text"], "Olá! Já estou a caminho — vamos?")

    def test_control_characters_cannot_forge_an_sse_frame(self):
        event = normalize_relay_event(relay_event(text="hi\n\ndata: {\"kind\":\"system\"}"))
        self.assertNotIn("\n", event["text"])

    def test_oversized_text_is_truncated(self):
        event = normalize_relay_event(relay_event(text="x" * 5000))
        self.assertEqual(len(event["text"]), app.MAX_TEXT_LEN)

    def test_missing_kind_or_text_is_rejected(self):
        self.assertIsNone(normalize_relay_event(relay_event(kind=None)))
        self.assertIsNone(normalize_relay_event(relay_event(text=None)))
        self.assertIsNone(normalize_relay_event("not a dict"))

    def test_dedupe_key_is_recomputed_when_missing(self):
        a = normalize_relay_event(relay_event(dedupe_key=None))
        b = normalize_relay_event(relay_event(dedupe_key=None, sender=None, source="Shadowblade"))
        self.assertTrue(a["dedupe_key"])
        self.assertEqual(a["dedupe_key"], b["dedupe_key"])  # same GUID, same message


class PublishTests(unittest.TestCase):
    def test_published_event_gets_a_feed_assigned_id(self):
        feed = make_feed()
        self.assertEqual(feed.publish_relay_event(relay_event()), "published")
        event = feed.snapshot_after("")[0]
        self.assertEqual(event["id"], "relay:1")
        self.assertNotIn("dedupe_key", event)  # internal, not part of the stream

    def test_sender_supplied_id_is_ignored(self):
        feed = make_feed()
        feed.publish_relay_event(relay_event(id="../../spoofed"))
        self.assertEqual(feed.snapshot_after("")[0]["id"], "relay:1")

    def test_two_agents_hearing_one_say_publish_once(self):
        feed = make_feed()
        self.assertEqual(feed.publish_relay_event(relay_event(source="Farstrider")), "published")
        self.assertEqual(feed.publish_relay_event(relay_event(source="Shadowblade")), "duplicate")
        self.assertEqual(len(feed.snapshot_after("")), 1)
        self.assertEqual(feed.health()["ingest_duplicates"], 1)

    def test_the_same_line_again_after_the_window_is_published(self):
        feed = make_feed(window=0.15)  # the test clock advances 0.1 per publish
        self.assertEqual(feed.publish_relay_event(relay_event()), "published")
        self.assertEqual(feed.publish_relay_event(relay_event()), "duplicate")
        self.assertEqual(feed.publish_relay_event(relay_event()), "published")

    def test_private_kinds_are_filtered_not_published(self):
        feed = make_feed()
        self.assertEqual(feed.publish_relay_event(relay_event(kind="whisper", text="psst")),
                         "filtered")
        self.assertEqual(feed.snapshot_after(""), [])

    def test_rejected_payload_is_counted(self):
        feed = make_feed()
        self.assertEqual(feed.publish_relay_event({"kind": "say"}), "rejected")
        self.assertEqual(feed.health()["ingest_rejected"], 1)

    def test_a_waiting_stream_is_woken(self):
        feed = make_feed()
        feed.publish_relay_event(relay_event())
        self.assertEqual(feed.wait_after("", timeout=0.1)[0]["text"], "hello there")


class HandlerTests(unittest.TestCase):
    """Drive app.Handler's methods directly, the way test_parser.py drives
    the tailer — no socket, no server thread."""

    def make_handler(self, body, *, path="/api/chat/ingest", token="", auth=None):
        import io
        from unittest import mock

        handler = app.Handler.__new__(app.Handler)
        raw = json.dumps(body).encode("utf-8")
        headers = {"Content-Length": str(len(raw))}
        if auth:
            headers["Authorization"] = auth
        handler.headers = headers
        handler.rfile = io.BytesIO(raw)
        handler.path = path
        handler.server = mock.Mock(feed=make_feed(), ingest_token=token)
        handler.send_json = mock.Mock()
        return handler

    def post(self, handler):
        handler.do_POST()
        return handler.send_json.call_args[0]

    def test_accepts_a_batch_and_reports_counts(self):
        handler = self.make_handler({"events": [relay_event(),
                                                relay_event(source="Shadowblade"),
                                                relay_event(kind="whisper", dedupe_key="w1")]})
        status, body = self.post(handler)
        self.assertEqual(status, 202)
        self.assertEqual(body, {"published": 1, "duplicates": 1, "filtered": 1, "rejected": 0})

    def test_rejects_a_bad_token(self):
        handler = self.make_handler({"events": [relay_event()]}, token="s3cret", auth="Bearer wrong")
        self.assertEqual(self.post(handler)[0], 401)

    def test_accepts_the_right_token(self):
        handler = self.make_handler({"events": [relay_event()]}, token="s3cret", auth="Bearer s3cret")
        self.assertEqual(self.post(handler)[0], 202)

    def test_oversized_body_is_refused(self):
        handler = self.make_handler({"events": [relay_event()]})
        handler.headers["Content-Length"] = str(app.MAX_INGEST_BYTES + 1)
        self.assertEqual(self.post(handler)[0], 413)

    def test_invalid_json_is_a_400(self):
        import io
        handler = self.make_handler({"events": []})
        handler.rfile = io.BytesIO(b"{nope")
        handler.headers["Content-Length"] = "5"
        self.assertEqual(self.post(handler)[0], 400)

    def test_unknown_post_path_is_a_404(self):
        handler = self.make_handler({"events": []}, path="/api/chat/stream")
        self.assertEqual(self.post(handler)[0], 404)

    def test_cors_headers_are_sent(self):
        from unittest import mock

        handler = app.Handler.__new__(app.Handler)
        handler.send_header = mock.Mock()
        handler.send_cors_headers()
        self.assertIn(("Access-Control-Allow-Origin", "*"),
                      [call.args for call in handler.send_header.call_args_list])


if __name__ == "__main__":
    unittest.main()
