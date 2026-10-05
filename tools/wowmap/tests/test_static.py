"""UM-72: the vendored Leaflet files under /static/ and the Leaflet map stage."""
import hashlib
import os
import pathlib
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402
import assets  # noqa: E402

# Leaflet 1.9.4 as published on npm (dist/leaflet.js, dist/leaflet.css), unmodified.
LEAFLET_SHA256 = {
    "leaflet.js": "db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a",
    "leaflet.css": "a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6",
}


class StaticRouteTests(unittest.TestCase):
    def setUp(self):
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

    def test_serves_the_vendored_leaflet_files_unmodified(self):
        for name, ctype in (("leaflet.js", "text/javascript"), ("leaflet.css", "text/css")):
            status, headers, body = self.get("/static/" + name)
            self.assertEqual(status, 200, name)
            self.assertTrue(headers["Content-Type"].startswith(ctype), name)
            self.assertIn("max-age", headers["Cache-Control"])
            self.assertEqual(hashlib.sha256(body).hexdigest(), LEAFLET_SHA256[name], name)

    def test_only_listed_files_are_reachable(self):
        self.assertTrue(os.path.isfile(os.path.join(assets.STATIC_DIR, "leaflet-LICENSE")))
        for path in ("/static/leaflet-LICENSE", "/static/", "/static/../app.py",
                     "/static/..%2Fapp.py", "/static/%2e%2e/calibration.json",
                     "/static/leaflet.js/x"):
            self.assertEqual(self.get(path)[0], 404, path)


class PageTests(unittest.TestCase):
    def test_page_loads_leaflet_from_this_server(self):
        self.assertIn('<link rel="stylesheet" href="/static/leaflet.css">', app.PAGE)
        self.assertIn('<script src="/static/leaflet.js"></script>', app.PAGE)
        self.assertNotIn("unpkg.com", app.PAGE)
        self.assertNotIn("cdnjs", app.PAGE)

    def test_stage_is_a_simple_crs_leaflet_map(self):
        self.assertIn('<div id="map"></div>', app.PAGE)
        self.assertIn("crs: L.CRS.Simple", app.PAGE)
        self.assertIn("function fitZone()", app.PAGE)
        for gone in ('id="mapimg"', 'id="wrap"', "stageScale"):
            self.assertNotIn(gone, app.PAGE)


class StatIconTests(unittest.TestCase):
    """#176: health/mana/gold/played/logout icons are an inline SVG sprite, no fetch."""
    NAMES = ("health", "mana", "gold", "played", "logout")

    def sprite(self):
        m = re.search(r'<svg id="stat-icons".*?</svg>', app.PAGE, re.S)
        self.assertIsNotNone(m, "inline icon sprite missing")
        return m.group(0)

    def test_every_stat_icon_is_an_inline_symbol(self):
        sprite = self.sprite()
        for n in self.NAMES:
            self.assertIn(f'<symbol id="i-{n}"', sprite, n)

    def test_sprite_makes_no_network_request(self):
        sprite = self.sprite()
        for ref in ("http", "url(", "<image", "xlink:href", "@import"):
            self.assertNotIn(ref, sprite)
        self.assertIn("`#i-${name}`", app.PAGE)  # icons only reference the inline sprite

    def test_icons_are_labelled(self):
        self.assertIn("setAttribute('aria-label', label)", app.PAGE)
        self.assertIn("setAttribute('role', 'img')", app.PAGE)


if __name__ == "__main__":
    unittest.main()
