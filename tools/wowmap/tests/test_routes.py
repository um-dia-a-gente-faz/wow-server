"""#249: the route table answers Requests without a socket and without a database.

Data comes from fake repo functions, so these cover routing, status codes and the JSON
shape of the handlers, not SQL (the SQL is covered by the per-feature tests)."""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agents  # noqa: E402
import character  # noqa: E402
import routes  # noqa: E402
import state  # noqa: E402
from repo import characters as characters_repo  # noqa: E402
from repo import players as players_repo  # noqa: E402
from webio import Request  # noqa: E402


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


def get(path, qs=None, method="GET", headers=None, body=b""):
    h = {"Content-Length": str(len(body)), **(headers or {})}
    return routes.dispatch(Request(method, path, qs or {}, h, lambda n: body[:n], "10.0.0.9"))


ONLINE = [("Aaa", 10, 1, 1, 0, 12, 100.0, 200.0, 30.0, 1.5, 0, 600, 1),
          ("Bbb", 12, 8, 7, 36, 1581, 1.0, 2.0, 3.0, 0.0, 5, 700, 1)]


class RouteTests(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(state, "tables", return_value=FakeTables()),
                  mock.patch.object(state, "grid_areas", return_value=FakeGrid()),
                  mock.patch.object(players_repo, "online", return_value=ONLINE),
                  mock.patch.object(players_repo, "online_zones", return_value=[(12, 0)])):
            p.start()
            self.addCleanup(p.stop)

    def test_healthz_and_unknown_paths(self):
        self.assertEqual(get("/healthz")[:2], (200, {"ok": True}))
        self.assertEqual(get("/nope")[:2], (404, {"error": "not found"}))
        self.assertEqual(get("/api/calibrate")[0], 404)            # POST-only
        self.assertEqual(get("/healthz", method="POST")[0], 404)   # GET-only

    def test_every_route_is_get_or_post_with_a_handler(self):
        self.assertTrue(all(m in ("GET", "POST") and callable(f) for m, _, f in routes.ROUTES))

    def test_players_and_summary_come_from_the_repo_rows(self):
        status, body, ctype, _ = get("/api/players")
        self.assertEqual((status, ctype), (200, "application/json"))
        self.assertEqual([p["name"] for p in body["players"]], ["Aaa", "Bbb"])
        self.assertEqual(body["players"][0]["class_name"], "Warrior")
        self.assertTrue(body["players"][0]["in_world"])
        self.assertFalse(body["players"][1]["in_world"])
        self.assertEqual(get("/api/summary")[:2], (200, {
            "online": 2, "in_world": 1, "in_instance": 1, "zones": ["Elwynn Forest"]}))

    def test_character_routes_pick_the_right_handler_and_unquote_the_name(self):
        with mock.patch.object(character, "fetch_character", return_value={"name": "A B"}) as detail, \
                mock.patch.object(character, "character_kind", return_value={"kind": "human"}) as kind:
            self.assertEqual(get("/api/character/A%20B")[:2], (200, {"name": "A B"}))
            detail.assert_called_once_with("A B")
            self.assertEqual(get("/api/character/Aaa/kind")[:3:2], (200, "application/json"))
            kind.assert_called_once_with("Aaa")
        with mock.patch.object(character, "fetch_character", return_value=None):
            self.assertEqual(get("/api/character/Zzz")[:2], (404, {"error": "character not found"}))
            self.assertEqual(get("/api/character/")[0], 404)

    def test_kind_and_explored_use_the_repo_and_set_no_store(self):
        with mock.patch.object(characters_repo, "account_of", return_value=("Aaa", "RUBENS")):
            status, body, _, cache = get("/api/character/Aaa/kind")
        self.assertEqual((status, body, cache), (200, {"name": "Aaa", "kind": "human"}, "no-store"))
        with mock.patch.object(characters_repo, "explored_zones", return_value=None):
            self.assertEqual(get("/api/character/Aaa/explored")[0], 404)

    def test_activity_is_503_without_a_feed_and_parses_the_limit(self):
        with mock.patch.object(state, "activity", None):
            self.assertEqual(get("/api/character/Aaa/activity")[0], 503)
        feed = mock.Mock()
        feed.feed.return_value = {"events": []}
        with mock.patch.object(state, "activity", feed):
            self.assertEqual(get("/api/character/Aaa/activity", {"limit": ["7"]})[:2], (200, {"events": []}))
            get("/api/character/Aaa/activity", {"limit": ["x"]})
        self.assertEqual([c.args for c in feed.feed.call_args_list], [("Aaa", 7), ("Aaa", 50)])

    def test_agent_routes(self):
        with mock.patch.object(agents, "AGENT_APIS", {"luaprata": ("Luaprata", "http://h:1")}):
            self.assertEqual(get("/api/agents")[:2], (200, {"agents": ["Luaprata"]}))
            self.assertEqual(get("/api/agent/Luaprata/secrets")[0], 404)

    def test_fleet_post_needs_the_header_before_anything_else(self):
        self.assertEqual(get("/api/fleet/agents/Luaprata/start", method="POST")[0], 403)

    def test_calibrate_validates_without_touching_the_store(self):
        self.assertEqual(get("/api/calibrate", method="POST", body=b"not json")[0], 400)
        self.assertEqual(get("/api/calibrate", method="POST",
                             body=b'{"area_id": 0, "dx": 1, "dy": 1}')[0], 400)
        self.assertEqual(get("/api/calibrate", method="POST",
                             headers={"Content-Length": "999999"})[0], 413)

    def test_page_is_html(self):
        status, body, ctype, _ = get("/")
        self.assertEqual((status, ctype), (200, "text/html; charset=utf-8"))
        self.assertIn("<html", body.lower())
        self.assertEqual(get("/index.html")[0], 200)

    def test_a_failing_get_handler_is_a_500_not_an_exception(self):
        with mock.patch.object(players_repo, "online", side_effect=RuntimeError("db down")), \
                self.assertLogs(state.log, "ERROR"):
            self.assertEqual(get("/api/players")[:2], (500, {"error": "db down"}))

    def test_areas_passes_the_map_filter(self):
        with mock.patch("areas.fetch_areas", return_value=[]) as fa, \
                mock.patch("areas.fetch_continents", return_value=[]):
            status, body, _, _ = get("/api/areas", {"map": ["530"]})
        fa.assert_called_once_with(530)
        self.assertEqual((status, body), (200, {"areas": [], "continents": [], "in_use": [
            {"zone": 12, "map": 0}]}))


if __name__ == "__main__":
    unittest.main()
