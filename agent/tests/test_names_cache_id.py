"""#751: the server's SMSG_CLIENTCACHE_VERSION id against the id names.json was written under
(ADR 0016, further slice 3). Every rule case runs in both arrival orders."""

import json
import os
import tempfile
import unittest
from unittest import mock

from agent import names as nm
from agent import perception as per
from agent.tests.test_names import _put, _read, main_loader

ORDERS = ("server first", "load first")


def cache_file(path, cache_id="absent", creatures=None):
    """A schema-2 names.json; cache_id "absent" leaves the key out (a file from before #751)."""
    doc = {"schema": 2, "creatures": creatures if creatures is not None else {"1": {"name": "Old"}},
           "gameobjects": {}}
    if cache_id != "absent":
        doc["cache_id"] = cache_id
    _put(path, json.dumps(doc))


def met(order, path, server_id):
    """A cache that met the file and the server id in the given order."""
    c = nm.NameCache(cache_path=path)
    if order == "server first":
        c.set_server_cache_id(server_id)
        c.load()
    else:
        c.load()
        c.set_server_cache_id(server_id)
    return c


class CacheIdRuleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "names.json")

    def test_different_ids_discard_the_file_names_without_setting_the_file_aside(self):
        for order in ORDERS:
            with self.subTest(order):
                cache_file(self.path, cache_id=5)
                c = met(order, self.path, 6)
                self.assertEqual((c.creatures, c.gameobjects), ({}, {}))
                self.assertFalse(os.path.exists(self.path + ".corrupt"))
                c.creatures[2] = {"name": "New"}
                c.save()
                doc = json.loads(_read(self.path))
                self.assertEqual((doc["cache_id"], doc["creatures"]), (6, {"2": {"name": "New"}}))
                self.assertFalse(os.path.exists(self.path + ".corrupt"))

    def test_equal_ids_keep_the_names_silently(self):
        for order in ORDERS:
            with self.subTest(order):
                cache_file(self.path, cache_id=5)
                with self.assertNoLogs("agent.names", "INFO"):
                    c = met(order, self.path, 5)
                self.assertEqual(c.creatures, {1: {"name": "Old"}})

    def test_file_without_an_id_is_kept_and_the_next_save_stamps_the_server_id(self):
        for order in ORDERS:
            with self.subTest(order):
                cache_file(self.path)
                with self.assertNoLogs("agent.names", "INFO"):
                    c = met(order, self.path, 7)
                self.assertEqual(c.creatures, {1: {"name": "Old"}})   # no name lost at upgrade
                c.save()
                doc = json.loads(_read(self.path))
                self.assertEqual((doc["cache_id"], doc["creatures"]), (7, {"1": {"name": "Old"}}))

    def test_unknown_server_id_keeps_the_names_and_never_overwrites_the_stored_id(self):
        for order in ORDERS:
            with self.subTest(order):
                cache_file(self.path, cache_id=5)
                c = met(order, self.path, None)
                self.assertEqual(c.creatures, {1: {"name": "Old"}})
                c.save()
                self.assertEqual(json.loads(_read(self.path))["cache_id"], 5)

    def test_a_none_after_a_known_server_id_changes_nothing(self):
        c = nm.NameCache()
        c.set_server_cache_id(8)
        c.set_server_cache_id(None)
        self.assertEqual(c.server_cache_id, 8)

    def test_no_id_anywhere_saves_null(self):
        nm.NameCache(cache_path=self.path).save()
        self.assertIsNone(json.loads(_read(self.path))["cache_id"])

    def test_a_late_discard_keeps_what_this_session_learned(self):
        cache_file(self.path, cache_id=5)
        c = nm.NameCache(cache_path=self.path)
        c.load()
        c.on_creature_query_response({"entry": 9, "found": True, "name": "Fresh"}, save=False)
        c.on_gameobject_query_response({"entry": 3, "found": False}, save=False)
        c.set_server_cache_id(6)
        self.assertEqual(c.creatures, {9: {"entry": 9, "found": True, "name": "Fresh"}})
        self.assertEqual(c.gameobjects, {3: None})

    def test_one_info_line_with_both_ids_not_one_per_name(self):
        cache_file(self.path, cache_id=5, creatures={str(i): {"name": "n"} for i in range(50)})
        for order in ORDERS:
            with self.subTest(order):
                with self.assertLogs("agent.names", "INFO") as logs:
                    met(order, self.path, 6)
                self.assertEqual(len(logs.output), 1)
                self.assertRegex(logs.output[0], r"\b5\b")
                self.assertRegex(logs.output[0], r"\b6\b")

    def test_a_repeated_packet_does_not_discard_or_log_again(self):
        cache_file(self.path, cache_id=5)
        c = met("load first", self.path, 6)
        with self.assertNoLogs("agent.names", "INFO"):
            c.set_server_cache_id(6)

    def test_malformed_stored_id_counts_as_none(self):
        for bad in ("7", True, 1.5, [1]):
            with self.subTest(bad):
                cache_file(self.path, cache_id=bad)
                c = met("server first", self.path, 7)
                self.assertEqual(c.creatures, {1: {"name": "Old"}})

    def test_round_trip_and_the_old_loader_still_reads_the_file(self):
        c1 = nm.NameCache(cache_path=self.path)
        c1.set_server_cache_id(11)
        c1.creatures[1] = {"name": "A", "found": True}
        c1.save()
        self.assertEqual(json.loads(_read(self.path))["schema"], 2)   # CACHE_SCHEMA not bumped
        c2 = met("server first", self.path, 11)
        self.assertEqual(c2.creatures, c1.creatures)
        c3 = nm.NameCache()
        main_loader(c3, self.path)
        self.assertEqual(c3.creatures, c1.creatures)

    def test_the_discard_runs_under_the_world_state_lock_without_disk_io(self):
        cache_file(self.path, cache_id=5)
        ws = per.WorldState()
        ws.names.cache_path = self.path
        ws.names.load()
        held = []
        real = ws.names.set_server_cache_id

        def spy(cid):
            held.append(ws._lock.locked())
            return real(cid)

        ws.names.set_server_cache_id = spy
        with mock.patch("agent.persist.write_atomic") as write, mock.patch("agent.persist.set_aside") as aside:
            ws.apply_server_cache_id(6)
        self.assertEqual(held, [True])
        write.assert_not_called()
        aside.assert_not_called()
        self.assertEqual(ws.names.creatures, {})


if __name__ == "__main__":
    unittest.main()
