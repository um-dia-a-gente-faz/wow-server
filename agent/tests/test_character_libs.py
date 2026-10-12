"""agents/character_rules.py and agents/realm.py (#391): what scripts/create_agent_roster.py
and tools/agent-runner share. No real network: the console test talks to a local HTTP server.

Runs from the repo root (`discover -t . -s agent/tests`), so `agents` imports as a namespace package."""
import ast
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from agents import character_rules, realm, roster

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import create_agent_roster as car  # noqa: E402


class FakeConsoleServer(ThreadingHTTPServer):
    """Records (method, has sid, body) per request and answers like engine.io polling."""

    def __init__(self):
        self.seen = []
        super().__init__(("127.0.0.1", 0), _ConsoleHandler)


class _ConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self, body):
        query = parse_qs(urlsplit(self.path).query)
        self.server.seen.append((self.command, urlsplit(self.path).path, query.get("EIO"),
                                 query.get("transport"), query.get("sid"), body))
        data = b'0{"sid":"abc123","upgrades":[]}' if "sid" not in query else b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._answer(None)

    def do_POST(self):
        self._answer(self.rfile.read(int(self.headers["Content-Length"])).decode())


class SharedDefinitionTests(unittest.TestCase):
    def test_the_script_uses_the_library_objects(self):
        for name in ("HORDE", "CLASS_NAMES", "valid_name", "gen_name"):
            self.assertIs(getattr(car, name), getattr(character_rules, name), name)
        self.assertIs(car.Realm, realm.Realm)
        self.assertIs(car.Console, realm.Console)

    def test_the_script_defines_no_copy(self):
        tree = ast.parse((REPO / "scripts" / "create_agent_roster.py").read_text())
        defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        defined |= {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets
                    if isinstance(t, ast.Name)}
        self.assertEqual(defined & {"HORDE", "CLASS_NAMES", "_SYLLABLES", "valid_name", "gen_name", "Realm",
                                    "Console", "account_name", "FIRST_PORT", "PARTY_SIZE"}, set())

    def test_account_name_matches_the_committed_roster(self):
        self.assertEqual([roster.account_name(a.index) for a in roster.load_roster()],
                         [a.account for a in roster.load_roster()])
        self.assertEqual(roster.account_name(7), "AGENT07")

    def test_the_script_reads_the_roster_through_the_library(self):
        self.assertEqual(car.load_roster(REPO / "agents" / "roster.json"),
                         [a.to_json() for a in roster.load_roster()])
        self.assertEqual(car.load_roster(REPO / "agents" / "no-such-file.json"), [])

    def test_character_rules_is_pure(self):
        tree = ast.parse((REPO / "agents" / "character_rules.py").read_text())
        imports = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, ["import random"])


class RealmModuleTests(unittest.TestCase):
    def test_importing_realm_imports_no_script_and_no_agent_module(self):
        tree = ast.parse((REPO / "agents" / "realm.py").read_text())
        top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(top, ["import json", "import os", "import time", "import urllib.request"])
        self.assertNotIn("wow_console", (REPO / "agents" / "realm.py").read_text().replace(
            "scripts/wow_console.py", ""))

    def test_console_sends_the_account_create_command(self):
        server = FakeConsoleServer()
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch.dict(os.environ, {"WOW_PORT": str(server.server_address[1])}), \
                mock.patch.object(realm.time, "sleep") as sleep:
            realm.Console("127.0.0.1").create_account("AGENT07", "pw-not-logged")
        self.assertEqual([c.args for c in sleep.call_args_list], [(0.5,), (2.0,)])
        self.assertEqual(server.seen, [
            ("GET", "/socket.io/", ["4"], ["polling"], None, None),
            ("POST", "/socket.io/", ["4"], ["polling"], ["abc123"], "40"),
            ("GET", "/socket.io/", ["4"], ["polling"], ["abc123"], None),
            ("POST", "/socket.io/", ["4"], ["polling"], ["abc123"],
             "42" + json.dumps(["worldserver_input", "account create AGENT07 pw-not-logged"])),
        ])

    def test_realm_open_returns_none_when_the_account_is_rejected(self):
        from agent import auth
        with mock.patch.object(auth, "auth_logon", side_effect=auth.AuthRejected("logon proof: unknown account", 4)):
            self.assertIsNone(realm.Realm("127.0.0.1", 3724).open("AGENT07", "pw"))

    def test_realm_open_connects_a_session_to_the_first_realm(self):
        from agent import auth, session
        made = []

        class Session:
            def __init__(self, *args):
                made.append(args)
                self.connected = False

            def connect(self):
                self.connected = True

        realms = {1: {"address": "10.0.0.9:8085", "id": 1}}
        with mock.patch.object(auth, "auth_logon", return_value=("AGENT07", b"key", realms)) as logon, \
                mock.patch.object(session, "WoWSession", Session):
            sess = realm.Realm("127.0.0.1", 3724).open("AGENT07", "pw")
        logon.assert_called_once_with("127.0.0.1", 3724, "AGENT07", "pw")
        self.assertEqual(made, [("10.0.0.9", 8085, "AGENT07", b"key", 1)])
        self.assertTrue(sess.connected)


if __name__ == "__main__":
    unittest.main()
