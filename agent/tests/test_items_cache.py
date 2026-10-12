"""ItemCache persistence (ADR 0018, slice 1): the schema guard, same rules as NameCache."""
import json
import os
import tempfile
import unittest
from unittest import mock

from agent import items as it


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


if __name__ == "__main__":
    unittest.main()
