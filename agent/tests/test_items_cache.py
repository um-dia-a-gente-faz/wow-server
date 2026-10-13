"""ItemCache persistence (ADR 0018, slice 1): the schema guard, same rules as NameCache."""
import json
import os
import struct
import tempfile
import unittest
from unittest import mock

from agent import items as it
from agent import loot as lo
from agent.tests import test_loot as tl


def main_loader(cache, path):
    """ItemCache.load as it is on main, reduced to what it reads: the old reader."""
    with open(path) as f:
        data = json.load(f)
    cache.items.update({int(k): v for k, v in data.get("items", {}).items()})


def _put(path, content):
    with open(path, "wb" if isinstance(content, bytes) else "w") as f:
        f.write(content)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


UNREADABLE_FILES = {
    "zero bytes": b"",
    "truncated": b'{"items": {"1": {"name": "Bro',
    "list": b"[]",
    "bad key": b'{"items": {"abc": {}}}',
    "section is a list": b'{"items": []}',
    "wrong value type": b'{"items": {"1": 5}}',
    "string schema": b'{"schema": "x"}',
    "deep": b"[" * 100000 + b"]" * 100000,
    "bad utf8": b'{"items": {"1": {"name": "\xff"}}}',
}


class ItemCacheSchemaTest(unittest.TestCase):
    def test_round_trip_and_file_written_by_main_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "items.json")
            _put(path, json.dumps({"items": {"25": {"name": "Worn Shortsword"}, "26": None}}))
            c = it.ItemCache()
            with self.assertNoLogs("agent.items", "WARNING"):
                c.load(path)
            self.assertEqual(c.items, {25: {"name": "Worn Shortsword"}, 26: None})
            c.save(path)
            self.assertEqual(json.loads(_read(path))["schema"], 1)
            c2 = it.ItemCache()
            main_loader(c2, path)   # the old reader still reads the new file
            self.assertEqual(c2.items, c.items)

    def test_newer_file_loads_nothing_and_is_never_written(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "items.json")
            other = os.path.join(d, "other.json")
            _put(path, '{"schema": 2, "items": {"1": {"name": "A"}}}')
            os.utime(path, (1000000000, 1000000000))
            before = _read(path)
            c = it.ItemCache(cache_path=path)
            with self.assertLogs("agent.items", "WARNING") as logs:
                c.load()
            self.assertEqual(len(logs.output), 1)
            self.assertIn(path, logs.output[0])
            self.assertEqual(c.items, {})
            c.items[5] = {"name": "X"}
            c.save()
            c.on_item_query_response({"entry": 6, "found": True, "name": "Y"})
            c.save()
            self.assertEqual(_read(path), before)
            self.assertEqual(os.stat(path).st_mtime, 1000000000)
            self.assertEqual(sorted(os.listdir(d)), ["items.json"])
            c.save(other)
            self.assertEqual(set(json.loads(_read(other))["items"]), {"5", "6"})

    def test_unreadable_files_are_set_aside_and_the_next_save_is_fresh(self):
        for label, content in UNREADABLE_FILES.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "items.json")
                _put(path, content)
                _put(path + ".corrupt", b"older corpse")
                c = it.ItemCache()
                with self.assertLogs("agent.items", "WARNING") as logs:
                    c.load(path)
                self.assertEqual(len(logs.output), 1)
                self.assertIn(path, logs.output[0])
                self.assertNotIn("\n", logs.output[0])
                self.assertLess(len(logs.output[0]), 500)
                self.assertEqual(c.items, {})
                self.assertEqual(_read(path + ".corrupt"), content)
                c.items[7] = {"name": "Z"}
                c.save(path)
                c2 = it.ItemCache()
                with self.assertNoLogs("agent.items", "WARNING"):
                    c2.load(path)
                self.assertEqual(c2.items, {7: {"name": "Z"}})

    def test_failed_move_aside_is_swallowed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "items.json")
            _put(path, b"[]")
            os.mkdir(path + ".corrupt")
            c = it.ItemCache()
            with self.assertLogs("agent.items", "WARNING"):
                c.load(path)
            c.items[1] = None
            c.save(path)
            self.assertEqual(json.loads(_read(path))["items"], {"1": None})

    def test_missing_and_older_files_are_silent(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "items.json")
            c = it.ItemCache()
            with self.assertNoLogs("agent.items", "WARNING"):
                c.load(path)
            _put(path, '{"schema": 1, "items": {"1": {"name": "A"}}}')
            with mock.patch.object(it, "CACHE_SCHEMA", 2):
                with self.assertNoLogs("agent.items", "WARNING"):
                    c.load(path)
                self.assertEqual(c.items, {})
                c.save(path)
                self.assertEqual(json.loads(_read(path)), {"schema": 2, "items": {}})
            self.assertFalse(os.path.exists(path + ".corrupt"))

    def test_load_survives_malformed_entries(self):
        cases = {
            "item without name": '{"items": {"1": {}}}',
            "item name not a string": '{"items": {"1": {"name": 5}}}',
            "item name null": '{"items": {"1": {"name": null}}}',
            "long non-numeric key": '{"items": {"' + "k" * 10000 + '": {}}}',
            "newline in key": '{"items": {"\\n3\\n": 5}}',
            "newline in non-numeric key": '{"items": {"a\\nb": {}}}',
            "astral non-numeric key": '{"items": {"' + "\U0010ffff" * 40 + '": {}}}',
            "4301-digit key": '{"items": {"' + "1" * 4301 + '": {"name": "x"}}}',
        }
        with tempfile.TemporaryDirectory() as d:
            for label, content in cases.items():
                with self.subTest(label):
                    path = os.path.join(d, "items.json")
                    _put(path, content)
                    c = it.ItemCache()
                    with self.assertLogs("agent.items", "WARNING") as logs:
                        c.load(path)  # must not raise
                    self.assertEqual(c.items, {})
                    self.assertEqual(len(logs.output), 1)  # exactly one warning per failed load
                    self.assertIn(path, logs.output[0])
                    self.assertNotIn("\n", logs.output[0])
                    self.assertLess(len(logs.output[0]), 500)
                    self.assertLessEqual(len(logs.output[0].replace(path, "")), 300)
                    self.assertEqual(_read(path + ".corrupt"), content.encode())
                    self.assertFalse(os.path.exists(path))

    def test_one_bad_entry_keeps_nothing_from_the_file(self):
        content = '{"items": {"1": {"name": "Sword"}, "2": {"name": 5}}}'
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "items.json")
            _put(path, content)
            c = it.ItemCache()
            with self.assertLogs("agent.items", "WARNING") as logs:
                c.load(path)
            self.assertEqual(len(logs.output), 1)
            self.assertEqual(c.items, {})

    def test_entries_of_the_current_schema_load_unchanged(self):
        content = ('{"schema": 1, "items": {"1": null, "2": {"name": "Sword", "extra": 1}}}')
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "items.json")
            _put(path, content)
            c = it.ItemCache()
            with self.assertNoLogs("agent.items", "WARNING"):
                c.load(path)
            self.assertEqual(c.items, {1: None, 2: {"name": "Sword", "extra": 1}})

    def test_what_the_agent_saves_loads_back_equal(self):
        found = tl.ParseItemQueryResponseTest._build(None, entry=159, name="Tough Jerky",
                                                     stats=((4, 12),), damage=((10.0, 20.0, 0),))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "items.json")
            c1 = it.ItemCache(cache_path=path)
            c1.on_item_query_response(lo.parse_item_query_response(struct.pack('<I', 999 | 0x80000000)))
            c1.on_item_query_response(lo.parse_item_query_response(found))
            c1.save()
            c2 = it.ItemCache()
            with self.assertNoLogs("agent.items", "WARNING"):
                c2.load(path)
            self.assertEqual(c2.items, c1.items)
            self.assertEqual(c2.items[159]["name"], "Tough Jerky")
            self.assertFalse(os.path.exists(path + ".corrupt"))


if __name__ == "__main__":
    unittest.main()
