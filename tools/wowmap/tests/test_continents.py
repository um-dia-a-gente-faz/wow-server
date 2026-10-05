"""Continent maps (UM-78), on synthetic DBCs holding the real 3.3.5a WorldMapArea rows
for Kalimdor, Eastern Kingdoms, Northrend, Durotar, Eversong Woods and Hrothgar's Landing."""
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402
import pagesrc  # noqa: E402
import areas  # noqa: E402
import players  # noqa: E402
import state  # noqa: E402
from test_position import f32, write_dbc  # noqa: E402
from transform import DbcTables  # noqa: E402

try:
    import extract_maps  # needs Pillow
except ImportError:  # pragma: no cover
    extract_maps = None

NONE = 0xFFFFFFFF
RAZOR_HILL = (1, 300.0, -4700.0)        # map, world X, world Y: Durotar's east coast


def wma(row_id, map_id, area_id, name, y1, y2, x1, x2, display=NONE):
    """WorldMapArea: ID, MapID, AreaID, AreaName, fields 4/5 (world Y), 6/7 (world X), DisplayMapID."""
    return [row_id, map_id, area_id, name, f32(y1), f32(y2), f32(x1), f32(x2), display]


class ContinentCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = pathlib.Path(cls.tmp.name)
        write_dbc(cls.dir / "WorldMapArea.dbc", 11, [
            wma(4, 1, 14, "Durotar", -1962.5, -7250.0, 1808.333, -1716.667),
            wma(13, 1, 0, "Kalimdor", 17066.6, -19733.211, 12799.9, -11733.3),
            wma(14, 0, 0, "Azeroth", 18171.971, -22569.211, 11176.344, -15973.344),
            wma(485, 571, 0, "Northrend", 9217.152, -8534.246, 10593.375, -1240.89),
            wma(462, 530, 3430, "EversongWoods", -4487.5, -9412.5, 11041.666, 7758.333, 0),
            wma(541, 571, 4742, "HrothgarsLanding", 2797.917, -879.167, 10781.25, 8329.166),
        ])
        area = lambda aid, map_id, name: [aid, map_id, 0] + [0] * 8 + [name]  # noqa: E731
        write_dbc(cls.dir / "AreaTable.dbc", 36, [
            area(14, 1, "Durotar"), area(3430, 530, "Eversong Woods"),
            area(4742, 571, "Hrothgar's Landing"),
        ])
        write_dbc(cls.dir / "Map.dbc", 66, [
            [0, "Azeroth", 0, 0, 0, "Eastern Kingdoms"],
            [1, "Kalimdor", 0, 0, 0, "Kalimdor"],
            [530, "Expansion01", 0, 0, 0, "Outland"],
            [571, "Northrend", 0, 0, 0, "Northrend"],
        ])
        cls.t = DbcTables(str(cls.dir))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TransformTests(ContinentCase):
    def test_continents_are_the_area_zero_rows_by_map(self):
        self.assertEqual(sorted(self.t.continent_rects), [0, 1, 571])
        self.assertEqual(self.t.continent_dirs[0], "Azeroth")
        self.assertNotIn(0, self.t.zone_map)

    def test_position_on_the_continent_uses_the_client_axes(self):
        nx, ny = self.t.continent_normalised(*RAZOR_HILL)
        self.assertAlmostEqual(nx, 0.5915, places=4)
        self.assertAlmostEqual(ny, 0.5095, places=4)
        # North (world X up) is up, west (world Y up) is left, as on a zone map.
        self.assertLess(self.t.continent_normalised(1, 400.0, -4700.0)[1], ny)
        self.assertLess(self.t.continent_normalised(1, 300.0, -4600.0)[0], nx)
        self.assertIsNone(self.t.continent_normalised(36, 0.0, 0.0))

    def test_zone_box_is_the_zone_rect_in_the_continent_frame(self):
        x0, y0, x1, y1 = self.t.zone_box(14)
        self.assertAlmostEqual(x0 * 1002, 518.1, places=1)
        self.assertAlmostEqual(y0 * 668, 299.3, places=1)
        self.assertAlmostEqual((x1 - x0) * 1002, 144.0, places=1)
        self.assertAlmostEqual((y1 - y0) * 668, 96.0, places=1)
        self.assertEqual(self.t.zone_continent(14), 1)

    def test_display_map_zone_is_on_a_continent_without_a_box(self):
        # Eversong is on map 530 but drawn on Eastern Kingdoms; its rect is map 530 space.
        self.assertEqual(self.t.zone_continent(3430), 0)
        self.assertIsNone(self.t.zone_box(3430))

    def test_rect_outside_the_frame_has_no_box(self):
        self.assertEqual(self.t.zone_continent(4742), 571)
        self.assertIsNone(self.t.zone_box(4742))


class AppTests(ContinentCase):
    def setUp(self):
        maps = tempfile.TemporaryDirectory()
        self.addCleanup(maps.cleanup)
        (pathlib.Path(maps.name) / "continent_1.png").write_bytes(b"png")
        for p in (mock.patch.object(state, "tables", return_value=self.t),
                  mock.patch.object(state, "overlays", return_value={}),
                  mock.patch.object(state, "MAPS_DIR", maps.name)):
            p.start()
            self.addCleanup(p.stop)

    def test_continents_list_their_zones(self):
        by_id = {c["area_id"]: c for c in areas.fetch_continents()}
        self.assertEqual(sorted(by_id), ["c0", "c1", "c571"])
        kalimdor = by_id["c1"]
        self.assertTrue(kalimdor["continent_view"])
        self.assertEqual((kalimdor["name"], kalimdor["image"], kalimdor["has_image"]),
                         ("Kalimdor", "continent_1.png", True))
        self.assertEqual(kalimdor["zones"],
                         [{"area_id": 14, "name": "Durotar", "box": [518.1, 299.3, 144.0, 96.0]}])
        self.assertFalse(by_id["c0"]["has_image"])
        self.assertEqual(by_id["c0"]["zones"],
                         [{"area_id": 3430, "name": "Eversong Woods", "box": None}])

    def test_zone_list_leaves_the_continent_rows_out(self):
        self.assertEqual(sorted(a["area_id"] for a in areas.fetch_areas()), [14, 3430, 4742])

    def test_continent_position(self):
        self.assertEqual(players.continent_position(self.t, 1, 14, 300.0, -4700.0)[0], 1)
        # Eversong (display map), an instance map and a point off the frame have none.
        self.assertIsNone(players.continent_position(self.t, 530, 3430, 10337.1, -6359.9))
        self.assertIsNone(players.continent_position(self.t, 36, 1581, 0.0, 0.0))
        self.assertIsNone(players.continent_position(self.t, 1, 14, 99999.0, 0.0))

    def test_page_has_the_navigation(self):
        for needle in ('id="toContinent"', "function zoneAt", "function showArea",
                       "map.on('contextmenu', zoomOut)"):
            self.assertIn(needle, pagesrc.PAGE)


@unittest.skipIf(extract_maps is None, "Pillow not installed")
class ExtractTests(ContinentCase):
    def test_continent_rows_are_keyed_by_map(self):
        self.assertEqual(extract_maps.continents(str(self.dir)),
                         [(0, "Azeroth"), (1, "Kalimdor"), (571, "Northrend")])


if __name__ == "__main__":
    unittest.main()
