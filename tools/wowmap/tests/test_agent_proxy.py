"""UM-50: wowmap's read-only proxy to the agents' observability APIs."""
import json
import pathlib
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402
import agents  # noqa: E402


class FakeAgent(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        FakeAgent.seen.append(self.path)
        body = json.dumps({"path": self.path, "goal": "level up"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class ParseTests(unittest.TestCase):
    def test_parses_names_and_skips_garbage(self):
        got = agents.parse_agent_urls(" Luaprata=http://h:9601/ ,bad, x=ftp://h, =http://h ,Far=https://h:9602")
        self.assertEqual(got, {"luaprata": ("Luaprata", "http://h:9601"),
                               "far": ("Far", "https://h:9602")})

    def test_empty(self):
        self.assertEqual(agents.parse_agent_urls(""), {})


class ProxyTests(unittest.TestCase):
    def setUp(self):
        FakeAgent.seen = []
        self.agent = _serve(FakeAgent)
        self.addCleanup(self.agent.server_close)
        self.addCleanup(self.agent.shutdown)
        agent_url = f"http://127.0.0.1:{self.agent.server_address[1]}"
        self.agent_url = agent_url
        p = mock.patch.object(agents, "AGENT_APIS", {
            "luaprata": ("Luaprata", agent_url),
            "gone": ("Gone", "http://127.0.0.1:1"),
        })
        p.start()
        self.addCleanup(p.stop)
        self.wowmap = _serve(app.Handler)
        self.addCleanup(self.wowmap.server_close)
        self.addCleanup(self.wowmap.shutdown)
        self.base = f"http://127.0.0.1:{self.wowmap.server_address[1]}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode()

    def test_lists_agent_names_without_urls(self):
        status, body = self.get("/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"agents": ["Gone", "Luaprata"]})
        self.assertNotIn("127.0.0.1", body)

    def test_proxies_known_views_case_insensitively(self):
        for view in agents.AGENT_VIEWS:
            status, body = self.get(f"/api/agent/luaprata/{view}")
            self.assertEqual(status, 200, view)
            self.assertEqual(json.loads(body)["path"], f"/{view}")

    def test_brain_forwards_only_numeric_n(self):
        self.get("/api/agent/Luaprata/brain?n=5")
        self.get("/api/agent/Luaprata/brain?n=5;x")
        self.assertEqual(FakeAgent.seen, ["/brain?n=5", "/brain"])

    def test_unknown_agent_or_view_is_404_and_not_forwarded(self):
        for path in ("/api/agent/Nobody/brain", "/api/agent/Luaprata/events",
                     "/api/agent/Luaprata/../state", "/api/agent/Luaprata/"):
            self.assertEqual(self.get(path)[0], 404, path)
        self.assertEqual(FakeAgent.seen, [])

    def test_unreachable_agent_is_502_without_leaking_url(self):
        status, body = self.get("/api/agent/Gone/brain")
        self.assertEqual(status, 502)
        self.assertNotIn("127.0.0.1", body)


class PageTests(unittest.TestCase):
    def test_agent_mind_is_spliced_in(self):
        self.assertNotIn("@agent-", app.PAGE)
        self.assertIn("const AgentMind", app.PAGE)
        self.assertIn("Agent mind", app.PAGE)


if __name__ == "__main__":
    unittest.main()
