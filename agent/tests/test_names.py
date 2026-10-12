"""Unit tests for agent.names: request builders, response parsers (against
hand-built bytes matching the layout cited in agent/names.py), and NameCache
(dedupe, budget, persistence)."""

import itertools
import json
import os
import struct
import tempfile
import unittest
from unittest import mock

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


class NameCacheBoundsTest(unittest.TestCase):
    """#569: queries that are never answered must not grow the cache without bound.
    Fake clock throughout (never the real one, never a sleep)."""

    def setUp(self):
        self.now = [0.0]
        self.c = nm.NameCache(clock=lambda: self.now[0], cache_path="/nonexistent/should-not-write")

    def test_unanswered_queries_keep_both_collections_bounded(self):
        for guid in range(1, 2001):
            self.c.want_player(guid)
        self.assertLessEqual(len(self.c._pending), nm.MAX_PENDING)
        self.assertLessEqual(len(self.c._in_flight), nm.MAX_PENDING)
        self.assertEqual(self.c.dropped_queries, 2000 - nm.MAX_PENDING)
        self.assertEqual(self.c.expired_queries, 0)

    def test_sent_query_expires_and_is_asked_again(self):
        for entry in range(1, 11):
            self.c.want_creature(entry, 0x1)
        self.assertEqual(len(self.c.drain()), 10)  # all ten sent at t=0
        self.now[0] = nm.QUERY_TIMEOUT_S + 1.0
        self.assertEqual(self.c.drain(), [])
        self.assertEqual(self.c.expired_queries, 10)
        self.assertEqual(len(self.c._in_flight), 0)
        self.assertNotIn(3, self.c.creatures)  # an expiry is not a "not found" answer
        self.c.want_creature(3, 0x1)
        self.assertEqual(self.c.drain(), [("creature", 3, 0x1)])

    def test_answer_inside_timeout_clears_token_and_fills_cache(self):
        self.c.want_player(7)
        self.c.drain()
        self.now[0] = 5.0
        self.c.on_name_query_response({"guid": 7, "found": True, "name": "Rubens"})
        self.assertEqual(self.c.players[7], "Rubens")
        self.assertEqual(len(self.c._in_flight), 0)
        self.now[0] = 60.0
        self.c.drain()
        self.assertEqual(self.c.expired_queries, 0)  # the answered token never expires

    def test_answer_after_expiry_still_fills_cache(self):
        self.c.want_player(8)
        self.c.drain()
        self.now[0] = nm.QUERY_TIMEOUT_S + 1.0
        self.c.drain()
        self.assertEqual(self.c.expired_queries, 1)
        self.c.on_name_query_response({"guid": 8, "found": True, "name": "Late"})  # must not raise
        self.assertEqual(self.c.players[8], "Late")

    def test_cap_drops_the_oldest_pending_query_and_it_can_be_wanted_again(self):
        c = nm.NameCache(budget_per_second=10**6, clock=lambda: self.now[0],
                         cache_path="/nonexistent/should-not-write")
        for guid in range(1, nm.MAX_PENDING + 2):  # one over the cap: guid 1 is dropped
            c.want_player(guid)
        self.assertEqual(c.dropped_queries, 1)
        c.want_player(1)  # not in flight any more, so this queues it again (and drops guid 2)
        self.assertEqual(c.duplicate_queries, 0)
        self.assertEqual(c.dropped_queries, 2)
        sent = {item[1] for item in c.drain()}
        self.assertIn(1, sent)
        self.assertNotIn(2, sent)

    def test_steady_stream_with_drain_every_100ms_stays_bounded(self):
        c = nm.NameCache(budget_per_second=10, clock=lambda: self.now[0],
                         cache_path="/nonexistent/should-not-write")
        limit = nm.MAX_PENDING + 10 * nm.QUERY_TIMEOUT_S
        guids = iter(range(1, 10001))
        for step in range(1200):  # 120 simulated seconds, a drain every 100 ms
            self.now[0] = step * 0.1
            for guid in itertools.islice(guids, 9):
                c.want_player(guid)
            c.drain()
            self.assertLessEqual(len(c._pending), limit)
            self.assertLessEqual(len(c._in_flight), limit)


class NameCachePersistenceTest(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "names.json")
            c1 = nm.NameCache()
            c1.creatures[17213] = {"entry": 17213, "found": True, "name": "Broom"}
            c1.gameobjects[181646] = {"entry": 181646, "found": True, "name": "Ship", "type": 0}
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
        with self.assertNoLogs("agent.names", "WARNING"):
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

    def test_load_survives_malformed_cache_files(self):
        cases = {
            "top-level list": "[]",
            "non-numeric key": '{"creatures": {"abc": {"name": "x"}}}',
            "long non-numeric key": '{"creatures": {"' + "k" * 10000 + '": {}}}',
            "newline in key": '{"creatures": {"\\n3\\n": 5}}',
            "newline in non-numeric key": '{"creatures": {"a\\nb": {}}}',
            "section is a list": '{"creatures": []}',
            "wrong value type": '{"creatures": {"1": 5}}',
            "4300-digit key": '{"creatures": {"' + "1" * 4300 + '": 5}}',
            "creature without name": '{"creatures": {"1": {}}}',
            "creature name not a string": '{"creatures": {"1": {"name": 5}}}',
            "creature name null": '{"creatures": {"1": {"name": null}}}',
            "gameobject without type": '{"gameobjects": {"1": {"name": "x"}}}',
            "gameobject type a string": '{"gameobjects": {"1": {"name": "x", "type": "3"}}}',
            "gameobject type a bool": '{"gameobjects": {"1": {"name": "x", "type": true}}}',
            "truncated": '{"creatures": {"1": {"name": "Bro',
            "deeply nested": "[" * 100000 + "]" * 100000,
            "empty file": "",
        }
        with tempfile.TemporaryDirectory() as d:
            for label, content in cases.items():
                with self.subTest(label):
                    path = os.path.join(d, "names.json")
                    with open(path, 'w') as f:
                        f.write(content)
                    c = nm.NameCache()
                    with self.assertLogs("agent.names", "WARNING") as logs:
                        c.load(path)  # must not raise
                    self.assertEqual(c.creatures, {})
                    self.assertEqual(c.gameobjects, {})
                    self.assertEqual(len(logs.output), 1)  # exactly one warning per failed load
                    self.assertIn(path, logs.output[0])
                    self.assertNotIn("\n", logs.output[0])  # one line, whatever the file held
                    self.assertLess(len(logs.output[0]), 500)  # bounded, whatever the file held
                    self.assertLessEqual(len(logs.output[0].replace(path, "")), 300)
                    self.assertEqual(_read(path + ".corrupt"), content.encode())  # set aside
                    self.assertFalse(os.path.exists(path))

    def test_one_bad_entry_keeps_nothing_from_the_file(self):
        content = ('{"creatures": {"1": {"name": "Wolf"}, "2": {"name": 5}},'
                   ' "gameobjects": {"3": {"name": "Chest", "type": 3}}}')
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            _put(path, content)
            c = nm.NameCache()
            with self.assertLogs("agent.names", "WARNING") as logs:
                c.load(path)
            self.assertEqual(len(logs.output), 1)
            self.assertEqual((c.creatures, c.gameobjects), ({}, {}))

    def test_entries_of_the_current_schema_load_unchanged(self):
        content = ('{"schema": 1, "creatures": {"1": null, "2": {"name": "Wolf", "extra": 1}},'
                   ' "gameobjects": {"3": {"name": "Chest", "type": 3}}}')
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            _put(path, content)
            c = nm.NameCache()
            with self.assertNoLogs("agent.names", "WARNING"):
                c.load(path)
            self.assertEqual(c.creatures, {1: None, 2: {"name": "Wolf", "extra": 1}})
            self.assertEqual(c.gameobjects, {3: {"name": "Chest", "type": 3}})

    def test_what_the_agent_saves_loads_back_equal(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            c1 = nm.NameCache(cache_path=path)
            c1.on_creature_query_response(nm.parse_creature_query_response(
                struct.pack('<I', 17213) + creature_response_body(name="Broom", subname="Friend")))
            c1.on_creature_query_response({"entry": 5, "found": False})
            c1.on_gameobject_query_response(nm.parse_gameobject_query_response(
                struct.pack('<I', 181646) + gameobject_response_body(name="Ship")))
            c2 = nm.NameCache()
            with self.assertNoLogs("agent.names", "WARNING"):
                c2.load(path)
            self.assertEqual(c2.creatures, c1.creatures)
            self.assertEqual(c2.gameobjects, c1.gameobjects)

    def test_on_creature_response_persists_found_entries(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            c = nm.NameCache(cache_path=path)
            c.on_creature_query_response({"entry": 1, "found": True, "name": "X"})
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data["creatures"]["1"]["name"], "X")


def main_loader(cache, path):
    """NameCache.load as it is on main (bd20f6a), reduced to what it reads: the old reader."""
    with open(path) as f:
        data = json.load(f)
    cache.creatures.update({int(k): v for k, v in data.get("creatures", {}).items()})
    cache.gameobjects.update({int(k): v for k, v in data.get("gameobjects", {}).items()})


def _put(path, content):
    with open(path, "wb" if isinstance(content, bytes) else "w") as f:
        f.write(content)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


UNREADABLE_FILES = {
    "zero bytes": b"",
    "truncated": b'{"creatures": {"1": {"name": "Bro',
    "list": b"[]",
    "bad key": b'{"creatures": {"abc": 1}}',
    "string schema": b'{"schema": "x"}',
    "bool schema": b'{"schema": true}',
    "deep": b"[" * 100000 + b"]" * 100000,
    "bad utf8": b'{"creatures": {"1": {"name": "\xff"}}}',
    "gameobjects bad, creatures fine": b'{"creatures": {"1": {"name": "a"}}, "gameobjects": {"x": {}}}',
}


class NameCacheSchemaTest(unittest.TestCase):
    def test_file_written_by_main_loads_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            _put(path, json.dumps({"creatures": {"1": {"name": "A"}, "2": None},
                                   "gameobjects": {"3": {"name": "B", "type": 0}}}))
            c = nm.NameCache()
            with self.assertNoLogs("agent.names", "WARNING"):
                c.load(path)
            self.assertEqual(c.creatures, {1: {"name": "A"}, 2: None})
            self.assertEqual(c.gameobjects, {3: {"name": "B", "type": 0}})
            self.assertFalse(os.path.exists(path + ".corrupt"))

    def test_saved_file_has_schema_1_and_the_old_loader_reads_it(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            c1 = nm.NameCache()
            c1.creatures[1] = {"name": "A"}
            c1.gameobjects[2] = None
            c1.save(path)
            self.assertEqual(json.loads(_read(path))["schema"], 1)
            c2 = nm.NameCache()
            main_loader(c2, path)
            self.assertEqual((c2.creatures, c2.gameobjects), ({1: {"name": "A"}}, {2: None}))

    def test_newer_file_loads_nothing_and_is_never_written(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            other = os.path.join(d, "other.json")
            _put(path, '{"schema": 2, "creatures": {"1": {"name": "A"}}}')
            os.utime(path, (1000000000, 1000000000))
            before = _read(path)
            c = nm.NameCache(cache_path=path)
            with self.assertLogs("agent.names", "WARNING") as logs:
                c.load()
            self.assertEqual(len(logs.output), 1)
            self.assertIn(path, logs.output[0])
            self.assertEqual((c.creatures, c.gameobjects), ({}, {}))
            c.creatures[5] = {"name": "X"}
            c.save()
            c.save(path)
            c.on_creature_query_response({"entry": 6, "found": True, "name": "Y"})
            self.assertEqual(_read(path), before)
            self.assertEqual(os.stat(path).st_mtime, 1000000000)
            self.assertEqual(sorted(os.listdir(d)), ["names.json"])
            c.save(other)   # saving is off per path, not per instance
            self.assertEqual(set(json.loads(_read(other))["creatures"]), {"5", "6"})

    def test_unreadable_files_are_set_aside_and_the_next_save_is_fresh(self):
        for label, content in UNREADABLE_FILES.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "names.json")
                _put(path, content)
                _put(path + ".corrupt", b"older corpse")
                c = nm.NameCache()
                with self.assertLogs("agent.names", "WARNING") as logs:
                    c.load(path)
                self.assertEqual(len(logs.output), 1)
                self.assertIn(path, logs.output[0])
                self.assertNotIn("\n", logs.output[0])
                self.assertLess(len(logs.output[0]), 500)
                self.assertEqual((c.creatures, c.gameobjects), ({}, {}))
                self.assertEqual(_read(path + ".corrupt"), content)
                self.assertFalse(os.path.exists(path))
                c.creatures[7] = {"name": "Z"}
                c.save(path)
                c2 = nm.NameCache()
                with self.assertNoLogs("agent.names", "WARNING"):
                    c2.load(path)
                self.assertEqual(c2.creatures, {7: {"name": "Z"}})

    def test_failed_move_aside_is_swallowed_and_save_still_replaces(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            _put(path, b"[]")
            os.mkdir(path + ".corrupt")
            c = nm.NameCache()
            with self.assertLogs("agent.names", "WARNING"):
                c.load(path)
            self.assertEqual(_read(path), b"[]")
            c.creatures[1] = None
            c.save(path)
            self.assertEqual(json.loads(_read(path))["creatures"], {"1": None})

    def test_unreadable_in_read_only_directory_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            _put(path, b"[]")
            os.chmod(d, 0o500)
            try:
                c = nm.NameCache()
                with self.assertLogs("agent.names", "WARNING"):
                    c.load(path)
                c.creatures[1] = None
                c.save(path)   # must not raise either
            finally:
                os.chmod(d, 0o700)

    def test_missing_file_is_silent_and_leaves_saving_on(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            c = nm.NameCache()
            with self.assertNoLogs("agent.names", "WARNING"):
                c.load(path)
            c.creatures[1] = None
            c.save(path)
            self.assertEqual(json.loads(_read(path))["creatures"], {"1": None})

    def test_older_file_is_a_silent_cold_start_replaced_by_the_next_save(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "names.json")
            _put(path, '{"schema": 1, "creatures": {"1": {"name": "A"}}}')
            c = nm.NameCache()
            with mock.patch.object(nm, "CACHE_SCHEMA", 2):
                with self.assertNoLogs("agent.names", "WARNING"):
                    c.load(path)
                self.assertEqual(c.creatures, {})
                c.save(path)
                self.assertEqual(json.loads(_read(path)),
                                 {"schema": 2, "creatures": {}, "gameobjects": {}})
            self.assertFalse(os.path.exists(path + ".corrupt"))


if __name__ == '__main__':
    unittest.main()
