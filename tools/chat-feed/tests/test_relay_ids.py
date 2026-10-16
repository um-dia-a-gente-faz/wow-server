"""gh-757: relay ids stay unique across chat-feed restarts (`relay:<start_ms>:<n>`)."""

import pathlib
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app import ChatFeed  # noqa: E402

PUBLIC = {"say", "yell", "channel", "emote", "system"}
OLD_START = 1_700_000_000_000
NEW_START = 1_700_000_500_000  # a later process start, in milliseconds


def relay_event(text):
    return {"at": "2026-10-16T10:00:00.000Z", "kind": "say", "sender": "Rubens",
            "text": text, "source": "Farstrider", "dedupe_key": text}


def feed(start_ms):
    return ChatFeed(capacity=10, public_kinds=PUBLIC, start_ms=start_ms)


class RelayIdTests(unittest.TestCase):
    def test_two_processes_never_produce_the_same_id(self):
        before, after = feed(OLD_START), feed(NEW_START)
        for text in ("one", "two", "three"):
            before.publish_relay_event(relay_event(text))
            after.publish_relay_event(relay_event(text))
        old_ids = [e["id"] for e in before.snapshot_after("")]
        new_ids = [e["id"] for e in after.snapshot_after("")]
        self.assertEqual(len(old_ids), 3)
        self.assertFalse(set(old_ids) & set(new_ids), (old_ids, new_ids))

    def test_id_is_start_then_sequence(self):
        f = feed(OLD_START)
        f.publish_relay_event(relay_event("hi"))
        self.assertEqual(f.snapshot_after("")[0]["id"], f"relay:{OLD_START}:1")

    def test_start_defaults_to_the_construction_time_in_milliseconds(self):
        before_ms = int(time.time() * 1000)
        f = ChatFeed(capacity=10, public_kinds=PUBLIC)
        after_ms = int(time.time() * 1000)
        f.publish_relay_event(relay_event("hi"))
        prefix, start, n = f.snapshot_after("")[0]["id"].split(":")
        self.assertEqual((prefix, n), ("relay", "1"))
        self.assertTrue(before_ms <= int(start) <= after_ms, start)

    def test_resuming_with_an_old_or_foreign_id_returns_everything_buffered(self):
        f = feed(OLD_START)
        f.publish_relay_event(relay_event("a"))
        f.publish_relay_event(relay_event("b"))
        everything = [f"relay:{OLD_START}:1", f"relay:{OLD_START}:2"]
        for last in ("relay:1", f"relay:{NEW_START}:1", "2490369:5120"):
            self.assertEqual([e["id"] for e in f.snapshot_after(last)], everything, last)
            self.assertEqual([e["id"] for e in f.wait_after(last, timeout=0.01)], everything, last)


if __name__ == "__main__":
    unittest.main()
