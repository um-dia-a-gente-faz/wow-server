"""#323: the FastAPI shell (api.py): the v2 JSON API, its SSE stream, the checked-in OpenAPI
spec, and the catch-all that serves the old routes with the bytes app.Handler sends.

No socket to the game and no database: the repo functions are patched with fakes, as in
test_routes.py. The catch-all is compared with routes.dispatch and with the old server."""
import io
import itertools
import json
import os
import pathlib
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

# #670: point CALIBRATION_FILE at a temp file before any wowmap import
import _calibration_env  # noqa: F401
# pagesrc puts wowmap/ on sys.path for this suite (the other wowmap tests do it with a sys.path edit)
import pagesrc  # noqa: F401
import agents  # noqa: E402
import api  # noqa: E402
import app  # noqa: E402
import players  # noqa: E402
import routes  # noqa: E402
import state  # noqa: E402
import webio  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from repo import players as players_repo  # noqa: E402

OPENAPI = pathlib.Path(__file__).resolve().parents[1] / "openapi.json"
REGENERATE = ("cd tools/wowmap/wowmap && python3 -c \"import api, json; "
              "print(json.dumps(api.app.openapi(), indent=2, sort_keys=True))\" > ../openapi.json")


class FakeTables:
    display_map = {}
    area_parent = {}

    def to_normalised(self, zone, x, y):
        return 0.25, 0.5

    def continent_normalised(self, cmap, x, y):
        return None

    def zone_name(self, area_id):
        return "Elwynn Forest"

    def continent_name(self, cmap, zone):
        return "Eastern Kingdoms"

    def game_coords(self, zone, x, y):
        return 40.0, 60.0


class FakeGrid:
    def area_id(self, cmap, x, y):
        return 0


ONLINE = [("Aaa", 10, 1, 1, 0, 12, 100.0, 200.0, 30.0, 1.5, 0, 600, 1),
          ("Bbb", 12, 8, 7, 36, 1581, 1.0, 2.0, 3.0, 0.0, 5, 700, 1)]


class FakeGameDb(unittest.TestCase):
    """The repo's online-characters query and the DBCs answer from the fakes above."""

    def setUp(self):
        for p in (mock.patch.object(state, "tables", return_value=FakeTables()),
                  mock.patch.object(state, "grid_areas", return_value=FakeGrid()),
                  mock.patch.object(players_repo, "online", return_value=ONLINE)):
            p.start()
            self.addCleanup(p.stop)


class PlayersTests(FakeGameDb):
    def setUp(self):
        super().setUp()
        self.client = TestClient(api.app)

    def test_players_are_the_fetch_players_rows_validated_by_the_model(self):
        r = self.client.get("/api/v2/players")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/json")
        expected = players.fetch_players()
        self.assertEqual(r.json(), {"players": expected})
        self.assertEqual(len(expected), 2)
        for p in r.json()["players"]:
            api.Player.model_validate(p)
            self.assertIn("class", p)   # the alias, not the Python name class_

    def test_players_route_runs_the_patched_fetch(self):
        row = dict(players.fetch_players()[0], name="Ccc")
        with mock.patch.object(players, "fetch_players", return_value=[row]):
            self.assertEqual(self.client.get("/api/v2/players").json(), {"players": [row]})


class StreamTests(FakeGameDb):
    def setUp(self):
        super().setUp()
        self.client = TestClient(api.app)

    def test_first_event_is_the_players_at_once(self):
        # The real generator, cut to its first event: TestClient reads a response only to its
        # end, and this stream never ends. The endless loop is covered by the next test.
        real_events = api._players_events
        with mock.patch.object(api, "_players_events",
                               side_effect=lambda: itertools.islice(real_events(), 1)), \
                self.client.stream("GET", "/api/v2/stream") as r:
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
            lines = []
            for line in r.iter_lines():
                lines.append(line)
                if line == "" and len(lines) > 2:   # the blank line ends the first event
                    break
        self.assertEqual(lines[0], "event: players")
        self.assertEqual(json.loads(lines[1][len("data: "):]), {"players": players.fetch_players()})

    def test_every_interval_sends_the_players_again(self):
        gen = api._players_events()
        with mock.patch.dict(os.environ, {"WOWMAP_SSE_INTERVAL_S": "0.25"}), \
                mock.patch.object(api.time, "sleep") as sleep:
            first = next(gen)
            second = next(gen)
        self.assertTrue(first.startswith("event: players\ndata: "))
        self.assertEqual(first, second)
        sleep.assert_called_with(0.25)


class CatchAllTests(FakeGameDb):
    """The routes the old server answers, through api.app, against routes.dispatch and app.Handler."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(api.app)
        self.old = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.old.serve_forever, daemon=True).start()
        self.addCleanup(self.old.server_close)
        self.addCleanup(self.old.shutdown)
        self.old_url = f"http://127.0.0.1:{self.old.server_address[1]}"

    def dispatch(self, method, path, body=b"", headers=None):
        """What routes.dispatch answers for the same request, as (status, body bytes, ctype, cache)."""
        parts = urlsplit(path)
        h = {"Content-Length": str(len(body)), **(headers or {})}
        status, out, ctype, cache = routes.dispatch(
            webio.Request(method, parts.path, parse_qs(parts.query), h, io.BytesIO(body).read, "testclient"))
        if isinstance(out, (dict, list)):
            out = json.dumps(out).encode()
        elif isinstance(out, str):
            out = out.encode()
        return status, out, ctype, cache

    def through_api(self, method, path, body=b"", headers=None):
        r = self.client.request(method, path, content=body, headers=headers)
        return r.status_code, r.content, r.headers["content-type"], r.headers.get("cache-control")

    def through_old_server(self, method, path, body=b"", headers=None):
        req = urllib.request.Request(self.old_url + path, data=body or None, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read(), r.headers["Content-Type"], r.headers.get("Cache-Control")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), e.headers["Content-Type"], e.headers.get("Cache-Control")

    def assert_same_as_old_server(self, method, path, body=b"", headers=None):
        want = self.dispatch(method, path, body, headers)
        got = self.through_api(method, path, body, headers)
        self.assertEqual(got, want, f"{method} {path}: api.app vs routes.dispatch")
        self.assertEqual(got, self.through_old_server(method, path, body, headers),
                         f"{method} {path}: api.app vs app.Handler")
        return got

    def test_healthz_page_and_json_routes_match_the_old_server(self):
        status, body, ctype, cache = self.assert_same_as_old_server("GET", "/healthz")
        self.assertEqual((status, json.loads(body), ctype), (200, {"ok": True}, "application/json"))
        status, body, ctype, _ = self.assert_same_as_old_server("GET", "/")
        self.assertEqual((status, ctype), (200, "text/html; charset=utf-8"))
        self.assertIn(b"<html", body.lower())
        self.assert_same_as_old_server("GET", "/api/agents")

    def test_static_file_keeps_its_content_type_and_cache_control(self):
        status, body, ctype, cache = self.assert_same_as_old_server("GET", "/static/leaflet.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", ctype)
        self.assertIn("max-age", cache)

    def test_cache_control_is_sent_when_the_route_sets_it(self):
        with mock.patch.object(agents, "fleet_status", return_value={"agents": []}):
            status, body, ctype, cache = self.assert_same_as_old_server("GET", "/api/fleet")
        self.assertEqual((status, cache), (200, "no-store"))

    def test_unknown_path_is_the_old_404(self):
        status, body, ctype, _ = self.assert_same_as_old_server("GET", "/no/such/route")
        self.assertEqual((status, json.loads(body)), (404, {"error": "not found"}))

    def test_post_body_reaches_its_route(self):
        seen = {}

        def fake_action(req, name, action):
            seen.update(name=name, action=action, body=req.read_body(int(req.header("Content-Length"))))
            return 200, {"ok": True}

        body = b'{"dry_run": true}'
        with mock.patch.object(agents, "fleet_action", side_effect=fake_action):
            status, out, ctype, _ = self.assert_same_as_old_server(
                "POST", "/api/fleet/agents/Luaprata/walk", body,
                {"X-Fleet-Action": "1", "Content-Type": "application/json"})
        self.assertEqual((status, json.loads(out)), (200, {"ok": True}))
        self.assertEqual(seen, {"name": "Luaprata", "action": "walk", "body": body})


class OpenApiTests(unittest.TestCase):
    def test_checked_in_spec_equals_the_app_spec(self):
        checked = json.loads(OPENAPI.read_text(encoding="utf-8"))
        self.assertEqual(checked, api.app.openapi(), f"openapi.json is stale. Regenerate: {REGENERATE}")

    def test_v2_routes_are_in_the_spec(self):
        paths = api.app.openapi()["paths"]
        self.assertIn("/api/v2/players", paths)
        self.assertIn("/api/v2/stream", paths)
        self.assertNotIn("/{path}", paths)   # the catch-all is not part of the contract


if __name__ == "__main__":
    unittest.main()
