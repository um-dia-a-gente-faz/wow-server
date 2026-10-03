"""Unit tests for agent.http_api (UM-50): the read-only observability API.

Runs a real server on a loopback ephemeral port against a fake session
holding a real WorldState — no game server needed."""

import collections
import json
import logging
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

from agent import __main__ as main_mod
from agent import perception as per
from agent import update_object as uo
from agent.audit import AuditLogger
from agent.config import Config
from agent.http_api import AgentObserver, make_server
from agent.think import ThinkState

SECRET_PASSWORD = "pw-do-not-leak-7f3a"
SECRET_API_KEY = "sk-do-not-leak-91bc"


def _fake_session():
    ws = per.WorldState()
    ws.set_my_guid(0x10)
    ws.set_my_map(530)
    ws.update_object(uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=0x10, object_type=uo.TYPEID_PLAYER,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION,
                  "x": 10.0, "y": 20.0, "z": 30.0, "o": 0.0},
        fields={}))
    ws.update_object(uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=0x20, object_type=uo.TYPEID_UNIT,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION,
                  "x": 15.0, "y": 20.0, "z": 30.0, "o": 0.0},
        fields={}))
    return SimpleNamespace(
        world_state=ws, player_guid=0x10, player_name="Luaprata",
        player_position=(530, 10.0, 20.0, 30.0, 0.0), race=10, class_=2, coinage=123,
        spellbook={133, 168}, chat_inbox=collections.deque([{"kind": "say", "text": "hi"}]),
        pending_invite=None, corpse_position=None, events=collections.deque(maxlen=100),
        unexpected_disconnect=False)


class _ServerCase(unittest.TestCase):
    def setUp(self):
        self.observer = AgentObserver("Luaprata", goal="level up", model="test-model")
        self.session = _fake_session()
        self.think_state = ThinkState()
        self.observer.attach(self.session, self.think_state, lambda s: {})
        self.server = make_server(self.observer, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            self.assertEqual(r.headers.get_content_type(), "application/json")
            return r.status, r.read().decode("utf-8")

    def request(self, method, path):
        req = urllib.request.Request(self.base + path, method=method, data=b"{}" if method != "HEAD" else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, e.headers


class EndpointsTest(_ServerCase):
    def test_all_endpoints_return_json(self):
        for path in ("/healthz", "/state", "/perception", "/brain", "/"):
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIsInstance(json.loads(body), dict, path)

    def test_healthz(self):
        body = json.loads(self.get("/healthz")[1])
        self.assertEqual(body["agent"], "Luaprata")
        self.assertTrue(body["connected"])

    def test_perception_is_world_snapshot(self):
        body = json.loads(self.get("/perception")[1])
        self.assertEqual(body["position"], {"map": 530, "x": 10.0, "y": 20.0, "z": 30.0})
        # UM-89: snapshot encodes GUIDs as handles, not raw numbers
        self.assertEqual([u["guid"] for u in body["nearby_units"]], ["p1"])
        self.assertEqual(body["chat_inbox"], [{"kind": "say", "text": "hi"}])

    def test_state_has_stats_spellbook_quests_inventory(self):
        body = json.loads(self.get("/state")[1])
        self.assertEqual(body["self"]["name"], "Luaprata")
        self.assertEqual(body["self"]["money"], 123)
        self.assertEqual([s["id"] for s in body["spellbook"]], [133, 168])
        for key in ("quest_log", "equipment", "inventory"):
            self.assertIn(key, body)

    def test_brain_shows_goal_and_last_decisions(self):
        for i in range(1, 8):
            self.observer.record_decision({
                "cycle": i, "goal": "level up", "model": "m", "tool_call": {"name": "idle", "args": {}},
                "prompt_tokens": 100, "completion_tokens": 10, "snapshot": {"big": True}})
        self.think_state.record("move_to", {"x": 1}, True, "fp")
        body = json.loads(self.get("/brain?n=5")[1])
        self.assertEqual(body["goal"], "level up")
        self.assertEqual([d["cycle"] for d in body["decisions"]], [3, 4, 5, 6, 7])
        self.assertNotIn("snapshot", body["decisions"][0])
        self.assertEqual(body["tokens"]["prompt_total"], 700)
        self.assertEqual(body["tokens"]["completion_total"], 70)
        self.assertEqual(body["history"][0]["action"], "move_to")
        self.assertIsNone(body["brain"])

    def test_brain_reports_which_brain_decided_and_jev_confidence(self):
        # UM-101: the observer's configured brain until a decision says otherwise.
        self.observer.brain_name = "jev"
        self.assertEqual(json.loads(self.get("/brain")[1])["brain"], "jev")
        self.observer.record_decision({
            "cycle": 1, "model": "free-model", "brain": "llm", "confidence": None,
            "fallback": "jev call failed: HTTP 402", "tool_call": {"name": "loot", "args": {}}})
        self.observer.record_decision({
            "cycle": 2, "model": "typesafe/jev-1.13", "brain": "jev", "confidence": 0.77,
            "tool_call": {"name": "loot", "args": {}}})
        body = json.loads(self.get("/brain?n=5")[1])
        self.assertEqual((body["brain"], body["model"]), ("jev", "typesafe/jev-1.13"))
        self.assertEqual(body["decisions"][0]["fallback"], "jev call failed: HTTP 402")
        self.assertEqual(body["decisions"][1]["confidence"], 0.77)

    def test_detached_session_reports_disconnected(self):
        self.observer.detach()
        for path in ("/state", "/perception", "/brain"):
            self.assertFalse(json.loads(self.get(path)[1])["connected"], path)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/act", timeout=5)
        cm.exception.close()
        self.assertEqual(cm.exception.code, 404)


class ReadOnlyTest(_ServerCase):
    def test_non_get_methods_are_405(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            for path in ("/state", "/brain", "/perception", "/events", "/act"):
                status, headers = self.request(method, path)
                self.assertEqual(status, 405, f"{method} {path}")
                self.assertEqual(headers.get("Allow"), "GET")


class EventsStreamTest(_ServerCase):
    def test_streams_new_events_and_decisions(self):
        resp = urllib.request.urlopen(self.base + "/events", timeout=5)
        self.addCleanup(resp.close)
        self.assertEqual(resp.headers.get_content_type(), "text/event-stream")
        time.sleep(0.1)
        self.session.events.append({"kind": "death", "t": time.monotonic()})
        self.observer.record_decision({"cycle": 1, "tool_call": {"name": "idle", "args": {}}})
        seen = {}
        deadline = time.monotonic() + 5
        kind = None
        while len(seen) < 2 and time.monotonic() < deadline:
            line = resp.readline().decode("utf-8").rstrip("\n")
            if line.startswith("event: "):
                kind = line[len("event: "):]
            elif line.startswith("data: "):
                seen[kind] = json.loads(line[len("data: "):])
        self.assertEqual(seen["event"]["kind"], "death")
        self.assertEqual(seen["decision"]["cycle"], 1)


class NoSecretsTest(unittest.TestCase):
    """A Config carrying a real-looking password and API key must never
    leak either through any endpoint, even when a decision record or
    snapshot happens to contain a secret-named key."""

    def test_no_endpoint_leaks_credentials(self):
        env = {"WOW_ACCOUNT": "AGENT01", "WOW_PASSWORD": SECRET_PASSWORD,
               "LLM_API_KEY": SECRET_API_KEY, "LLM_MODEL": "m", "AGENT_NAME": "Luaprata",
               "AGENT_HTTP_PORT": "1", "AGENT_HTTP_BIND": "127.0.0.1"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = Config()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        audit = AuditLogger("Luaprata", base_dir=tmp.name)
        with socket.socket() as s:  # pick a free port
            s.bind(("127.0.0.1", 0))
            cfg.http_port = s.getsockname()[1]
        observer, server = main_mod._start_observer(cfg, audit, logging.getLogger("test"))
        self.assertIsNotNone(server)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sess = _fake_session()
        sess.config = cfg  # even a stray reference must not be walked into a response
        observer.attach(sess, ThinkState(), lambda s: {"api_key": SECRET_API_KEY})
        audit.record(cycle=1, snapshot={"password": SECRET_PASSWORD},
                     tool_call={"name": "say", "args": {"api_key": SECRET_API_KEY}},
                     valid=True, result={"ok": True, "error": None})
        base = f"http://127.0.0.1:{cfg.http_port}"
        for path in ("/healthz", "/state", "/perception", "/brain", "/"):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                body = r.read().decode("utf-8")
            self.assertNotIn(SECRET_PASSWORD, body, path)
            self.assertNotIn(SECRET_API_KEY, body, path)


class OffByDefaultTest(unittest.TestCase):
    def test_port_defaults_to_off_and_loopback(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = Config()
        self.assertEqual(cfg.http_port, 0)
        self.assertEqual(cfg.http_bind, "127.0.0.1")

    def test_start_observer_does_nothing_when_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = Config()
        with mock.patch.object(main_mod, "start_server") as start:
            self.assertEqual(main_mod._start_observer(cfg, None, logging.getLogger("t")), (None, None))
        start.assert_not_called()

    def test_bind_failure_is_not_fatal(self):
        with mock.patch.dict(os.environ, {"AGENT_HTTP_PORT": "9601"}, clear=True):
            cfg = Config()
        with mock.patch.object(main_mod, "start_server", side_effect=OSError("in use")):
            with self.assertLogs("t", level="ERROR"):
                self.assertEqual(main_mod._start_observer(cfg, None, logging.getLogger("t")), (None, None))


if __name__ == "__main__":
    unittest.main()
