"""#262: the page routes answer 200 and the page references only assets that exist."""
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import routes  # noqa: E402
from webio import Request  # noqa: E402

STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"
PAGE_ROUTES = ("/", "/index.html")


def get(path):
    return routes.dispatch(Request("GET", path, {}, {}, lambda n: b"", "10.0.0.9"))


class PageSmokeTests(unittest.TestCase):
    def test_every_page_route_is_html_200(self):
        for path in PAGE_ROUTES:
            status, body, ctype, _ = get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(ctype.startswith("text/html"), path)
            self.assertIn("<!doctype html>", body)

    def test_template_is_fully_filled_in(self):
        body = get("/")[1]
        self.assertNotIn("$chat_feed_url", body)
        self.assertIn("window.CHAT_FEED_URL = ", body)

    def test_every_local_reference_is_served_with_the_right_type(self):
        body = get("/")[1]
        refs = re.findall(r'(?:href|src)="(/[^"]*)"', body)
        self.assertTrue(refs)
        for ref in refs:
            status, data, ctype, _ = get(ref)
            self.assertEqual(status, 200, ref)
            self.assertTrue(data, ref)
            want = "text/css" if ref.endswith(".css") else "text/javascript"
            self.assertTrue(ctype.startswith(want), (ref, ctype))

    def test_no_orphan_page_assets(self):
        """Every page css/js in static/ is loaded by index.html (Leaflet is checked above).
        three.min.js is the exception: charview.js fetches it only when a model is shown (#171)."""
        body = get("/")[1]
        for f in STATIC.iterdir():
            if f.suffix in (".css", ".js") and f.name != "three.min.js":
                self.assertIn(f'/static/{f.name}"', body, f"{f.name} is never loaded")

    def test_templates_and_licences_are_not_served(self):
        for path in ("/static/index.html", "/static/leaflet-LICENSE", "/static/nope.js",
                     "/static/../pages.py"):
            self.assertEqual(get(path)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
