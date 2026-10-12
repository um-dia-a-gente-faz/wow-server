"""#705: the names/items cache files are written after WorldState._lock is released, not
while it is held (every snapshot() and reflex getter waits on that lock)."""

import tempfile
import types
import unittest
from unittest import mock

from agent import perception as per
from agent.handlers import loot as loot_h
from agent.handlers import world as world_h

CREATURE = {"entry": 1, "found": True, "name": "Boar"}
GAMEOBJECT = {"entry": 2, "found": True, "name": "Chest", "type": 3}
ITEM = {"entry": 3, "found": True, "name": "Sword"}


class CacheWriteOutsideLockTest(unittest.TestCase):
    def run_handler(self, handler, parser, data, cache):
        ws = per.WorldState()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws.names.cache_path = f"{tmp.name}/names.json"
        ws.items.cache_path = f"{tmp.name}/items.json"
        ctx = types.SimpleNamespace(state=types.SimpleNamespace(world_state=ws))
        held, written = [], []

        def fake_write(path, current, body):
            held.append(ws._lock.locked())
            written.append(path)

        with mock.patch(parser, return_value=data), \
                mock.patch("agent.persist.write_versioned", fake_write):
            handler(ctx, b"")
        self.assertEqual(written, [getattr(ws, cache).cache_path])
        self.assertEqual(held, [False], "the cache file was written while WorldState._lock was held")

    def test_creature_response(self):
        self.run_handler(world_h.handle_creature_query_response,
                         "agent.names.parse_creature_query_response", CREATURE, "names")

    def test_gameobject_response(self):
        self.run_handler(world_h.handle_gameobject_query_response,
                         "agent.names.parse_gameobject_query_response", GAMEOBJECT, "names")

    def test_item_response(self):
        self.run_handler(loot_h.handle_item_query_response,
                         "agent.loot.parse_item_query_response", ITEM, "items")


if __name__ == "__main__":
    unittest.main()
