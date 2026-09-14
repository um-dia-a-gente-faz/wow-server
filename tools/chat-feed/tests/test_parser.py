import os
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402
from app import ChatFeed, parse_chat_line, tail_log  # noqa: E402


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.lines = (pathlib.Path(__file__).parent / "fixtures" / "chat.log").read_text().splitlines()

    def parse(self, index):
        return parse_chat_line(self.lines[index], "inode:42", "2026-09-14T12:34:56Z")

    def test_say_preserves_colons(self):
        self.assertEqual(self.parse(0), {"id": "inode:42", "at": "2026-09-14T12:34:56Z",
                                         "kind": "say", "sender": "Alice", "channel": None,
                                         "text": "Hello: world"})

    def test_yell(self):
        self.assertEqual(self.parse(1)["kind"], "yell")
        self.assertEqual(self.parse(1)["sender"], "Bob")

    def test_guild_is_classified_for_private_filtering(self):
        event = self.parse(2)
        self.assertEqual((event["kind"], event["text"]), ("guild", "Ready: now"))

    def test_channel(self):
        event = self.parse(3)
        self.assertEqual((event["kind"], event["channel"], event["text"]),
                         ("channel", "world", "LFG: RFC"))

    def test_unknown_candidate_is_an_event(self):
        event = self.parse(4)
        self.assertEqual((event["kind"], event["sender"]), ("unknown", None))

    def test_non_chat_line_is_ignored(self):
        self.assertIsNone(parse_chat_line("2026-09-14 INFO server started"))

    def test_engine_line_with_player_is_counted_but_not_published(self):
        feed = ChatFeed(capacity=10, public_kinds={"say", "yell", "channel"})

        feed.publish_line("Loading Player Totem models...\n", "inode:43")

        self.assertEqual(feed.health(), {"ok": True, "events": 0,
                                         "chat_candidates": 1, "parse_errors": 1})
        self.assertEqual(feed.snapshot_after(""), [])


class TailerTests(unittest.TestCase):
    def test_partial_line_is_rewound_without_publishing(self):
        class PartialLineHandle:
            def __init__(self):
                self.position = 0
                self.seek_calls = []

            def close(self):
                pass

            def seek(self, offset, whence=os.SEEK_SET):
                self.seek_calls.append((offset, whence))
                self.position = offset

            def tell(self):
                return self.position

            def readline(self):
                self.position += len("partial chat line")
                return "partial chat line"

        handle = PartialLineHandle()
        feed = mock.Mock()
        stat = SimpleNamespace(st_dev=1, st_ino=2, st_size=0)
        with mock.patch.object(app.os, "stat", return_value=stat), \
             mock.patch("builtins.open", return_value=handle), \
             mock.patch.object(app.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                tail_log(feed, "/logs/Server.log")

        feed.publish_line.assert_not_called()
        self.assertEqual(handle.seek_calls, [(0, os.SEEK_END), (0, os.SEEK_SET)])


if __name__ == "__main__":
    unittest.main()
