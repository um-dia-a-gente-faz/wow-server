"""Tests for tools/agent-runner (#136). No real containers: `docker` is a fake
script on PATH, the agents' APIs and wowmap are tiny local HTTP servers, and
the checkout is a temporary git repo, so nothing here touches the real host."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(REPO))
import runner  # noqa: E402
from agent import api_contract  # noqa: E402  (#264: fixtures come from the shared agent API schema)

_EP = api_contract.load()["endpoints"]

TOKEN = "t0ken-for-tests"
PASSWORD = "hunter2-agent-password"

FAKE_DOCKER = """\
#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DOCKER_DIR"]
state = json.load(open(d + "/state.json"))
a = sys.argv[1:]
open(d + "/calls.log", "a").write(json.dumps(a) + "\\n")
if state.get("docker_down"):
    sys.stderr.write("Cannot connect to the Docker daemon at unix:///var/run/docker.sock\\n")
    sys.exit(1)
if a[0] == "inspect":
    found = [dict(c, Name="/" + n) for n, c in state["containers"].items() if n in a[1:]]
    print(json.dumps(found))
    if len(found) < len(a) - 1:
        sys.stderr.write(state.get("missing_msg", "Error: No such object: missing") + "\\n")
        sys.exit(1)
elif a[:2] == ["image", "inspect"]:
    if state.get("image_created") is None:
        sys.stderr.write("Error: No such image\\n")
        sys.exit(1)
    print(state["image_created"])
elif a[0] == "compose":
    if state.get("compose_fail"):
        sys.stderr.write(state["compose_fail"])
        sys.exit(1)
"""

ROSTER = {"agents": [{"account": "AGENT01", "character": "Alpha"},
                     {"account": "AGENT02", "character": "Bravo"},
                     {"account": "AGENT03", "character": "Charlie"}]}


class FakeHTTP(ThreadingHTTPServer):
    """Serves canned JSON by path; unknown paths 404."""

    def __init__(self, routes, post=None):
        outer = routes
        self.posts = []  # (path, Authorization, parsed body) of every POST received
        server = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
                server.posts.append((self.path, self.headers.get("Authorization"), body))
                status, obj = post(self.path, body) if post else (405, {"error": "read-only"})
                data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                body = outer.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        super().__init__(("127.0.0.1", 0), H)
        threading.Thread(target=self.serve_forever, args=(0.01,), daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]


def container(status, exit_code=0, started="2026-10-03T10:00:00.123456789Z",
              finished="0001-01-01T00:00:00Z"):
    return {"State": {"Status": status, "ExitCode": exit_code, "StartedAt": started,
                      "FinishedAt": finished}}


def write_audit(audit_dir, name, day, records):
    d = Path(audit_dir) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin, self.fake, self.runtime = root / "bin", root / "fake", root / "runtime"
        self.repo, self.audit = root / "checkout", root / "audit"
        for d in (self.bin, self.fake, self.repo / "agents", self.repo / "agent"):
            d.mkdir(parents=True)
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
        (self.repo / "agents" / "roster.json").write_text(json.dumps(ROSTER))
        (self.repo / "Dockerfile").write_text("FROM scratch\n")
        (self.repo / "agent" / "a.py").write_text("x = 1\n")
        env = dict(os.environ, GIT_AUTHOR_DATE="2024-06-01T00:00:00Z",
                   GIT_COMMITTER_DATE="2024-06-01T00:00:00Z")
        for cmd in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, env=env)

        patch_env = mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DOCKER_DIR": str(self.fake), "AGENT_PASSWORD": PASSWORD})
        patch_env.start()
        self.addCleanup(patch_env.stop)

        self.agent_api = FakeHTTP({
            "/healthz": api_contract.sample(_EP["/healthz"]["responses"]["ok"], agent="Alpha"),
            "/state": api_contract.sample(_EP["/state"]["responses"]["in game"], **{
                "self": {"level": 4, "position": {"map": 530, "x": 1.0, "y": 2.0, "z": 3.0}}})})
        self.wowmap = FakeHTTP({
            "/api/character/Alpha": {"level": 4, "totaltime": 1984, "zone_name": "Sunstrider Isle"}})
        for s in (self.agent_api, self.wowmap):
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)

        self.set_state(containers={"wow-agent-alpha": container("running"),
                                   "wow-agent-charlie": container(
                                       "exited", 137, finished="2026-09-19T08:30:00.5Z")},
                       image_created="2025-01-01T00:00:00Z")
        write_audit(self.audit, "Alpha", "2026-10-02", [{"ts": 1, "model": "old", "valid": True,
                                                         "result": {"ok": True, "error": None}}])
        self.last_ts = time.time() - 42
        write_audit(self.audit, "Alpha", "2026-10-03", [
            {"ts": self.last_ts - 10, "model": "auto", "valid": True, "result": {"ok": True}},
            {"ts": self.last_ts, "model": "auto", "valid": False, "brain": "llm", "cycle": 9,
             "result": {"ok": False, "error": "gateway 503 no_providers_configured"}}])

        self.config = runner.Config({
            "AGENT_RUNNER_TOKEN": TOKEN, "AGENT_RUNNER_PORT": "0", "AGENT_RUNNER_REPO": str(self.repo),
            "AGENT_RUNNER_RUNTIME_DIR": str(self.runtime), "AGENT_RUNNER_AUDIT_DIR": str(self.audit),
            "AGENT_RUNNER_CHARACTER_URL": f"http://127.0.0.1:{self.wowmap.port}"})
        gen = runner.load_generator(REPO)
        patcher = mock.patch.object(gen, "FIRST_PORT", self.agent_api.port)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fleet = runner.Fleet(self.config, gen=gen)
        self.server = runner.make_server(self.config, self.fleet)
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def set_state(self, **state):
        (self.fake / "state.json").write_text(json.dumps(state))

    def calls(self):
        path = self.fake / "calls.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def compose_calls(self):
        return [c for c in self.calls() if c[0] == "compose"]

    def req(self, path, method="GET", token=TOKEN):
        r = urllib.request.Request(self.base + path, method=method,
                                   data=b"" if method == "POST" else None)
        if token is not None:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())


class AuthTest(RunnerCase):
    def test_healthz_needs_no_token(self):
        self.assertEqual(self.req("/healthz", token=None), (200, {"ok": True}))

    def test_missing_wrong_and_correct_token(self):
        for path, method in (("/agents", "GET"), ("/agents/Alpha", "GET"),
                             ("/agents/Alpha/start", "POST"), ("/agents/Alpha/stop", "POST")):
            self.assertEqual(self.req(path, method, token=None)[0], 401, path)
            self.assertEqual(self.req(path, method, token="wrong")[0], 401, path)
            self.assertEqual(self.req(path, method, token=TOKEN[:-1])[0], 401, path)
        self.assertEqual(self.compose_calls(), [], "a refused write must not reach docker")
        self.assertEqual(self.req("/agents")[0], 200)

    def test_refused_write_is_logged_with_caller(self):
        with self.assertLogs("agent-runner", "INFO") as cm:
            self.req("/agents/Alpha/stop", "POST", token="wrong")
        line = json.loads(cm.records[0].getMessage())
        self.assertEqual((line["action"], line["agent"], line["caller"], line["result"]),
                         ("stop", "Alpha", "127.0.0.1", "401"))


class StatusTest(RunnerCase):
    def agents(self):
        status, body = self.req("/agents")
        self.assertEqual(status, 200)
        return {a["name"]: a for a in body["agents"]}

    def test_signals_are_kept_separate(self):
        a = self.agents()["Alpha"]
        self.assertEqual(a["container"]["state"], "running")
        self.assertEqual(a["container"]["since"], "2026-10-03T10:00:00Z")
        self.assertEqual(a["agent_api"]["state"], "ok")
        self.assertEqual(a["agent_api"]["position"]["map"], 530)
        self.assertEqual(a["character"]["level"], 4)
        self.assertEqual(a["character"]["playtime_seconds"], 1984)
        self.assertEqual(a["character"]["zone"], "Sunstrider Isle")
        self.assertIn("wowmap", a["character"]["source"])

    def test_agent_api_version_skew(self):
        self.assertIn(api_contract.load()["api_version"], runner.SUPPORTED_API_VERSIONS)
        port = self.agent_api.port
        future = dict(api_contract.sample(_EP["/healthz"]["responses"]["ok"]), api_version=99, new_field=1)
        with mock.patch.object(runner.Fleet, "_get_json", return_value=future):
            got = self.fleet.agent_api(port)
        self.assertEqual(got["state"], "unsupported")
        self.assertIn("version 99", got["error"])
        legacy = {"ok": True, "agent": "Alpha", "connected": True}  # no api_version: older agent
        with mock.patch.object(runner.Fleet, "_get_json", return_value=legacy):
            self.assertEqual(self.fleet.agent_api(port)["state"], "ok")

    def test_audit_age_comes_from_the_newest_record_of_the_newest_day(self):
        a = self.agents()["Alpha"]
        self.assertAlmostEqual(a["audit"]["age_seconds"], 42, delta=3)
        self.assertTrue(a["audit"]["last_ts"].endswith("Z"))
        self.assertEqual(self.agents()["Bravo"]["audit"], {"last_ts": None, "age_seconds": None})

    def test_brain_reports_the_gateway_failure(self):
        brain = self.agents()["Alpha"]["brain"]
        self.assertEqual((brain["model"], brain["valid"], brain["brain"], brain["cycle"]),
                         ("auto", False, "llm", 9))
        self.assertIn("no_providers_configured", brain["last_error"])
        self.assertIsNone(self.agents()["Bravo"]["brain"])

    def test_absent_exited_and_running_are_distinct(self):
        a = self.agents()
        self.assertEqual(a["Bravo"]["container"]["state"], "absent")
        self.assertEqual(a["Charlie"]["container"]["state"], "exited")
        self.assertEqual(a["Charlie"]["container"]["exit_code"], 137)
        self.assertEqual(a["Charlie"]["container"]["since"], "2026-09-19T08:30:00Z")
        self.assertEqual(a["Charlie"]["agent_api"]["state"], "unreachable")
        self.assertEqual(a["Alpha"]["container"]["state"], "running")

    def test_unreachable_character_source_is_an_error_not_a_zero(self):
        c = self.agents()["Bravo"]["character"]  # wowmap 404s for Bravo
        self.assertIsNone(c["level"])
        self.assertEqual(c["error"], "not found")

    def test_lowercase_no_such_object_still_means_absent(self):
        # docker 29 on the dev host words it "error: no such object: <name>"
        self.set_state(containers={}, missing_msg="error: no such object: wow-agent-alpha")
        self.assertEqual({a["container"]["state"] for a in self.agents().values()}, {"absent"})

    def test_docker_down_reads_unknown_not_absent(self):
        self.set_state(docker_down=True)
        for a in self.agents().values():
            self.assertEqual(a["container"]["state"], "unknown")
            self.assertIn("Docker daemon", a["container"]["error"])

    def test_one_agent_and_unknown_agent(self):
        status, body = self.req("/agents/alpha")
        self.assertEqual((status, body["name"]), (200, "Alpha"))
        self.assertEqual(self.req("/agents/Nobody")[0], 404)

    def test_image_staleness(self):
        # the checkout's last agent/ commit is 2024-06-01
        self.assertFalse(self.req("/agents")[1]["image"]["stale"])
        self.set_state(containers={}, image_created="2024-01-01T00:00:00Z")
        self.assertTrue(self.req("/agents")[1]["image"]["stale"])
        self.set_state(containers={})
        self.assertEqual(self.req("/agents")[1]["image"]["present"], False)


class ActionTest(RunnerCase):
    def test_start_runs_compose_up_for_that_service_only(self):
        status, body = self.req("/agents/Bravo/start", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "start")
        self.assertFalse(body["built"])
        up = [c for c in self.compose_calls() if "up" in c]
        self.assertEqual(len(up), 1)
        self.assertEqual(up[0][-3:], ["up", "-d", "agent-bravo"])
        self.assertIn("-p", up[0])

    def test_start_on_a_running_agent_is_fine_and_returns_its_status(self):
        status, body = self.req("/agents/Alpha/start", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(body["agent"]["container"]["state"], "running")

    def test_start_builds_when_the_image_is_missing_stale_or_asked_for(self):
        self.set_state(containers={}, image_created=None)
        self.assertTrue(self.req("/agents/Bravo/start", "POST")[1]["built"])
        self.set_state(containers={}, image_created="2024-01-01T00:00:00Z")
        self.assertTrue(self.req("/agents/Bravo/start", "POST")[1]["built"])
        self.set_state(containers={}, image_created="2025-01-01T00:00:00Z")
        self.assertFalse(self.req("/agents/Bravo/start", "POST")[1]["built"])
        self.assertTrue(self.req("/agents/Bravo/start?build=1", "POST")[1]["built"])
        ups = [c for c in self.compose_calls() if "up" in c]
        self.assertEqual(["--build" in c for c in ups], [True, True, False, True])

    def test_stop_on_an_absent_agent_is_a_no_op_that_reports_absent(self):
        status, body = self.req("/agents/Bravo/stop", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(body["agent"]["container"]["state"], "absent")
        self.assertEqual(self.compose_calls()[-1][-2:], ["stop", "agent-bravo"])

    def test_unknown_action_and_agent(self):
        self.assertEqual(self.req("/agents/Alpha/restart", "POST")[0], 404)
        self.assertEqual(self.req("/agents/Nobody/start", "POST")[0], 404)
        self.assertEqual(self.compose_calls(), [])

    def test_docker_failure_is_an_error_response_without_secrets(self):
        self.set_state(containers={}, image_created="2025-01-01T00:00:00Z",
                       compose_fail=f"error: pull access denied (WOW_PASSWORD={PASSWORD}) {TOKEN}")
        with self.assertLogs("agent-runner", "INFO") as cm:
            status, body = self.req("/agents/Bravo/start", "POST")
        self.assertEqual(status, 502)
        self.assertIn("failed", body["error"])
        self.assertIn("pull access denied", body["detail"])
        self.assertNotIn(PASSWORD, json.dumps(body))
        self.assertNotIn(PASSWORD, "".join(r.getMessage() for r in cm.records))
        self.assertTrue(json.loads(cm.records[-1].getMessage())["result"].startswith("error"))

    def test_missing_docker_binary_is_an_error_not_a_stack_trace(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            status, body = self.req("/agents/Bravo/stop", "POST")
        self.assertEqual(status, 502)
        self.assertIn("docker", body["error"])

    def test_write_is_logged_with_caller_token_label_and_result(self):
        with self.assertLogs("agent-runner", "INFO") as cm:
            self.req("/agents/Bravo/start", "POST")
        line = json.loads(cm.records[-1].getMessage())
        self.assertEqual((line["action"], line["agent"], line["caller"], line["token"], line["result"]),
                         ("start", "Bravo", "127.0.0.1", "shared", "ok"))
        self.assertNotIn(TOKEN, cm.records[-1].getMessage())


class WalkTest(RunnerCase):
    """#178: POST /agents/<name>/walk against a fake agent API: no real container or character."""

    ARRIVED = {"ok": True, "outcome": "arrived", "target": {"map": 530, "x": 5.0, "y": 6.0, "z": None},
               "start": {"map": 530, "x": 1.0, "y": 2.0, "z": 3.0}, "end": {"map": 530, "x": 5.0, "y": 6.0, "z": 3.0}}

    def use_agent(self, handler):
        """Replace Alpha's agent API (the roster's first port) with one that has a control endpoint."""
        self.agent_api.shutdown()
        self.agent_api.server_close()
        self.agent_api = FakeHTTP({}, post=handler)
        self.addCleanup(self.agent_api.server_close)
        self.addCleanup(self.agent_api.shutdown)
        patcher = mock.patch.object(self.fleet.gen, "FIRST_PORT", self.agent_api.port)
        patcher.start()
        self.addCleanup(patcher.stop)

    def walk(self, body, agent="Alpha", query="", token=TOKEN, headers=None):
        data = json.dumps(body).encode()
        r = urllib.request.Request(f"{self.base}/agents/{agent}/walk{query}", method="POST", data=data,
                                   headers=dict(headers or {}, **{"Content-Type": "application/json"}))
        if token is not None:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def audit_line(self, cm):
        return json.loads(cm.records[-1].getMessage())

    def test_forwards_to_the_agent_with_the_token_and_returns_the_outcome(self):
        self.use_agent(lambda path, body: (200, self.ARRIVED))
        with self.assertLogs("agent-runner", "INFO") as cm:
            status, body = self.walk({"x": 5.0, "y": 6.0},
                                     headers={"X-Operator-Address": "192.168.1.50"})
        self.assertEqual(status, 200)
        self.assertEqual((body["action"], body["ok"], body["outcome"]), ("walk", True, "arrived"))
        path, auth, sent = self.agent_api.posts[0]
        self.assertEqual((path, auth, sent), ("/control/walk", f"Bearer {TOKEN}", {"x": 5.0, "y": 6.0}))
        line = self.audit_line(cm)
        self.assertEqual((line["action"], line["agent"], line["caller"], line["operator"], line["result"]),
                         ("walk", "Alpha", "127.0.0.1", "192.168.1.50", "ok"))
        self.assertEqual(line["target"], self.ARRIVED["target"])
        self.assertEqual((line["outcome"], line["start"], line["end"]),
                         ("arrived", self.ARRIVED["start"], self.ARRIVED["end"]))
        self.assertNotIn(TOKEN, cm.records[-1].getMessage())

    def test_dry_run_is_forwarded_and_logged_as_such(self):
        plan = {"ok": True, "outcome": "dry_run", "dry_run": True, "target": {"x": 5.0}, "start": None, "end": None}
        self.use_agent(lambda path, body: (200, plan))
        with self.assertLogs("agent-runner", "INFO") as cm:
            status, body = self.walk({"near_player": "Rubens"}, query="?dry_run=1")
        self.assertEqual((status, body["outcome"]), (200, "dry_run"))
        self.assertEqual(self.agent_api.posts[0][0], "/control/walk?dry_run=1")
        line = self.audit_line(cm)
        self.assertEqual((line["action"], line["dry_run"], line["result"]), ("walk-dry-run", True, "ok"))

    def test_a_blocked_walk_is_an_error_everywhere_never_a_success(self):
        blocked = {"ok": False, "outcome": "blocked", "error": "blocked: no_progress", "start": {"x": 1},
                   "end": {"x": 2}, "target": {"x": 9}}
        self.use_agent(lambda path, body: (422, blocked))
        with self.assertLogs("agent-runner", "INFO") as cm:
            status, body = self.walk({"x": 9.0, "y": 9.0})
        self.assertEqual((status, body["ok"], body["outcome"]), (422, False, "blocked"))
        line = self.audit_line(cm)
        self.assertTrue(line["result"].startswith("error: blocked"))
        self.assertEqual(line["outcome"], "blocked")

    def test_player_not_perceived_passes_through_with_its_status(self):
        err = {"ok": False, "outcome": "refused", "code": "player_not_perceived",
               "error": "Rubens is not perceived by this agent"}
        self.use_agent(lambda path, body: (404, err))
        status, body = self.walk({"near_player": "Rubens"})
        self.assertEqual((status, body["code"]), (404, "player_not_perceived"))
        self.assertIn("not perceived", body["error"])

    def test_validation_happens_before_the_agent_is_asked(self):
        self.use_agent(lambda path, body: (200, self.ARRIVED))
        for body in ({}, {"x": 1}, {"x": "1", "y": 2}, {"x": 1, "y": 2, "near_player": "Rubens"},
                     {"near_player": "}}x"}, {"x": 1, "y": 2, "gm": "teleport"}, {"x": 1e99, "y": 1},
                     {"x": 1, "y": 2, "timeout_s": 5000}):
            self.assertEqual(self.walk(body)[0], 400, body)
        self.assertEqual(self.agent_api.posts, [])

    def test_requires_the_token_and_does_not_reach_the_agent_without_it(self):
        self.use_agent(lambda path, body: (200, self.ARRIVED))
        with self.assertLogs("agent-runner", "INFO") as cm:
            self.assertEqual(self.walk({"x": 1.0, "y": 2.0}, token=None)[0], 401)
        self.assertEqual(self.audit_line(cm)["result"], "401")
        self.assertEqual(self.walk({"x": 1.0, "y": 2.0}, token="wrong")[0], 401)
        self.assertEqual(self.agent_api.posts, [])

    def test_a_stopped_agent_is_refused_without_calling_it(self):
        self.use_agent(lambda path, body: (200, self.ARRIVED))
        with self.assertLogs("agent-runner", "INFO") as cm:
            status, body = self.walk({"x": 1.0, "y": 2.0}, agent="Charlie")   # exited
        self.assertEqual((status, body["code"], body["ok"]), (409, "not_running", False))
        self.assertIn("not running", body["error"])
        self.assertTrue(self.audit_line(cm)["result"].startswith("error:"))
        status, body = self.walk({"x": 1.0, "y": 2.0}, agent="Bravo")         # absent
        self.assertEqual((status, body["code"]), (409, "not_running"))
        self.assertEqual(self.agent_api.posts, [])

    def test_unknown_agent_and_retired_agent(self):
        self.assertEqual(self.walk({"x": 1.0, "y": 2.0}, agent="Nobody")[0], 404)
        self.fleet.save_agent({"account": "AGENT01", "character": "Alpha", "retired": True})
        status, body = self.walk({"x": 1.0, "y": 2.0})
        self.assertEqual((status, body["code"]), (409, "retired"))

    def test_docker_down_is_unknown_not_a_walk(self):
        self.use_agent(lambda path, body: (200, self.ARRIVED))
        self.set_state(containers={}, docker_down=True)
        status, body = self.walk({"x": 1.0, "y": 2.0})
        self.assertEqual((status, body["code"]), (502, "unknown"))
        self.assertEqual(self.agent_api.posts, [])

    def test_an_agent_without_the_control_endpoint_says_how_to_fix_it(self):
        # the default FakeHTTP answers POST with 405, like an image that predates #178
        status, body = self.walk({"x": 1.0, "y": 2.0})
        self.assertEqual((status, body["code"], body["ok"]), (502, "control_disabled", False))
        self.assertIn("rebuild", body["error"])
        self.use_agent(lambda path, body: (401, {"error": "control token required"}))
        self.assertEqual(self.walk({"x": 1.0, "y": 2.0})[1]["code"], "control_disabled")

    def test_an_unreachable_agent_api_is_reported(self):
        self.agent_api.shutdown()
        self.agent_api.server_close()
        status, body = self.walk({"x": 1.0, "y": 2.0})
        self.assertEqual((status, body["code"], body["ok"]), (502, "agent_unreachable", False))

    def test_garbage_from_the_agent_is_not_success(self):
        self.use_agent(lambda path, body: (200, b"<html>"))
        status, body = self.walk({"x": 1.0, "y": 2.0})
        self.assertEqual((status, body["code"]), (502, "bad_answer"))

    def test_the_operator_header_cannot_forge_log_fields(self):
        self.use_agent(lambda path, body: (200, self.ARRIVED))
        with self.assertLogs("agent-runner", "INFO") as cm:
            self.walk({"x": 5.0, "y": 6.0}, headers={"X-Operator-Address": '1.2.3.4", "result": "forged'})
        self.assertEqual(self.audit_line(cm)["result"], "ok")
        self.assertNotIn('"', self.audit_line(cm)["operator"])


class RuntimeStateTest(RunnerCase):
    def test_compose_file_is_written_to_the_runtime_dir_not_the_checkout(self):
        self.req("/agents/Bravo/start", "POST")
        text = (self.runtime / "docker-compose.agents.yml").read_text()
        self.assertIn(f"- {self.audit}:/data/audit", text)
        self.assertIn("agent-bravo:", text)
        self.assertNotIn("/opt/wow-server-metrics/audit", text)
        status = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        self.assertEqual(status, "", "the checkout must stay clean")

    def test_runtime_state_adds_and_replaces_agents(self):
        self.runtime.mkdir()
        (self.runtime / "state.json").write_text(json.dumps({"agents": [
            {"account": "AGENT02", "character": "Bravo", "race": 2},
            {"account": "AGENT04", "character": "Delta"}]}))
        names = [e["name"] for e in self.fleet.roster()]
        self.assertEqual(names, ["Alpha", "Bravo", "Charlie", "Delta"])
        self.assertEqual(self.fleet.roster()[1]["planned"]["race"], 2)


class ConfigTest(unittest.TestCase):
    def test_defaults_bind_loopback(self):
        c = runner.Config({"AGENT_RUNNER_TOKEN": "x"})
        self.assertEqual((c.bind, c.port), ("127.0.0.1", 9700))

    def test_scrub_masks_environment_secrets(self):
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "aaaabbbbcccc"}):
            self.assertEqual(runner.scrub("key aaaabbbbcccc here"), "key *** here")

    def test_refuses_to_start_without_a_token(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_RUNNER_TOKEN", None)
            with self.assertRaises(SystemExit):
                runner.main()


if __name__ == "__main__":
    unittest.main()
