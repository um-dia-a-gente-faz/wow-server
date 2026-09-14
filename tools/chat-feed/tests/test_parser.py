import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app import parse_chat_line  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
