"""#137: the fleet panel's proxy to the agent runner, against a fake runner (#136 contract)."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402
import agents  # noqa: E402
import fleet  # noqa: E402

TOKEN = "test-token-1234"

RUNNING = {"name": "Luaprata", "account": "AGENT01", "service": "agent-luaprata",
           "container": {"state": "running", "status": "running", "exit_code": None, "since": "2026-10-03T08:00:00Z"},
           "agent_api": {"state": "ok", "connected": True},
           "audit": {"last_ts": "2026-10-03T19:07:12Z", "age_seconds": 4},
           "brain": {"model": "auto", "valid": True, "last_error": None, "brain": "llm", "cycle": 9},
           "character": {"level": 12, "playtime_seconds": 3600, "zone": "Elwynn Forest", "source": "wowmap"}}
BROKEN = {"name": "Spellweaver", "account": "AGENT05", "service": "agent-spellweaver",
          "container": {"state": "running", "status": "running", "exit_code": None, "since": "2026-10-03T08:00:00Z"},
          "agent_api": {"state": "ok", "connected": True},
          "audit": {"last_ts": "2026-10-03T19:00:00Z", "age_seconds": 900},
          "brain": {"model": "auto", "valid": False, "last_error": "gateway 503 no_providers_configured",
                    "brain": "llm", "cycle": 3},
          "character": {"level": 2, "playtime_seconds": 90, "zone": "Eversong Woods", "source": "wowmap"}}
DEAD = {"name": "Farstrider", "account": "AGENT02", "service": "agent-farstrider",
        "container": {"state": "exited", "status": "exited", "exit_code": 137, "since": "2026-09-19T08:30:00Z"},
        "agent_api": {"state": "unreachable"}, "audit": {"last_ts": None, "age_seconds": None},
        "brain": None, "character": {"level": None, "playtime_seconds": None, "zone": None, "error": "not found"}}


class FakeRunner(BaseHTTPRequestHandler):
    """Scripted runner. `mode` picks the behaviour; `seen` records (method, path, auth)."""
    mode = "ok"
    seen = []

    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        FakeRunner.seen.append((self.command, self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, {"error": "token required"})
        if FakeRunner.mode == "garbage":
            data = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if self.command == "GET" and self.path == "/agents":
            if FakeRunner.mode == "500":
                return self._send(500, {"error": "docker exploded"})
            return self._send(200, {"agents": [RUNNING, BROKEN, DEAD],
                                    "image": {"name": "wow-agent", "stale": True, "present": True},
                                    "generated_at": "2026-10-03T19:07:16Z"})
        if self.command == "POST" and self.path.startswith("/agents/"):
            _, _, name, action = self.path.split("/")
            if name == "Nobody":
                return self._send(404, {"error": "no such agent: Nobody", "detail": None})
            if FakeRunner.mode == "fail":
                # a runner that leaks the token into docker's stderr: wowmap must scrub it
                return self._send(502, {"error": "docker compose up failed (exit 1)",
                                        "detail": f"exited 1: no space left on device ({TOKEN})"})
            state = "running" if action == "start" else "exited"
            return self._send(200, {"action": action, "built": action == "start",
                                    "agent": {**DEAD, "name": name, "container": {"state": state, "exit_code": 0}}})
        return self._send(404, {"error": "not found"})

    do_GET = do_POST = _handle  # noqa: N815


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class FleetBase(unittest.TestCase):
    token = TOKEN

    def setUp(self):
        FakeRunner.seen, FakeRunner.mode = [], "ok"
        self.runner = _serve(FakeRunner)
        self.addCleanup(self.runner.server_close)
        self.addCleanup(self.runner.shutdown)
        self.runner_url = f"http://127.0.0.1:{self.runner.server_address[1]}"
        p = mock.patch.object(agents, "RUNNER", fleet.Runner(self.runner_url, self.token))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(agents, "AGENT_APIS", {})
        p.start()
        self.addCleanup(p.stop)
        self.wowmap = _serve(app.Handler)
        self.addCleanup(self.wowmap.server_close)
        self.addCleanup(self.wowmap.shutdown)
        self.base = f"http://127.0.0.1:{self.wowmap.server_address[1]}"

    def call(self, path, method="GET", headers=None):
        req = urllib.request.Request(self.base + path, method=method, headers=headers or {},
                                     data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode()

    def post(self, path, headers=None):
        return self.call(path, "POST", {"X-Fleet-Action": "1"} if headers is None else headers)


class StatusTests(FleetBase):
    def test_lists_the_runner_agents_with_every_signal_separate(self):
        status, body = self.call("/api/fleet")
        self.assertEqual(status, 200)
        j = json.loads(body)
        self.assertEqual((j["configured"], j["controls"], j["runner"]), (True, True, "ok"))
        self.assertEqual([a["name"] for a in j["agents"]], ["Luaprata", "Spellweaver", "Farstrider"])
        self.assertEqual(j["agents"][2]["container"]["exit_code"], 137)
        self.assertEqual(j["agents"][1]["brain"]["last_error"], "gateway 503 no_providers_configured")
        self.assertEqual(j["image"]["stale"], True)
        self.assertEqual(j["generated_at"], "2026-10-03T19:07:16Z")

    def test_sends_the_bearer_token_to_the_runner_only(self):
        status, body = self.call("/api/fleet")
        self.assertEqual(FakeRunner.seen, [("GET", "/agents", f"Bearer {TOKEN}")])
        self.assertNotIn(TOKEN, body)

    def test_token_and_runner_url_never_reach_the_browser(self):
        for path in ("/api/fleet", "/"):
            status, body = self.call(path)
            self.assertNotIn(TOKEN, body)
            self.assertNotIn(self.runner_url, body)
            self.assertNotIn("127.0.0.1:" + str(self.runner.server_address[1]), body)
        # not in the page source either, even though the token env var is named in README only
        self.assertNotIn("AGENT_RUNNER_TOKEN", app.PAGE)

    def test_unreachable_runner_is_unknown_not_healthy(self):
        with mock.patch.object(agents, "RUNNER", fleet.Runner("http://127.0.0.1:1", TOKEN)):
            status, body = self.call("/api/fleet")
        j = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual((j["runner"], j["agents"]), ("unreachable", []))
        self.assertTrue(j["error"])
        self.assertNotIn("127.0.0.1:1", body)

    def test_a_dead_runner_still_names_the_agents_it_last_listed(self):
        runner = agents.RUNNER
        runner.status()
        self.runner.shutdown()
        self.runner.server_close()
        j = json.loads(self.call("/api/fleet")[1])
        self.assertEqual(j["runner"], "unreachable")
        self.assertEqual([a["name"] for a in j["agents"]], ["Luaprata", "Spellweaver", "Farstrider"])
        # names only: the old signals must not be shown as current
        self.assertEqual(set(j["agents"][0]), {"name", "account"})

    def test_wrong_token_is_reported_as_unauthorized(self):
        with mock.patch.object(agents, "RUNNER", fleet.Runner(self.runner_url, "wrong-token-0000")):
            j = json.loads(self.call("/api/fleet")[1])
        self.assertEqual((j["runner"], j["agents"]), ("unauthorized", []))
        self.assertIn("token", j["error"])

    def test_missing_token_is_reported_as_unauthorized(self):
        with mock.patch.object(agents, "RUNNER", fleet.Runner(self.runner_url, "")):
            j = json.loads(self.call("/api/fleet")[1])
        self.assertEqual(j["runner"], "unauthorized")

    def test_runner_error_and_garbage_are_states_not_500s(self):
        FakeRunner.mode = "500"
        j = json.loads(self.call("/api/fleet")[1])
        self.assertEqual((j["runner"], j["error"]), ("error", "docker exploded"))
        FakeRunner.mode = "garbage"
        status, body = self.call("/api/fleet")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["runner"], "unreachable")


class ActionTests(FleetBase):
    def test_start_and_stop_forward_with_the_token_and_return_the_result(self):
        for action, state in (("start", "running"), ("stop", "exited")):
            status, body = self.post(f"/api/fleet/agents/Luaprata/{action}")
            j = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual((j["action"], j["agent"]["container"]["state"]), (action, state))
        self.assertEqual(FakeRunner.seen, [("POST", "/agents/Luaprata/start", f"Bearer {TOKEN}"),
                                           ("POST", "/agents/Luaprata/stop", f"Bearer {TOKEN}")])

    def test_runner_failure_text_passes_through_verbatim_but_scrubbed(self):
        FakeRunner.mode = "fail"
        status, body = self.post("/api/fleet/agents/Luaprata/start")
        j = json.loads(body)
        self.assertEqual(status, 502)
        self.assertEqual(j["error"], "docker compose up failed (exit 1)")
        self.assertIn("exited 1: no space left on device", j["detail"])
        self.assertNotIn(TOKEN, body)

    def test_runner_404_for_an_unknown_agent_passes_through(self):
        status, body = self.post("/api/fleet/agents/Nobody/start")
        self.assertEqual((status, json.loads(body)["error"]), (404, "no such agent: Nobody"))

    def test_runner_rejecting_the_token_is_a_wowmap_problem_not_a_401(self):
        with mock.patch.object(agents, "RUNNER", fleet.Runner(self.runner_url, "wrong-token-0000")):
            status, body = self.post("/api/fleet/agents/Luaprata/stop")
        self.assertEqual(status, 502)
        self.assertIn("token", json.loads(body)["error"])

    def test_unreachable_runner_says_the_action_may_have_run(self):
        with mock.patch.object(agents, "RUNNER", fleet.Runner("http://127.0.0.1:1", TOKEN)):
            status, body = self.post("/api/fleet/agents/Luaprata/stop")
        self.assertEqual(status, 502)
        self.assertIn("may or may not", json.loads(body)["error"])

    def test_cross_site_posts_are_refused(self):
        status, _ = self.post("/api/fleet/agents/Luaprata/stop", headers={})
        self.assertEqual(status, 403)
        self.assertEqual(FakeRunner.seen, [])

    def test_only_start_and_stop_and_sane_names(self):
        for path in ("/api/fleet/agents/Luaprata/delete", "/api/fleet/agents/Luaprata",
                     "/api/fleet/agents//start", "/api/fleet/agents/" + "x" * 40 + "/start"):
            self.assertEqual(self.post(path)[0], 404, path)
        # an encoded slash must not be able to reach another runner path
        self.assertEqual(self.post("/api/fleet/agents/..%2Fhealthz/start")[0], 404)
        self.assertEqual(FakeRunner.seen, [])


class NoRunnerTests(FleetBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(agents, "RUNNER", fleet.Runner("", ""))
        p.start()
        self.addCleanup(p.stop)

    def test_read_only_and_says_why(self):
        j = json.loads(self.call("/api/fleet")[1])
        self.assertEqual((j["configured"], j["controls"], j["runner"]), (False, False, "disabled"))
        self.assertIn("AGENT_RUNNER_URL", j["error"])
        self.assertEqual(j["agents"], [])

    def test_actions_are_refused(self):
        status, body = self.post("/api/fleet/agents/Luaprata/start")
        self.assertEqual(status, 503)
        self.assertIn("read-only", json.loads(body)["error"])
        self.assertEqual(FakeRunner.seen, [])

    def test_rows_come_from_the_agent_apis_with_only_that_signal(self):
        class Health(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                data = json.dumps({"ok": True, "connected": False}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        up = _serve(Health)
        self.addCleanup(up.server_close)
        self.addCleanup(up.shutdown)
        apis = {"luaprata": ("Luaprata", f"http://127.0.0.1:{up.server_address[1]}"),
                "gone": ("Gone", "http://127.0.0.1:1")}
        with mock.patch.object(agents, "AGENT_APIS", apis):
            j = json.loads(self.call("/api/fleet")[1])
        self.assertEqual(j["runner"], "disabled")
        by = {a["name"]: a for a in j["agents"]}
        self.assertEqual(by["Luaprata"], {"name": "Luaprata", "agent_api": {"state": "ok", "connected": False}})
        self.assertEqual(by["Gone"]["agent_api"]["state"], "unreachable")
        self.assertNotIn("container", by["Luaprata"])


class PageTests(unittest.TestCase):
    def test_page_has_the_panel_and_no_innerhtml_for_runner_text(self):
        self.assertIn('id="fleet"', app.PAGE)
        self.assertIn('id="tglFleet"', app.PAGE)
        self.assertNotIn("@fleet-", app.PAGE)
        self.assertNotIn("innerHTML", fleet.FLEET_JS)
        self.assertNotIn("innerHTML", fleet.FLEET_HTML)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_fleet_script_parses(self):
        script = fleet.FLEET_JS.replace("<script>", "").replace("</script>", "")
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "fleet.js"
            f.write_text(script)
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
