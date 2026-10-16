"""#252: AGENT_STATE_DIR moves the names/items caches into a per-agent directory that
survives a container recreate, and the path is set before the names cache is loaded at login."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from agent import items as it
from agent import login
from agent import names as nm
from agent.config import Config


def _world_state():
    return SimpleNamespace(names=nm.NameCache(), items=it.ItemCache())


class StateDirSettingTests(unittest.TestCase):
    def test_read_into_cfg_state_dir(self):
        with mock.patch.dict("os.environ", {"AGENT_STATE_DIR": "/data/state/luaprata"}, clear=True):
            self.assertEqual(Config().state_dir, "/data/state/luaprata")

    def test_unset_is_empty(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Config().state_dir, "")


class ApplyStateDirTests(unittest.TestCase):
    def test_sets_both_cache_paths_under_the_state_dir(self):
        ws = _world_state()
        login.apply_state_dir(SimpleNamespace(state_dir="/x/luaprata"), ws)
        self.assertEqual(ws.names.cache_path, "/x/luaprata/names.json")
        self.assertEqual(ws.items.cache_path, "/x/luaprata/items.json")

    def test_empty_leaves_the_defaults(self):
        ws = _world_state()
        login.apply_state_dir(SimpleNamespace(state_dir=""), ws)
        self.assertEqual(ws.names.cache_path, nm.DEFAULT_CACHE_PATH)
        self.assertEqual(ws.items.cache_path, it.DEFAULT_CACHE_PATH)

    def test_cfg_without_the_field_leaves_the_defaults(self):
        ws = _world_state()
        login.apply_state_dir(SimpleNamespace(), ws)
        self.assertEqual(ws.names.cache_path, nm.DEFAULT_CACHE_PATH)


class SurvivesRestartTests(unittest.TestCase):
    def test_names_saved_by_one_instance_load_in_a_second(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state", "luaprata")   # does not exist yet
            first = nm.NameCache(cache_path=os.path.join(state, "names.json"))
            first.on_creature_query_response({"entry": 1234, "found": True, "name": "Wolf", "type": 1})
            self.assertTrue(os.path.isfile(os.path.join(state, "names.json")))

            second = nm.NameCache(cache_path=os.path.join(state, "names.json"))
            second.load()
            self.assertEqual(second.creatures[1234]["name"], "Wolf")


class LoadOrderTests(unittest.TestCase):
    """connect_and_login: the state dir is applied before the session's names.load() at login."""

    class FakeSession:
        def __init__(self, world_state):
            self.world_state = world_state
            self.chat_relay = None

        def login_character(self, guid):
            # Mirrors WoWSession.login_character, which loads the names cache at login (session.py).
            self.world_state.names.load()
            self.player_guid = guid
            self.player_position = (0, 0, 0)

        def join_channels(self, names):
            return {}

    def test_path_is_set_before_names_load(self):
        seen = []
        real_load = nm.NameCache.load

        def recording_load(self_, path=None):
            seen.append(self_.cache_path)
            return real_load(self_, path)

        with tempfile.TemporaryDirectory() as tmp:
            ws = _world_state()
            sess = self.FakeSession(ws)
            char = {"guid": 7, "name": "Luaprata", "race": 1, "class_": 1}
            env = {"WOW_ACCOUNT": "a", "WOW_PASSWORD": "p", "WOW_CHARACTER": "Luaprata",
                   "AGENT_STATE_DIR": tmp}
            with mock.patch.dict("os.environ", env, clear=True), \
                    mock.patch.object(login, "authenticate_and_login", return_value=(sess, [char])), \
                    mock.patch.object(nm.NameCache, "load", recording_load):
                login.connect_and_login(Config(), mock.Mock())
        self.assertEqual(seen, [os.path.join(tmp, "names.json")])

    def test_activity_events_flag_reaches_the_session_from_cfg(self):
        # ADR 0020 (#390): AGENT_ACTIVITY_EVENTS, read by Config, is what the session buffers with.
        for value, expected in (("1", True), ("0", False)):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                sess = self.FakeSession(_world_state())
                sess.activity_events = None  # a marker: connect_and_login must set the flag itself
                char = {"guid": 7, "name": "Luaprata", "race": 1, "class_": 1}
                env = {"WOW_ACCOUNT": "a", "WOW_PASSWORD": "p", "WOW_CHARACTER": "Luaprata",
                       "AGENT_STATE_DIR": tmp, "AGENT_ACTIVITY_EVENTS": value}
                with mock.patch.dict("os.environ", env, clear=True), \
                        mock.patch.object(login, "authenticate_and_login", return_value=(sess, [char])):
                    login.connect_and_login(Config(), mock.Mock())
                self.assertIs(sess.activity_events, expected)


if __name__ == "__main__":
    unittest.main()
