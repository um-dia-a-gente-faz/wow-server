"""UM-79: fog of war — explore bits, revealed overlays, composed art, API and routes."""
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402
import pagesrc  # noqa: E402
import areas  # noqa: E402
import fogview  # noqa: E402
import state  # noqa: E402
import fog  # noqa: E402

try:
    from PIL import Image
    import extract_maps
except ImportError:  # pragma: no cover
    Image = extract_maps = None

# Eversong Woods (WorldMapArea 462, area 3430). Explore bits are the real ones from
# the 12340 AreaTable.dbc (field 3).
AREA_BITS = {3430: 1581, 3431: 1582, 3487: 1111, 3533: 1157, 3470: 1096}


def overlay(oid, area_ids, texture="Tex", size=(4, 4), offset=(0, 0)):
    return {"id": oid, "map_area_id": 462, "area_ids": area_ids, "texture": texture,
            "width": size[0], "height": size[1], "offset_x": offset[0], "offset_y": offset[1],
            "hit": None}


OVERLAYS = [
    overlay(1127, [3431], "SunstriderIsle", (4, 4), (2, 1)),
    overlay(1128, [3533, 3470], "RuinsofSilvermoon", (3, 2), (5, 5)),
    overlay(1175, [3487], "SilvermoonCity", (2, 2), (4, 3)),
    overlay(1384, [3470], "", (0, 0)),                          # hit rect only, no art
]


def explored_zones(bits):
    """What Player::SaveToDB writes: 128 uint32 values, each followed by a space."""
    values = [0] * 128
    for bit in bits:
        values[bit // 32] |= 1 << (bit % 32)
    return "".join(f"{v} " for v in values)


class ExploredBitsTests(unittest.TestCase):
    def test_bit_index_is_value_index_times_32_plus_bit(self):
        self.assertEqual(fog.explored_bits("1 0 2147483648 "), {0, 95})
        self.assertEqual(fog.explored_bits("0 6"), {33, 34})

    def test_round_trips_the_server_format(self):
        bits = {0, 31, 32, 1111, 1581, 1582, 3617, 4095}
        self.assertEqual(fog.explored_bits(explored_zones(bits)), bits)

    def test_empty_null_and_fresh_characters(self):
        self.assertEqual(fog.explored_bits(None), set())
        self.assertEqual(fog.explored_bits(""), set())
        self.assertEqual(fog.explored_bits(explored_zones(())), set())

    def test_bad_tokens_count_as_zero_and_extra_values_are_ignored(self):
        self.assertEqual(fog.explored_bits("x 1 -1 4294967296 2"), {32, 129})
        self.assertEqual(fog.explored_bits("0 " * 128 + "1 "), set())


class RevealedTests(unittest.TestCase):
    def test_overlay_is_revealed_by_any_of_its_areas(self):
        bits = {AREA_BITS[3431], AREA_BITS[3470]}
        self.assertEqual(fog.revealed(OVERLAYS, AREA_BITS, bits), [1127, 1128, 1384])

    def test_nothing_explored_and_unknown_areas(self):
        self.assertEqual(fog.revealed(OVERLAYS, AREA_BITS, set()), [])
        self.assertEqual(fog.revealed([overlay(1, [424242])], AREA_BITS, {0, 1}), [])

    def test_the_zone_s_own_bit_reveals_no_overlay(self):
        self.assertEqual(fog.revealed(OVERLAYS, AREA_BITS, {AREA_BITS[3430]}), [])


class FakeTables:
    """WorldMapArea row 462 (Eversong Woods, map 530)."""

    _wm = [(462, 530, 3430, 1, 0, 0, 0, 0, 0, 0, 0)]
    rects = {3430: (12996.0, 9466.0, -4950.0, -7250.0)}
    area_names = {}
    area_bits = AREA_BITS

    def zone_name(self, area_id):
        return "Eversong Woods"

    def map_name(self, map_id):
        return "Expansion01"


class FakeDb:
    """Stands in for state.db(): one characters row, or none."""

    def __init__(self, row):
        self.row = row
        self.executed = []

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql, args=()):
        self.executed.append((sql, args))

    def fetchone(self):
        return self.row


class AppCase(unittest.TestCase):
    def patch_app(self, row=None, maps_dir="/nonexistent"):
        self.db = FakeDb(row)
        for p in (mock.patch.object(state, "tables", return_value=FakeTables()),
                  mock.patch.object(state, "overlays", return_value={462: OVERLAYS}),
                  mock.patch.object(state, "db", self.db),
                  mock.patch.object(state, "MAPS_DIR", maps_dir)):
            p.start()
            self.addCleanup(p.stop)


class FetchExploredTests(AppCase):
    def test_lists_revealed_overlays_per_zone_area_id(self):
        self.patch_app(("Rubens", explored_zones({AREA_BITS[3430], AREA_BITS[3431], AREA_BITS[3487]})))
        self.assertEqual(fogview.fetch_explored("rubens"),
                         {"name": "Rubens", "explored_bits": 3, "zones": {"3430": [1127, 1175]}})
        sql, args = self.db.executed[0]
        self.assertIn("%s", sql)
        self.assertEqual(args, ("rubens",))

    def test_fresh_character_has_no_zones_and_unknown_is_none(self):
        self.patch_app(("Fresh", explored_zones(())))
        self.assertEqual(fogview.fetch_explored("Fresh"),
                         {"name": "Fresh", "explored_bits": 0, "zones": {}})
        self.patch_app(None)
        self.assertIsNone(fogview.fetch_explored("Nobody"))


class ArtCase(AppCase):
    """A maps directory with a tiny base sheet and one flat-colour PNG per overlay."""

    BASE = (10, 10, 10, 255)
    COLOURS = {1127: (255, 0, 0, 255), 1128: (0, 255, 0, 255), 1175: (0, 0, 255, 255)}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.maps = self.tmp.name
        os.mkdir(os.path.join(self.maps, fog.OVERLAY_DIR))
        Image.new("RGBA", (16, 12), self.BASE).save(os.path.join(self.maps, "3430_base.png"))
        for o in OVERLAYS:
            if o["texture"]:
                Image.new("RGBA", (o["width"], o["height"]), self.COLOURS[o["id"]]).save(
                    fog.overlay_path(self.maps, o["id"]))
        fog._cache.clear()
        self.patch_app(maps_dir=self.maps)

    def pixels(self, data):
        return Image.open(io.BytesIO(data)).convert("RGBA")


@unittest.skipIf(Image is None, "Pillow not installed")
class ComposeTests(ArtCase):
    def test_only_the_listed_overlays_are_drawn_at_their_offsets(self):
        image = self.pixels(fog.compose(self.maps, 3430, OVERLAYS, [1127]))
        self.assertEqual(image.size, (16, 12))
        self.assertEqual(image.getpixel((2, 1)), self.COLOURS[1127])
        self.assertEqual(image.getpixel((1, 1)), self.BASE)
        self.assertEqual(image.getpixel((6, 5)), self.BASE)           # 1128 not revealed

    def test_overlays_stack_in_dbc_order_whatever_the_id_order(self):
        # 1127 covers (2..5, 1..4), 1175 covers (4..5, 3..4): the later row wins there.
        for ids in ([1127, 1175], [1175, 1127]):
            image = self.pixels(fog.compose(self.maps, 3430, OVERLAYS, ids))
            self.assertEqual(image.getpixel((4, 3)), self.COLOURS[1175])
            self.assertEqual(image.getpixel((3, 3)), self.COLOURS[1127])

    def test_translucent_art_blends_with_the_base(self):
        Image.new("RGBA", (4, 4), (255, 255, 255, 128)).save(fog.overlay_path(self.maps, 1127))
        r, g, b, a = self.pixels(fog.compose(self.maps, 3430, OVERLAYS, [1127])).getpixel((2, 1))
        self.assertEqual(a, 255)
        self.assertTrue(120 < r < 140, r)

    def test_unknown_textureless_and_missing_overlays_are_skipped(self):
        os.remove(fog.overlay_path(self.maps, 1128))
        image = self.pixels(fog.compose(self.maps, 3430, OVERLAYS, [1128, 1384, 99999]))
        self.assertEqual(image.getpixel((6, 5)), self.BASE)

    def test_missing_base_art_is_none(self):
        self.assertIsNone(fog.compose(self.maps, 14, OVERLAYS, [1127]))

    def test_result_is_cached_until_the_base_art_changes(self):
        first = fog.compose(self.maps, 3430, OVERLAYS, [1127])
        with mock.patch("PIL.Image.open", side_effect=AssertionError("recomposed")):
            self.assertIs(fog.compose(self.maps, 3430, OVERLAYS, [1127]), first)
        base = os.path.join(self.maps, "3430_base.png")
        Image.new("RGBA", (16, 12), (99, 99, 99, 255)).save(base)
        os.utime(base, ns=(1, 1))
        image = self.pixels(fog.compose(self.maps, 3430, OVERLAYS, [1127]))
        self.assertEqual(image.getpixel((0, 0)), (99, 99, 99, 255))

    def test_cache_is_bounded(self):
        for n in range(fog.CACHE_SIZE + 5):
            fog.compose(self.maps, 3430, OVERLAYS + [overlay(5000 + n, [1], "X")], [1127])
        self.assertLessEqual(len(fog._cache), fog.CACHE_SIZE)

    def test_has_art_needs_the_base_and_every_textured_overlay(self):
        self.assertTrue(fog.has_art(self.maps, 3430, OVERLAYS))
        self.assertFalse(fog.has_art(self.maps, 3430, []))                 # e.g. a city map
        self.assertFalse(fog.has_art(self.maps, 14, OVERLAYS))             # no base art
        os.remove(fog.overlay_path(self.maps, 1175))
        self.assertFalse(fog.has_art(self.maps, 3430, OVERLAYS))

    def test_areas_report_whether_fog_art_exists(self):
        Image.new("RGBA", (16, 12), self.BASE).save(os.path.join(self.maps, "3430.png"))
        (eversong,) = areas.fetch_areas(530)
        self.assertTrue(eversong["fog"])
        os.remove(fog.overlay_path(self.maps, 1175))
        self.assertFalse(areas.fetch_areas(530)[0]["fog"])

    def test_extraction_writes_each_overlay_s_own_art(self):
        class Archive:
            def read_file(self, path):
                return path

        sheet = Image.new("RGBA", (1024, 768), (0, 0, 0, 255))
        tile = Image.new("RGBA", (256, 256), (0, 255, 0, 255))
        row = overlay(1143, [3468], "TorWatha", (256, 353), (648, 315))
        out_dir = os.path.join(self.maps, "written")
        os.mkdir(out_dir)
        with mock.patch.object(extract_maps, "read_blp", return_value=tile):
            full, drawn, missing = extract_maps.composite_overlays(
                sheet, Archive(), "EversongWoods", [row, OVERLAYS[3]], save_dir=out_dir)
        self.assertEqual((drawn, missing), (2, 0))
        self.assertEqual(os.listdir(out_dir), ["1143.png"])
        with Image.open(os.path.join(out_dir, "1143.png")) as art:
            self.assertEqual(art.size, (256, 353))
            self.assertEqual(art.getpixel((0, 352)), (0, 255, 0, 255))
        # stacking the written art on the base gives the same image as the full composite
        Image.new("RGBA", (1024, 768), (0, 0, 0, 255)).save(os.path.join(out_dir, "7_base.png"))
        os.mkdir(os.path.join(out_dir, fog.OVERLAY_DIR))
        os.rename(os.path.join(out_dir, "1143.png"), fog.overlay_path(out_dir, 1143))
        again = self.pixels(fog.compose(out_dir, 7, [row], [1143]))
        self.assertEqual(again.tobytes(), full.tobytes())


@unittest.skipIf(Image is None, "Pillow not installed")
class RouteTests(ArtCase):
    def setUp(self):
        super().setUp()
        self.db.row = ("Rubens", explored_zones({AREA_BITS[3431]}))
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

    def test_explored_endpoint(self):
        status, headers, body = self.get("/api/character/Rubens/explored")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(body),
                         {"name": "Rubens", "explored_bits": 1, "zones": {"3430": [1127]}})
        self.db.row = None
        self.assertEqual(self.get("/api/character/Nobody/explored")[0], 404)
        self.assertEqual(self.get("/api/character//explored")[0], 404)

    def test_map_art_with_explored_overlays(self):
        status, headers, body = self.get("/maps/3430.png?explored=1127,1175")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(self.pixels(body).getpixel((4, 3)), self.COLOURS[1175])

    def test_map_art_rejects_bad_queries(self):
        for query in ("abc", "1127;x", "../1127", ",".join(str(n) for n in range(200))):
            self.assertEqual(self.get("/maps/3430.png?explored=" + query)[0], 404, query)
        self.assertEqual(self.get("/maps/99.png?explored=1127")[0], 404)       # no such zone art

    def test_plain_map_files_are_served_as_before(self):
        status, _, body = self.get("/maps/3430_base.png")
        self.assertEqual(status, 200)
        self.assertEqual(self.pixels(body).getpixel((2, 1)), self.BASE)


class PageTests(unittest.TestCase):
    def test_page_has_the_fog_toggle_and_art_switch(self):
        self.assertIn('id="tglFog"', pagesrc.PAGE)
        self.assertIn("function artUrl(a)", pagesrc.PAGE)
        self.assertIn("/explored`", pagesrc.PAGE)


if __name__ == "__main__":
    unittest.main()
