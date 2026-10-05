"""#178: walking an agent from the console. A fake runner stands in for tools/agent-runner;
nothing here moves a character."""
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
import pagesrc  # noqa: E402
import state  # noqa: E402
import agents  # noqa: E402
import fleet  # noqa: E402
import walk  # noqa: E402
from test_position import RUBENS, write_dbc, f32  # noqa: E402
from transform import DbcTables  # noqa: E402

TOKEN = "walk-test-token-1234"
ARRIVED = {"ok": True, "outcome": "arrived", "start": {"map": 530, "x": 1.0, "y": 2.0, "z": 3.0},
           "end": {"map": 530, "x": 10.0, "y": 20.0, "z": 3.0}, "target": {"map": 530, "x": 10.0, "y": 20.0}}


def make_tables(tmp):
    d = pathlib.Path(tmp)
    write_dbc(d / "WorldMapArea.dbc", 11, [
        [462, 530, 3430, "EversongWoods", f32(-4487.5), f32(-9412.5), f32(11041.667), f32(7758.333), 0]])
    write_dbc(d / "AreaTable.dbc", 36, [[3430, 530, 0] + [0] * 8 + ["Eversong Woods"]])
    write_dbc(d / "Map.dbc", 66, [[530, "Expansion01", 0, 0, 0, "Outland"]])
    return DbcTables(str(d))


class TransformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.t = make_tables(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_from_normalised_inverts_to_normalised(self):
        for x, y in ((RUBENS[1], RUBENS[2]), (9000.0, -5000.0), (8000.0, -9000.0)):
            nx, ny = self.t.to_normalised(3430, x, y)
            wx, wy = self.t.from_normalised(3430, nx, ny)
            self.assertAlmostEqual(wx, x, places=2)
            self.assertAlmostEqual(wy, y, places=2)

    def test_the_map_corners_are_the_rect_corners(self):
        # (0, 0) is the top-left of the image: world X = rect "left", world Y = rect "top"
        x, y = self.t.from_normalised(3430, 0.0, 0.0)
        self.assertAlmostEqual(x, 11041.667, places=1)
        self.assertAlmostEqual(y, -4487.5, places=1)

    def test_unknown_zone(self):
        self.assertIsNone(self.t.from_normalised(1, 0.5, 0.5))


class BodyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.t = make_tables(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def bad(self, payload, status=400):
        with self.assertRaises(walk.WalkError) as cm:
            walk.build_runner_body(payload, self.t)
        self.assertEqual(cm.exception.status, status, payload)

    def test_a_map_click_becomes_world_coordinates_on_the_zones_map(self):
        nx, ny = self.t.to_normalised(3430, RUBENS[1], RUBENS[2])
        body = walk.build_runner_body({"area_id": 3430, "nx": nx, "ny": ny}, self.t)
        self.assertEqual(set(body), {"x", "y", "map"})   # no z: a click has no height
        self.assertAlmostEqual(body["x"], RUBENS[1], delta=0.1)
        self.assertAlmostEqual(body["y"], RUBENS[2], delta=0.1)
        self.assertEqual(body["map"], 530)

    def test_a_player_name(self):
        self.assertEqual(walk.build_runner_body({"near_player": " Rubens "}, self.t), {"near_player": "Rubens"})

    def test_bad_requests(self):
        self.bad([])
        self.bad({})
        self.bad({"area_id": 3430})
        self.bad({"area_id": 3430, "nx": "0.5", "ny": 0.5})
        self.bad({"area_id": 3430, "nx": float("nan"), "ny": 0.5})
        self.bad({"area_id": 3430, "nx": 1.5, "ny": 0.5})
        self.bad({"area_id": 3430, "nx": -0.1, "ny": 0.5})
        self.bad({"area_id": True, "nx": 0.5, "ny": 0.5})
        self.bad({"area_id": 3430, "nx": 0.5, "ny": 0.5, "z": 99})        # unknown field
        self.bad({"near_player": "}}x"})
        self.bad({"near_player": "Rubens", "area_id": 3430, "nx": 0.5, "ny": 0.5})
        self.bad({"area_id": 1, "nx": 0.5, "ny": 0.5}, status=404)


class WalkRunner(BaseHTTPRequestHandler):
    """Records every walk the runner is asked for and answers from `reply`."""
    seen = []
    reply = (200, ARRIVED)

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        WalkRunner.seen.append((self.path, self.headers.get("Authorization"),
                                self.headers.get("X-Operator-Address"), json.loads(self.rfile.read(length) or b"null")))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            status, obj = 401, {"error": "token required"}
        else:
            status, obj = WalkRunner.reply
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class ProxyTests(unittest.TestCase):
    token = TOKEN

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.t = make_tables(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        WalkRunner.seen, WalkRunner.reply = [], (200, ARRIVED)
        runner = serve(WalkRunner)
        self.addCleanup(runner.server_close)
        self.addCleanup(runner.shutdown)
        self.start_wowmap(fleet.Runner(f"http://127.0.0.1:{runner.server_address[1]}", self.token))

    def start_wowmap(self, rn):
        for mod, name, value in ((agents, "RUNNER", rn), (agents, "AGENT_APIS", {}),
                                 (state, "tables", lambda: self.t)):
            p = mock.patch.object(mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        srv = serve(app.Handler)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(self, name, payload, query="", headers=None, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        hdrs = {"X-Fleet-Action": "1", "Content-Type": "application/json"} if headers is None else headers
        req = urllib.request.Request(f"{self.base}/api/fleet/agents/{name}/walk{query}", data=data,
                                     method="POST", headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_a_click_is_forwarded_as_world_coordinates_with_the_token_and_the_operator(self):
        nx, ny = self.t.to_normalised(3430, RUBENS[1], RUBENS[2])
        status, body = self.post("Luaprata", {"area_id": 3430, "nx": nx, "ny": ny})
        self.assertEqual((status, body["outcome"]), (200, "arrived"))
        path, auth, operator, sent = WalkRunner.seen[0]
        self.assertEqual((path, auth, operator), ("/agents/Luaprata/walk", f"Bearer {TOKEN}", "127.0.0.1"))
        self.assertEqual(sent["map"], 530)
        self.assertAlmostEqual(sent["x"], RUBENS[1], delta=0.1)

    def test_near_player_and_dry_run(self):
        WalkRunner.reply = (200, {"ok": True, "outcome": "dry_run", "dry_run": True})
        status, body = self.post("Luaprata", {"near_player": "Rubens"}, query="?dry_run=1")
        self.assertEqual((status, body["outcome"]), (200, "dry_run"))
        self.assertEqual(WalkRunner.seen[0][0], "/agents/Luaprata/walk?dry_run=1")
        self.assertEqual(WalkRunner.seen[0][3], {"near_player": "Rubens"})

    def test_failures_pass_through_with_their_reason_and_never_look_like_success(self):
        for status, reply in ((422, {"ok": False, "outcome": "blocked", "error": "blocked: no_progress"}),
                              (422, {"ok": False, "outcome": "timeout", "error": "timed out after 60s"}),
                              (404, {"ok": False, "outcome": "refused", "code": "player_not_perceived",
                                     "error": "Rubens is not perceived by this agent"}),
                              (409, {"ok": False, "outcome": "refused", "code": "not_running", "error": "not running"})):
            WalkRunner.reply = (status, reply)
            got, body = self.post("Luaprata", {"near_player": "Rubens"})
            self.assertEqual(got, status)
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"], reply["error"])

    def test_an_odd_200_that_is_not_arrival_is_marked_failed(self):
        WalkRunner.reply = (200, {"ok": True, "outcome": "stopped"})
        self.assertFalse(self.post("Luaprata", {"near_player": "Rubens"})[1]["ok"])

    def test_cross_site_posts_are_refused_and_nothing_reaches_the_runner(self):
        self.assertEqual(self.post("Luaprata", {"near_player": "Rubens"}, headers={})[0], 403)
        self.assertEqual(WalkRunner.seen, [])

    def test_bad_bodies_are_400_without_calling_the_runner(self):
        self.assertEqual(self.post("Luaprata", None, raw=b"nope")[0], 400)
        self.assertEqual(self.post("Luaprata", None, raw=b"")[0], 400)
        self.assertEqual(self.post("Luaprata", {"near_player": "}}"})[0], 400)
        self.assertEqual(self.post("Luaprata", {"x": 1, "y": 2})[0], 400)      # raw world coordinates are not accepted
        self.assertEqual(WalkRunner.seen, [])

    def test_runner_rejecting_the_token_is_wowmaps_problem_not_a_401(self):
        self.start_wowmap(fleet.Runner(f"http://127.0.0.1:{self.runner_port()}", "wrong"))
        status, body = self.post("Luaprata", {"near_player": "Rubens"})
        self.assertEqual((status, body["ok"]), (502, False))
        self.assertIn("token", body["error"])

    def runner_port(self):
        return int(agents.RUNNER.url.rsplit(":", 1)[1])

    def test_unreachable_runner_says_the_walk_may_have_run(self):
        self.start_wowmap(fleet.Runner("http://127.0.0.1:1", TOKEN))
        status, body = self.post("Luaprata", {"near_player": "Rubens"})
        self.assertEqual((status, body["ok"]), (502, False))
        self.assertIn("may or may not", body["error"])

    def test_no_runner_means_read_only(self):
        self.start_wowmap(fleet.Runner("", ""))
        status, body = self.post("Luaprata", {"near_player": "Rubens"})
        self.assertEqual((status, body["ok"], body["code"]), (503, False, "no_runner"))
        self.assertEqual(WalkRunner.seen, [])

    def test_the_token_never_reaches_the_browser(self):
        WalkRunner.reply = (422, {"ok": False, "outcome": "failed", "error": f"leaked {TOKEN}"})
        self.assertNotIn(TOKEN, json.dumps(self.post("Luaprata", {"near_player": "Rubens"})[1]))


WHY_JS = r"""
const cases = JSON.parse(process.argv[2]);
const out = cases.map((c) => walkWhyNot(c.name, c.fleet, c.isAgent, c.here));
console.log(JSON.stringify(out));
"""


def fleet_state(**agent):
    entry = {"name": "Luaprata", "retired": False, "container": {"state": "running"},
             "agent_api": {"state": "ok", "connected": True}}
    entry.update(agent)
    return {"configured": True, "runner": "ok", "error": None, "agents": [entry]}


class PageTests(unittest.TestCase):
    def test_page_has_the_walk_block_and_hooks(self):
        self.assertNotIn("@walk-", pagesrc.PAGE)
        self.assertIn("const Walk = ", pagesrc.PAGE)
        self.assertIn("Walk.pickPoint(e)", pagesrc.PAGE)
        self.assertIn("Walk.pickPlayer(name)", pagesrc.PAGE)
        self.assertNotIn("innerHTML", pagesrc.WALK_JS)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_walk_script_parses(self):
        script = pagesrc.WALK_JS.replace("<script>", "").replace("</script>", "")
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "walk.js"
            f.write_text(script)
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_disabled_with_a_reason_unless_the_agent_can_walk(self):
        script = pagesrc.WALK_JS.replace("<script>", "").replace("</script>", "")
        start = script.index("function walkWhyNot")
        fn = script[start:script.index("\nconst Walk = ")]
        here = {"in_world": True}
        cases = [
            {"name": "Rubens", "fleet": fleet_state(), "isAgent": False, "here": here},                       # human
            {"name": "Luaprata", "fleet": None, "isAgent": True, "here": here},                               # no runner info
            {"name": "Luaprata", "fleet": {"configured": False, "runner": "disabled"}, "isAgent": True, "here": here},
            {"name": "Luaprata", "fleet": dict(fleet_state(), runner="unreachable"), "isAgent": True, "here": here},
            {"name": "Luaprata", "fleet": fleet_state(container={"state": "exited"}), "isAgent": True, "here": here},
            {"name": "Luaprata", "fleet": fleet_state(container={"state": "unknown"}), "isAgent": True, "here": here},
            {"name": "Luaprata", "fleet": fleet_state(agent_api={"state": "unreachable"}), "isAgent": True, "here": here},
            {"name": "Luaprata", "fleet": fleet_state(agent_api={"state": "ok", "connected": False}), "isAgent": True, "here": here},
            {"name": "Luaprata", "fleet": fleet_state(retired=True), "isAgent": True, "here": here},
            {"name": "Luaprata", "fleet": fleet_state(), "isAgent": True, "here": {"in_world": False}},       # instance
            {"name": "Ghost", "fleet": fleet_state(), "isAgent": True, "here": here},                         # not in roster
            {"name": "luaprata", "fleet": fleet_state(), "isAgent": True, "here": here},                      # can walk
        ]
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "why.js"
            f.write_text(fn + WHY_JS)
            r = subprocess.run(["node", str(f), json.dumps(cases)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        reasons = json.loads(r.stdout)
        self.assertIn("Human characters", reasons[0])
        self.assertIn("GM commands", reasons[0])
        for i, fragment in ((1, "runner"), (2, "No agent runner"), (3, "unreachable"), (4, "exited"),
                            (5, "unknown"), (6, "API is unreachable"), (7, "not logged in"), (8, "retired"),
                            (9, "instance"), (10, "roster")):
            self.assertIn(fragment, reasons[i], i)
        self.assertIsNone(reasons[11])


if __name__ == "__main__":
    unittest.main()
