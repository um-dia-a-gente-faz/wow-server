"""Unit tests for agent.names: request builders, response parsers (against
hand-built bytes matching the layout cited in agent/names.py), and NameCache
(dedupe, budget, persistence)."""

import json
import os
import struct
import tempfile
import unittest

from agent import names as nm
from agent import packets as pk


def cstring(s: str) -> bytes:
    return s.encode('utf-8') + b'\x00'


class BuildQueryTest(unittest.TestCase):
    def test_name_query(self):
        self.assertEqual(nm.build_name_query(0x1234), struct.pack('<Q', 0x1234))

    def test_creature_query(self):
        self.assertEqual(nm.build_creature_query(17213, 0x1234), struct.pack('<IQ', 17213, 0x1234))

    def test_gameobject_query(self):
        self.assertEqual(nm.build_gameobject_query(181646, 0x1234), struct.pack('<IQ', 181646, 0x1234))


class ParseNameQueryResponseTest(unittest.TestCase):
    def test_found(self):
        guid = 0x0000000000000007
        payload = (pk.pack_packed_guid(guid) + bytes([0])
                   + cstring("Rubens") + cstring("")
                   + bytes([2, 0, 1])  # race, sex, class
                   + bytes([0]))       # has_declined_names
        info = nm.parse_name_query_response(payload)
        self.assertEqual(info, {"guid": guid, "found": True, "name": "Rubens", "realm": "",
                                 "race": 2, "sex": 0, "class_": 1, "has_declined_names": False})

    def test_not_found(self):
        guid = 0xDEAD
        payload = pk.pack_packed_guid(guid) + bytes([1])
        info = nm.parse_name_query_response(payload)
        self.assertEqual(info, {"guid": guid, "found": False})


def creature_response_body(name="Broom", subname="", cursor_name="", flags=0,
                            creature_type=8, creature_family=0, classification=0) -> bytes:
    body = (cstring(name) + bytes([0, 0, 0])
            + cstring(subname) + cstring(cursor_name)
            + struct.pack('<I', flags)
            + struct.pack('<I', creature_type)
            + struct.pack('<I', creature_family)
            + struct.pack('<I', classification)
            + struct.pack(f'<{nm.MAX_KILL_CREDIT}I', *([0] * nm.MAX_KILL_CREDIT))
            + struct.pack(f'<{nm.MAX_CREATURE_MODELS}I', *([0] * nm.MAX_CREATURE_MODELS))
            + struct.pack('<2f', 1.0, 1.0)
            + bytes([0])  # leader
            + struct.pack(f'<{nm.MAX_CREATURE_QUEST_ITEMS}I', *([0] * nm.MAX_CREATURE_QUEST_ITEMS))
            + struct.pack('<I', 0))  # movement_info_id
    return body


class ParseCreatureQueryResponseTest(unittest.TestCase):
    def test_found(self):
        payload = struct.pack('<I', 17213) + creature_response_body(
            name="Broom", subname="", creature_type=8, classification=0)
        info = nm.parse_creature_query_response(payload)
        self.assertTrue(info["found"])
        self.assertEqual(info["entry"], 17213)
        self.assertEqual(info["name"], "Broom")
        self.assertEqual(info["creature_type_name"], "critter")
        self.assertEqual(info["rank"], "normal")

    def test_elite_rank(self):
        payload = struct.pack('<I', 100) + creature_response_body(name="Boss", classification=3)
        info = nm.parse_creature_query_response(payload)
        self.assertEqual(info["rank"], "worldboss")

    def test_not_found(self):
        payload = struct.pack('<I', 999 | 0x80000000)
        info = nm.parse_creature_query_response(payload)
        self.assertEqual(info, {"entry": 999, "found": False})

    def test_leftover_bytes_raise(self):
        payload = struct.pack('<I', 1) + creature_response_body() + b'\xFF'
        with self.assertRaises(ValueError):
            nm.parse_creature_query_response(payload)

    def test_subname_is_kept(self):
        payload = struct.pack('<I', 1) + creature_response_body(name="Guard", subname="of Silvermoon")
        info = nm.parse_creature_query_response(payload)
        self.assertEqual(info["subname"], "of Silvermoon")


def gameobject_response_body(name="Ship", icon="", cast_bar="", unk="", size=1.0) -> bytes:
    return (struct.pack('<I', 0)   # type
            + struct.pack('<I', 0)  # display_id
            + cstring(name) + bytes([0, 0, 0])
            + cstring(icon) + cstring(cast_bar) + cstring(unk)
            + struct.pack(f'<{nm.MAX_GAMEOBJECT_DATA}I', *([0] * nm.MAX_GAMEOBJECT_DATA))
            + struct.pack('<f', size)
            + struct.pack(f'<{nm.MAX_GAMEOBJECT_QUEST_ITEMS}I', *([0] * nm.MAX_GAMEOBJECT_QUEST_ITEMS)))


class ParseGameObjectQueryResponseTest(unittest.TestCase):
    def test_found(self):
        payload = struct.pack('<I', 181646) + gameobject_response_body(name="Ship, Night Elf")
        info = nm.parse_gameobject_query_response(payload)
        self.assertTrue(info["found"])
        self.assertEqual(info["name"], "Ship, Night Elf")

    def test_not_found(self):
        payload = struct.pack('<I', 42 | 0x80000000)
        info = nm.parse_gameobject_query_response(payload)
        self.assertEqual(info, {"entry": 42, "found": False})

    def test_leftover_bytes_raise(self):
        payload = struct.pack('<I', 1) + gameobject_response_body() + b'\xFF'
        with self.assertRaises(ValueError):
            nm.parse_gameobject_query_response(payload)


class NameCacheDedupeTest(unittest.TestCase):
    def test_want_creates_pending_item(self):
        c = nm.NameCache()
        c.want_creature(17213, 0x1)
        self.assertEqual(c.drain(), [("creature", 17213, 0x1)])

    def test_duplicate_want_before_response_is_counted_not_requeued(self):
        c = nm.NameCache()
        c.want_creature(17213, 0x1)
        c.want_creature(17213, 0x2)  # still in-flight — a duplicate
        self.assertEqual(c.duplicate_queries, 1)
        self.assertEqual(len(c.drain()), 1)

    def test_known_entry_is_not_requeried(self):
        c = nm.NameCache()
        c.creatures[17213] = {"name": "Broom", "found": True}
        c.want_creature(17213, 0x1)
        self.assertEqual(c.drain(), [])

    def test_response_clears_in_flight_and_allows_a_future_requery_of_new_entries(self):
        c = nm.NameCache(cache_path="/nonexistent/should-not-write")
        c.want_creature(1, 0x1)
        c.drain()
        c.on_creature_query_response({"entry": 1, "found": True, "name": "X"})
        self.assertEqual(c.creatures[1]["name"], "X")
        c.want_creature(2, 0x1)
        self.assertEqual(len(c.drain()), 1)

    def test_not_found_response_is_cached_as_none(self):
        c = nm.NameCache(cache_path="/nonexistent/should-not-write")
        c.want_creature(1, 0x1)
        c.drain()
        c.on_creature_query_response({"entry": 1, "found": False})
        self.assertIsNone(c.creatures[1])
        # a second want() must not re-query — the "not found" result is cached too
        c.want_creature(1, 0x1)
        self.assertEqual(c.drain(), [])


class NameCacheBudgetTest(unittest.TestCase):
    def test_drain_respects_budget_per_second(self):
        now = [0.0]
        c = nm.NameCache(budget_per_second=3, clock=lambda: now[0])
        for i in range(5):
            c.want_creature(i, 0x1)
        first_batch = c.drain()
        self.assertEqual(len(first_batch), 3)
        self.assertEqual(c.drain(), [])  # budget exhausted this second

        now[0] = 1.1  # window rolls over
        second_batch = c.drain()
        self.assertEqual(len(second_batch), 2)

    def test_max_items_caps_a_single_drain(self):
        c = nm.NameCache(budget_per_second=10)
        for i in range(5):
            c.want_creature(i, 0x1)
        self.assertEqual(len(c.drain(max_items=2)), 2)


class NameCachePersistenceTest(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "names.json")
            c1 = nm.NameCache()
            c1.creatures[17213] = {"entry": 17213, "found": True, "name": "Broom"}
            c1.gameobjects[181646] = {"entry": 181646, "found": True, "name": "Ship"}
            c1.save(path)

            c2 = nm.NameCache()
            c2.load(path)
            self.assertEqual(c2.creatures[17213]["name"], "Broom")
            self.assertEqual(c2.gameobjects[181646]["name"], "Ship")

    def test_player_names_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            c = nm.NameCache()
            c.players[7] = "Rubens"
            c.save(path)
            with open(path) as f:
                data = json.load(f)
            self.assertNotIn("players", data)

    def test_load_missing_file_is_a_no_op(self):
        c = nm.NameCache()
        c.load("/nonexistent/path/names.json")
        self.assertEqual(c.creatures, {})

    def test_load_corrupt_file_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            with open(path, 'w') as f:
                f.write("not json{{{")
            c = nm.NameCache()
            c.load(path)  # must not raise
            self.assertEqual(c.creatures, {})

    def test_on_creature_response_persists_found_entries(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            c = nm.NameCache(cache_path=path)
            c.on_creature_query_response({"entry": 1, "found": True, "name": "X"})
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data["creatures"]["1"]["name"], "X")


if __name__ == '__main__':
    unittest.main()
