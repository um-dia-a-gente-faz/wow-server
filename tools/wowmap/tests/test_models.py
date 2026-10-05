"""#171: character model extraction format, /models route and the vendored three.js."""
import hashlib
import os
import pathlib
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402
import models  # noqa: E402

# three.js r159 build/three.min.js as published on npm, unmodified.
THREE_SHA256 = "7b1c5d75b28d9de15042e2b374f83566d8c7146697af8fdeb4558b0fb528a585"


def fake_m2(n_verts):
    """A minimal 3.3.5a M2: header, vertices, then one texture record."""
    header = bytearray(0x100)
    struct.pack_into("<4sI", header, 0, b"MD20", 264)
    verts = b"".join(struct.pack("<3f4B4B3f2f2f", i, i + 0.5, i + 1, 0, 0, 0, 0, 0, 0, 0, 0,
                                 0, 0, 1, i / 10, i / 20, 0, 0) for i in range(n_verts))
    struct.pack_into("<II", header, 60, n_verts, len(header))
    tex_at = len(header) + len(verts)
    struct.pack_into("<II", header, 80, 1, tex_at)
    return bytes(header) + verts + struct.pack("<IIII", 1, 0, 0, 0)


def fake_skin(lookup, indices, geosets):
    """geosets: [(geoset id, index start, index count)]."""
    head = bytearray(48)
    head[:4] = b"SKIN"
    at = len(head)
    parts = [struct.pack(f"<{len(lookup)}H", *lookup), struct.pack(f"<{len(indices)}H", *indices)]
    subs = b"".join(struct.pack("<6H36x", g, 0, 0, 0, s, c) for g, s, c in geosets)
    struct.pack_into("<II", head, 4, len(lookup), at)
    struct.pack_into("<II", head, 12, len(indices), at + len(parts[0]))
    struct.pack_into("<II", head, 28, len(geosets), at + len(parts[0]) + len(parts[1]))
    return bytes(head) + parts[0] + parts[1] + subs


class MeshTests(unittest.TestCase):
    def setUp(self):
        self.m2 = fake_m2(6)
        # Two triangles for the body (geoset 0), one for a non-default hair style (7).
        self.skin = fake_skin([5, 4, 3, 2, 1, 0], [0, 1, 2, 3, 4, 5, 0, 2, 4], [(0, 0, 6), (7, 6, 3)])

    def test_parses_vertices_and_texture_types(self):
        verts, types = models.parse_m2(self.m2)
        self.assertEqual(len(verts), 6)
        self.assertEqual(verts[2][:2], ((2.0, 2.5, 3.0), (0.0, 0.0, 1.0)))
        self.assertAlmostEqual(verts[2][2][0], 0.2, places=6)
        self.assertAlmostEqual(verts[2][2][1], 0.1, places=6)
        self.assertEqual(types, [1])

    def test_blob_keeps_only_default_geosets_and_round_trips(self):
        verts, idx = models.parse_blob(models.build_blob(self.m2, self.skin))
        self.assertEqual(len(idx), 6)                 # geoset 7 (a hair style) dropped
        self.assertEqual(len(verts), 6)
        self.assertEqual(verts[0][:3], (5.0, 5.5, 6.0))   # lookup[0] = vertex 5
        self.assertEqual(max(idx), 5)

    def test_unreferenced_vertices_are_dropped(self):
        skin = fake_skin([0, 1, 2, 3, 4, 5], [0, 1, 2], [(0, 0, 3)])
        verts, idx = models.parse_blob(models.build_blob(self.m2, skin))
        self.assertEqual((len(verts), idx), (3, (0, 1, 2)))

    def test_rejects_other_versions_and_bad_files(self):
        with self.assertRaises(ValueError):
            models.parse_m2(b"MD21" + bytes(300))
        with self.assertRaises(ValueError):
            models.parse_skin(b"XXXX" + bytes(60))
        with self.assertRaises(ValueError):
            models.build_blob(self.m2, fake_skin([0], [9], [(0, 0, 1)]))   # index past the lookup
        with self.assertRaises(ValueError):
            models.build_blob(self.m2, fake_skin([0, 1, 2], [0, 1, 2], [(5, 0, 3)]))  # nothing visible
        with self.assertRaises(ValueError):
            models.parse_blob(b"WMDL" + bytes(12))

    def test_default_geosets(self):
        keep = [0, 1, 101, 401, 501, 1301, 1501]
        drop = [2, 7, 18, 102, 402, 502, 803, 1502, 1703]
        self.assertTrue(all(models.default_geoset(g) for g in keep))
        self.assertFalse(any(models.default_geoset(g) for g in drop))

    def test_keys_and_paths(self):
        self.assertEqual(models.model_key(1, 0), "1_0")
        self.assertEqual(models.model_key(10, 1), "10_1")
        self.assertIsNone(models.model_key(9, 0))      # goblins are not playable
        self.assertIsNone(models.model_key(1, 2))
        self.assertEqual(models.mpq_paths(1, 0), ("Character\\Human\\Male\\HumanMale.M2",
                                                  "Character\\Human\\Male\\HumanMale00.skin"))

    def test_base_skin_is_type_0_variation_0_colour_0(self):
        def dbc(rows, strings):
            return struct.pack("<4sIIII", b"WDBC", len(rows), 10, 40, len(strings)) + \
                b"".join(struct.pack("<10I", *r) for r in rows) + strings
        skin_s = b"Character\\Human\\Male\\Skin.blp\0"
        strings = b"\0" + skin_s + b"Character\\Human\\Male\\Face.blp\0"
        skin, face = 1, 1 + len(skin_s)
        rows = [(1, 1, 0, 1, face, 0, 0, 0, 0, 0),    # a face section, not the body
                (2, 1, 0, 0, skin, 0, 0, 0, 0, 3),    # another colour
                (3, 1, 0, 0, skin, 0, 0, 0, 0, 0),
                (4, 1, 1, 0, face, 0, 0, 0, 0, 0)]    # female
        self.assertEqual(models.base_skin_paths(dbc(rows, strings), 1, 0), "Character\\Human\\Male\\Skin.blp")
        self.assertIsNone(models.base_skin_paths(dbc(rows, strings), 2, 0))
        with self.assertRaises(ValueError):
            models.base_skin_paths(b"WDBC" + struct.pack("<IIII", 0, 9, 36, 0), 1, 0)


class ModelRouteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        old = app.MODELS_DIR
        app.MODELS_DIR = self.dir.name
        self.addCleanup(setattr, app, "MODELS_DIR", old)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def put(self, name, data=b"x"):
        with open(os.path.join(self.dir.name, name), "wb") as f:
            f.write(data)

    def test_empty_models_dir_means_no_model(self):
        self.assertIsNone(app.model_url(1, 0))
        self.assertEqual(self.get("/models/1_0.bin")[0], 404)

    def test_model_needs_both_mesh_and_skin(self):
        self.put("1_0.bin")
        self.assertIsNone(app.model_url(1, 0))
        self.put("1_0.png")
        self.assertEqual(app.model_url(1, 0), "/models/1_0")
        self.assertIsNone(app.model_url(1, 1))
        self.assertIsNone(app.model_url(9, 0))

    def test_serves_mesh_and_skin(self):
        self.put("1_0.bin", b"WMDL")
        self.put("1_0.png", b"\x89PNG")
        status, headers, body = self.get("/models/1_0.bin")
        self.assertEqual((status, body), (200, b"WMDL"))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(self.get("/models/1_0.png")[1]["Content-Type"], "image/png")

    def test_only_model_files_are_reachable(self):
        self.put("notes.txt")
        self.put("1_0.bin")
        for path in ("/models/notes.txt", "/models/", "/models/../app.py", "/models/..%2Fapp.py",
                     "/models/1_0.bin/x", "/models/1_0"):
            self.assertEqual(self.get(path)[0], 404, path)


class VendoredViewerTests(unittest.TestCase):
    def test_three_js_is_vendored_unmodified_and_licensed(self):
        d = pathlib.Path(app.STATIC_DIR)
        self.assertEqual(hashlib.sha256((d / "three.min.js").read_bytes()).hexdigest(), THREE_SHA256)
        self.assertIn("MIT License", (d / "three-LICENSE").read_text())
        self.assertLess((d / "three.min.js").stat().st_size, 1_000_000)

    def test_page_loads_nothing_remote_and_three_only_on_demand(self):
        self.assertIn('<script src="/static/charview.js"></script>', app.PAGE)
        self.assertNotIn("three.min.js", app.PAGE)          # lazy: fetched when a model is shown
        text = (pathlib.Path(app.STATIC_DIR) / "charview.js").read_text()
        self.assertIn("/static/three.min.js", text)
        for remote in ("http://", "https://", "//cdn", "unpkg", "jsdelivr"):
            self.assertNotIn(remote, text, remote)

    def test_drawer_has_a_placeholder_for_a_missing_model(self):
        self.assertIn("No 3D model extracted", app.PAGE)

    def test_static_routes_serve_the_viewer(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for name in ("three.min.js", "charview.js"):
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/static/{name}") as r:
                r.read()
                self.assertEqual(r.status, 200, name)
                self.assertTrue(r.headers["Content-Type"].startswith("text/javascript"), name)


if __name__ == "__main__":
    unittest.main()
