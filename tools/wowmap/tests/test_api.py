"""#323: the FastAPI shell (api.py): the v2 JSON API, its SSE stream, the checked-in OpenAPI
spec, and the catch-all that serves the old routes with the bytes app.Handler sends.

No socket to the game and no database: the repo functions are patched with fakes, as in
test_routes.py. The catch-all is compared with routes.dispatch and with the old server."""
import asyncio
import io
import json
import os
import pathlib
import tempfile
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
import anyio  # noqa: E402
import anyio.to_thread  # noqa: E402
import httpx  # noqa: E402
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
                  mock.patch.object(players_repo, "online", return_value=ONLINE),
                  mock.patch.object(players_repo, "recent_offline", return_value=[])):
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
        self.assertEqual(r.json(), {"players": [dict(p, online=True, last_seen=None) for p in expected]})
        self.assertEqual(len(expected), 2)
        for p in r.json()["players"]:
            api.Player.model_validate(p)
            self.assertIn("class", p)   # the alias, not the Python name class_

    def test_players_route_runs_the_patched_fetch(self):
        row = dict(players.fetch_players()[0], name="Ccc")
        with mock.patch.object(players, "fetch_players", return_value=[row]):
            self.assertEqual(self.client.get("/api/v2/players").json(),
                             {"players": [dict(row, online=True, last_seen=None)]})

    def test_offline_rows_follow_the_online_ones_and_a_duplicate_name_is_dropped(self):
        online = [dict(players.fetch_players()[0], name="Aaa"), dict(players.fetch_players()[0], name="Zzz")]
        off = [dict(online[0], name="Aaa", online=False, last_seen=5),
               dict(online[0], name="Bbb", online=False, last_seen=9)]
        with mock.patch.object(players, "fetch_players", return_value=online), \
                mock.patch.object(players, "fetch_offline", return_value=off):
            got = self.client.get("/api/v2/players").json()["players"]
        self.assertEqual([(p["name"], p["online"], p["last_seen"]) for p in got],
                         [("Aaa", True, None), ("Zzz", True, None), ("Bbb", False, 9)])

    def test_fetch_offline_marks_rows_offline_with_last_seen(self):
        row = ("Off", 10, 1, 1, 0, 12, 1.0, 2.0, 3.0, 0.0, 0, 60, 0, 1700000000)
        with mock.patch.object(players_repo, "recent_offline", return_value=[row]):
            got = players.fetch_offline()
        self.assertEqual((got[0]["name"], got[0]["online"], got[0]["last_seen"]), ("Off", False, 1700000000))
        api.Player.model_validate(got[0])

    def test_old_players_route_never_reads_the_offline_rows(self):
        with mock.patch.object(players_repo, "recent_offline", side_effect=AssertionError("called")):
            r = self.client.get("/api/players")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([p["name"] for p in r.json()["players"]], ["Aaa", "Bbb"])


class ActivityTests(unittest.TestCase):
    class Fake:
        def feed(self, name, limit):
            return {"character": name, "agent": True, "sources": {"db": None},
                    "events": [{"t": 1.5, "kind": "level", "text": f"limit {limit}",
                                "detail": {"to": 5}, "source": "db", "inferred": False}]}

    def setUp(self):
        self.client = TestClient(api.app)

    def test_feed_is_returned_with_the_limit(self):
        with mock.patch.object(state, "activity", self.Fake()):
            r = self.client.get("/api/v2/characters/Luaprata/activity?limit=7")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["character"], "Luaprata")
        self.assertEqual(r.json()["events"][0]["text"], "limit 7")

    def test_default_limit_is_50(self):
        with mock.patch.object(state, "activity", self.Fake()):
            r = self.client.get("/api/v2/characters/Luaprata/activity")
        self.assertEqual(r.json()["events"][0]["text"], "limit 50")

    def test_disabled_feed_is_503(self):
        with mock.patch.object(state, "activity", None):
            r = self.client.get("/api/v2/characters/Luaprata/activity")
        self.assertEqual((r.status_code, r.json()), (503, {"error": "activity feed disabled"}))

    def test_limit_outside_1_to_200_is_422(self):
        with mock.patch.object(state, "activity", self.Fake()):
            for limit in ("0", "201", "-1", "x"):
                self.assertEqual(self.client.get(f"/api/v2/characters/A/activity?limit={limit}").status_code, 422)
            for limit in ("1", "200"):
                self.assertEqual(self.client.get(f"/api/v2/characters/A/activity?limit={limit}").status_code, 200)


async def open_stream(app, path):
    """Run an ASGI GET as a task. Returns (task, queue of body chunks, disconnect event): the
    stream runs until the test sets disconnect, which is how a client that went away looks to it."""
    chunks = asyncio.Queue()
    disconnect = asyncio.Event()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": b"", "headers": [], "client": ("testclient", 50000),
             "server": ("testserver", 80)}
    sent_request = False

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            await chunks.put(message["body"])

    return asyncio.ensure_future(app(scope, receive, send)), chunks, disconnect


class StreamTests(FakeGameDb):
    def setUp(self):
        super().setUp()
        self.client = TestClient(api.app)

    def test_first_event_is_the_players_at_once(self):
        # The real generator, cut to its first event: TestClient reads a response only to its
        # end, and this stream never ends. The endless loop is covered by the next test.
        real_events = api._players_events

        async def first_only():
            async for event in real_events():
                yield event
                return

        with mock.patch.object(api, "_players_events", side_effect=first_only), \
                self.client.stream("GET", "/api/v2/stream") as r:
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
            lines = []
            for line in r.iter_lines():
                lines.append(line)
                if line == "" and len(lines) > 2:   # the blank line ends the first event
                    break
        self.assertEqual(lines[0], "event: players")
        self.assertEqual(json.loads(lines[1][len("data: "):]), {"players": [dict(p, online=True, last_seen=None) for p in players.fetch_players()]})

    def test_every_interval_sends_the_players_again(self):
        async def two_events():
            gen = api._players_events()
            first = await gen.__anext__()
            second = await gen.__anext__()
            return first, second

        with mock.patch.dict(os.environ, {"WOWMAP_SSE_INTERVAL_S": "0.25"}), \
                mock.patch.object(api.asyncio, "sleep", new_callable=mock.AsyncMock) as sleep:
            first, second = asyncio.run(two_events())
        self.assertTrue(first.startswith("event: players\ndata: "))
        self.assertEqual(first, second)
        sleep.assert_awaited_with(0.25)

    def test_streams_do_not_hold_the_threadpool(self):
        # Three open streams against a threadpool of two: a stream that sleeps on a worker thread
        # would hold its token for good, so /healthz (a catch-all route, also threadpool) would wait.
        async def scenario():
            limiter = anyio.to_thread.current_default_thread_limiter()
            limiter.total_tokens = 2
            try:
                streams = [await open_stream(api.app, "/api/v2/stream") for _ in range(3)]
                for _task, chunks, _disconnect in streams:
                    await asyncio.wait_for(chunks.get(), 5)   # every stream gets its first event
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                             base_url="http://testserver") as client:
                    health = await asyncio.wait_for(client.get("/healthz"), 3)
                for task, _chunks, disconnect in streams:
                    disconnect.set()
                    await asyncio.wait_for(task, 5)
                return health.status_code, health.json()
            finally:
                limiter.total_tokens = 40   # anyio's default

        # A long interval: a sync generator sleeps on its worker thread for all of it, so /healthz
        # would wait the whole 10 s. The async one holds no thread while it waits.
        with mock.patch.object(players, "fetch_players", return_value=[]), \
                mock.patch.dict(os.environ, {"WOWMAP_SSE_INTERVAL_S": "10"}):
            self.assertEqual(asyncio.run(scenario()), (200, {"ok": True}))


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
        calls = []

        def fake_action(req, name, action):
            calls.append({"name": name, "action": action,
                          "body": req.read_body(int(req.header("Content-Length")))})
            return 200, {"ok": True}

        body = b'{"dry_run": true}'
        with mock.patch.object(agents, "fleet_action", side_effect=fake_action):
            # Only the catch-all's own call: nothing else (routes.dispatch, app.Handler) runs first.
            status, out, ctype, _ = self.through_api(
                "POST", "/api/fleet/agents/Luaprata/walk", body,
                {"X-Fleet-Action": "1", "Content-Type": "application/json"})
        self.assertEqual(calls, [{"name": "Luaprata", "action": "walk", "body": body}])
        self.assertEqual((status, json.loads(out)), (200, {"ok": True}))


class SpaTests(FakeGameDb):
    """/v2 serves the SvelteKit build (#324), registered before the catch-all."""

    def setUp(self):
        super().setUp()
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = pathlib.Path(d.name)
        (root / "index.html").write_text("<p>spa shell</p>")
        (root / "manifest.webmanifest").write_text('{"name": "wowmap"}')
        for attr, value in (("directory", str(root)), ("all_directories", [str(root)])):
            p = mock.patch.object(api.web, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(api.app)

    def test_root_is_index_html(self):
        r = self.client.get("/v2/")
        self.assertEqual((r.status_code, r.text), (200, "<p>spa shell</p>"))

    def test_unknown_deep_link_falls_back_to_index_html(self):
        r = self.client.get("/v2/some/deep/link")
        self.assertEqual((r.status_code, r.text), (200, "<p>spa shell</p>"))

    def test_manifest_is_served(self):
        r = self.client.get("/v2/manifest.webmanifest")
        self.assertEqual(r.json(), {"name": "wowmap"})

    def test_missing_asset_is_a_404_not_the_shell(self):
        self.assertEqual(self.client.get("/v2/_app/missing.js").status_code, 404)

    def test_old_root_page_is_unchanged(self):
        self.assertNotIn("spa shell", self.client.get("/").text)


class OpenApiTests(unittest.TestCase):
    def test_checked_in_spec_equals_the_app_spec(self):
        checked = json.loads(OPENAPI.read_text(encoding="utf-8"))
        self.assertEqual(checked, api.app.openapi(), f"openapi.json is stale. Regenerate: {REGENERATE}")

    def test_v2_routes_are_in_the_spec(self):
        paths = api.app.openapi()["paths"]
        self.assertIn("/api/v2/players", paths)
        self.assertIn("/api/v2/stream", paths)
        self.assertIn("/api/v2/characters/{name}/activity", paths)
        self.assertNotIn("/{path}", paths)   # the catch-all is not part of the contract


if __name__ == "__main__":
    unittest.main()
