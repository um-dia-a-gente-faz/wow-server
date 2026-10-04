"""#174: the Agent mind tab asks the server whether a character is an agent or a human."""
import json
import pathlib
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402


class FakeConnection:
    """characters JOIN auth.account -> (name, username) or nothing."""
    def __init__(self, row):
        self.row, self.args = row, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql, args):
        self.args = args

    def fetchone(self):
        return self.row


def kind(row, apis=None):
    with mock.patch.object(app, "db", return_value=FakeConnection(row)), \
            mock.patch.object(app, "AGENT_APIS", apis or {}):
        return app.character_kind("whoever")


class AgentAccountTests(unittest.TestCase):
    def test_agent_accounts(self):
        for name in ("AGENT01", "AGENT25", "agent07"):
            self.assertTrue(app.is_agent_account(name), name)

    def test_other_accounts(self):
        for name in ("RUBENS", "AGENT", "AGENTX1", "MYAGENT01", "", None,
                     "AGENT00", "AGENT26", "AGENT99", "AGENT1", "AGENT001"):
            self.assertFalse(app.is_agent_account(name), name)


class CharacterKindTests(unittest.TestCase):
    def test_agent_with_api(self):
        apis = {"luaprata": ("Luaprata", "http://h:9601")}
        self.assertEqual(kind(("Luaprata", "AGENT01"), apis),
                         {"name": "Luaprata", "kind": "agent", "account": "AGENT01",
                          "agent_api": True, "fleet_configured": True})

    def test_agent_without_api_on_this_page(self):
        apis = {"far": ("Far", "http://h:9602")}
        got = kind(("Luaprata", "AGENT01"), apis)
        self.assertEqual((got["kind"], got["agent_api"], got["fleet_configured"]),
                         ("agent", False, True))

    def test_no_fleet_configured(self):
        got = kind(("Luaprata", "AGENT01"))
        self.assertEqual((got["kind"], got["agent_api"], got["fleet_configured"]),
                         ("agent", False, False))

    def test_human_hides_the_login_name(self):
        got = kind(("Rubens", "RUBENS"))
        self.assertEqual(got["kind"], "human")
        self.assertNotIn("account", got)
        self.assertNotIn("RUBENS", repr(got))

    def test_out_of_roster_agent_name_is_human(self):
        got = kind(("Imposter", "AGENT26"))
        self.assertEqual(got["kind"], "human")
        self.assertNotIn("AGENT26", repr(got))

    def test_character_with_no_account_row_is_human(self):
        self.assertEqual(kind(("Orphan", None))["kind"], "human")

    def test_unknown_character(self):
        self.assertIsNone(kind(None))


class KindRouteTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/api/character/"

    def get(self, row, name):
        with mock.patch.object(app, "db", return_value=FakeConnection(row)), \
                mock.patch.object(app, "AGENT_APIS", {}):
            try:
                with urllib.request.urlopen(self.base + name + "/kind") as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

    def test_human(self):
        self.assertEqual(self.get(("Rubens", "RUBENS"), "Rubens"),
                         (200, {"name": "Rubens", "kind": "human"}))

    def test_unknown_is_404(self):
        self.assertEqual(self.get(None, "Nobody")[0], 404)


class MindTabPageTests(unittest.TestCase):
    """The tab names each state in words instead of leaving the panel blank."""

    def test_states_are_spelled_out(self):
        for text in ("Human player — no agent brain attached", "brain API unreachable",
                     "not configured on this page", "/kind"):
            self.assertIn(text, app.AGENT_JS)

    def test_no_silent_catch(self):
        self.assertNotIn("catch (e) { /*", app.AGENT_JS)


if __name__ == "__main__":
    unittest.main()
