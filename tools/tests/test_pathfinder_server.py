"""tools/pathfinder/server.py over real HTTP: the server runs on port 0 in a thread, over a temp mmaps
dir holding the fixture tiles that test_pathfinder_navmesh.py builds (build_tile, grid, sloped).
Stdlib only.
"""
import json
import os
import pathlib
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from test_pathfinder_navmesh import build_tile, grid, sloped  # noqa: E402  (tools/tests is the start dir)
from tools.pathfinder.navmesh import DEFAULT_MAX_TILES, NavMesh  # noqa: E402  (the checkout root is on sys.path, ADR 0019)
from tools.pathfinder.server import Maps, Server, main  # noqa: E402

MAP = 530
TILE_KEY = NavMesh({}, {}).tile_coords(25, 25)   # every fixture point is in this tile


def wow_point(x, y, z=0.0):
    return {"x": x, "y": y, "z": z}


class ServerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mmaps = pathlib.Path(self._tmp.name)
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self._tmp.cleanup()

    def serve(self, polys, map_id=MAP):
        """Write the map's files for one tile of polys, then start the server on port 0."""
        self.write_map(polys, map_id)
        self.start()

    def start(self):
        self.server = Server(("127.0.0.1", 0), self.mmaps)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def write_map(self, polys, map_id=MAP):
        """The .mmap and the one .mmtile of map_id (Detour points, as build_tile takes them)."""
        gx, gy = TILE_KEY
        (self.mmaps / f"{map_id:03}.mmap").write_bytes(
            struct.pack("<5f2i", 0, 0, 0, 533.3333, 533.3333, 64, 1024))
        (self.mmaps / f"{map_id:03}{gx:02}{gy:02}.mmtile").write_bytes(build_tile(polys))

    def call(self, method, path, body=None, raw=None, timeout=5):
        """(status, decoded JSON) of one request. body is encoded as JSON, raw is sent as is."""
        port = self.server.server_address[1]
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            with err:
                return err.code, json.loads(err.read())

    def path(self, start, end, map_id=MAP, timeout=5):
        return self.call("POST", "/path", {"map": map_id, "from": start, "to": end}, timeout=timeout)


class PathEndpointTests(ServerCase):
    def test_path_goes_around_a_blocking_wall(self):
        # 5x5 cells of 10 with the centre one removed: the wall is Detour [20, 30]^2, WoW [20, 30]^2.
        self.serve(grid(5, 5, 10, missing=((2, 2),)))
        status, body = self.path(wow_point(5, 25), wow_point(45, 25))
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        points = body["points"]
        self.assertGreater(len(points), 2)
        self.assertEqual((points[0]["x"], points[0]["y"]), (5, 25))
        self.assertEqual((points[-1]["x"], points[-1]["y"]), (45, 25))
        for p in points[1:-1]:
            self.assertFalse(20 < p["x"] < 30 and 20 < p["y"] < 30, f"corner {p} is inside the wall")

    def test_unreachable_target_is_no_path(self):
        self.serve(grid(3, 1, 10, missing=((1, 0),)))   # two islands, Detour x [0, 10] and [20, 30]
        self.assertEqual(self.path(wow_point(5, 5), wow_point(25, 5)),
                         (200, {"status": "no_path", "points": []}))

    def test_map_without_an_mmap_is_no_map(self):
        self.serve(grid(2, 1, 10))
        self.assertEqual(self.path(wow_point(5, 5), wow_point(15, 5), map_id=571),
                         (200, {"status": "no_map", "points": []}))

    def test_a_failed_map_load_is_not_cached(self):
        # The map's files appear after a no_map answer; the next request must load them.
        self.start()
        self.assertEqual(self.path(wow_point(5, 5), wow_point(5, 15))[1]["status"], "no_map")
        self.write_map(grid(2, 1, 10))
        self.assertEqual(self.path(wow_point(5, 5), wow_point(5, 15))[1]["status"], "ok")

    def test_malformed_bodies_are_400_with_an_error(self):
        self.serve(grid(2, 1, 10))
        bad = [
            {"raw": b"{not json"},
            {"raw": b"[1, 2]"},
            {"body": {"from": wow_point(5, 5), "to": wow_point(15, 5)}},            # no map
            {"body": {"map": 530, "from": wow_point(5, 5), "to": {"x": 1, "y": 2}}},  # no z
            {"body": {"map": 530, "from": {"x": 10 ** 400, "y": 5, "z": 0}, "to": wow_point(15, 5)}},  # huge int
            {"body": {"map": 530, "from": wow_point("5", 5), "to": wow_point(15, 5)}},
            {"body": {"map": True, "from": wow_point(5, 5), "to": wow_point(15, 5)}},
            {"body": {"map": 530, "from": wow_point(5, 5), "to": wow_point(float("nan"), 5)}},
        ]
        for case in bad:
            with self.subTest(case=case):
                status, body = self.call("POST", "/path", body=case.get("body"), raw=case.get("raw"))
                self.assertEqual(status, 400)
                self.assertIsInstance(body.get("error"), str)

    def test_unreadable_mmap_is_500_json(self):
        (self.mmaps / "571.mmap").mkdir()   # read_bytes on a directory raises IsADirectoryError
        self.start()
        self.assertEqual(self.path(wow_point(5, 5), wow_point(5, 15), map_id=571),
                         (500, {"error": "internal error"}))

    def test_unknown_path_is_404_json(self):
        self.serve(grid(2, 1, 10))
        status, body = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)


class HeightEndpointTests(ServerCase):
    def test_height_is_the_fixture_terrain(self):
        self.serve(grid(2, 2, 10, height=sloped))
        # WoW (x, y) is Detour (y, x), so the plane is sloped(y, x) at WoW (x, y).
        status, body = self.call("GET", f"/height?map={MAP}&x=7&y=3")
        self.assertEqual(status, 200)
        self.assertAlmostEqual(body["z"], sloped(3, 7), delta=1e-3)

    def test_height_off_the_mesh_is_null(self):
        self.serve(grid(2, 2, 10, height=sloped))
        self.assertEqual(self.call("GET", f"/height?map={MAP}&x=500&y=500"), (200, {"z": None}))

    def test_height_of_an_unknown_map_is_null(self):
        self.serve(grid(2, 2, 10))
        self.assertEqual(self.call("GET", "/height?map=571&x=5&y=5"), (200, {"z": None}))

    def test_height_with_a_bad_query_is_400(self):
        self.serve(grid(2, 2, 10))
        huge = "9" * 400
        for query in ("", f"map={MAP}&x=5", f"map={MAP}&x=abc&y=5", f"map={MAP}&x=5&y=5&z=nan",
                      f"map={MAP}&x={huge}&y=5"):
            with self.subTest(query=query):
                status, body = self.call("GET", f"/height?{query}")
                self.assertEqual(status, 400)
                self.assertIn("error", body)


class LoadTests(ServerCase):
    def test_a_slow_load_of_one_map_does_not_block_a_loaded_map_or_healthz(self):
        self.serve(grid(2, 1, 10))                       # map 530 is loaded by the first request
        self.write_map(grid(2, 1, 10), map_id=531)
        self.assertEqual(self.path(wow_point(5, 5), wow_point(5, 15))[1]["status"], "ok")
        real_load = NavMesh.load
        entered, gate = threading.Event(), threading.Event()

        def slow_load(mmaps_dir, map_id):
            if map_id == 531:
                entered.set()
                gate.wait(10)
            return real_load(mmaps_dir, map_id)

        answer = {}
        loader = threading.Thread(target=lambda: answer.update(
            r=self.path(wow_point(5, 5), wow_point(5, 15), map_id=531)))
        with patch.object(NavMesh, "load", staticmethod(slow_load)):
            try:
                loader.start()
                self.assertTrue(entered.wait(5), "the load of map 531 never started")
                self.assertEqual(self.path(wow_point(5, 5), wow_point(5, 15), timeout=2)[1]["status"], "ok")
                self.assertEqual(self.call("GET", "/healthz", timeout=2),
                                 (200, {"ok": True, "maps_loaded": [MAP]}))
            finally:
                gate.set()
                loader.join(10)
        self.assertEqual(answer["r"][1]["status"], "ok")


class HealthzTests(ServerCase):
    def test_healthz_lists_the_map_after_a_path_call(self):
        self.serve(grid(2, 1, 10))
        self.assertEqual(self.call("GET", "/healthz"), (200, {"ok": True, "maps_loaded": []}))
        self.path(wow_point(5, 5), wow_point(5, 15))
        self.assertEqual(self.call("GET", "/healthz"), (200, {"ok": True, "maps_loaded": [MAP]}))


class MapsTests(ServerCase):
    def test_each_loaded_map_is_capped_at_max_tiles(self):
        self.write_map(grid(2, 1, 10))
        self.assertEqual(Maps(self.mmaps, max_tiles=2).get(MAP).tiles.max_tiles, 2)


class MainEnvTests(ServerCase):
    """main() reads PATHFINDER_MAX_TILES at startup; Server is patched so main returns instead of serving."""

    def run_main(self, max_tiles_env):
        with patch.dict(os.environ, {"MMAPS_DIR": str(self.mmaps)}), \
                patch("logging.basicConfig"), patch("tools.pathfinder.server.Server") as server_cls:
            os.environ.pop("PATHFINDER_MAX_TILES", None)   # patch.dict restores the environment after
            if max_tiles_env is not None:
                os.environ["PATHFINDER_MAX_TILES"] = max_tiles_env
            main()
        return server_cls

    def test_max_tiles_below_one_or_not_an_int_is_refused_at_startup(self):
        for value in ("-1", "0", "abc", "1.5", ""):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as ctx:
                    self.run_main(value)
                self.assertIn("PATHFINDER_MAX_TILES must be a positive integer", str(ctx.exception))

    def test_valid_max_tiles_reaches_the_server_and_the_default_is_64(self):
        self.assertEqual(self.run_main("7").call_args.args[2], 7)
        self.assertEqual(self.run_main(None).call_args.args[2], DEFAULT_MAX_TILES)


if __name__ == "__main__":
    unittest.main()
