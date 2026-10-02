import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402

CHARACTER_ROW = (
    7, "Rubens", 2, 10, 2, 0, 3430, 530,
    10350.301, -6348.671, 31.791, 4.6691, 30,
    19281, 1789510926, 1,
    4231, 1020, 0, 0, 100, 0, 8, 0,
)
INVENTORY_ROWS = [
    (0, 3, 101, 45, "Initiate's Shirt", 1),
    (0, 19, 200, 4496, "Small Brown Pouch", 1),
    (0, 24, 102, 20482, "Torn Wyrm Scale", 6),
    (200, 0, 103, 159, "Refreshing Spring Water", 5),
]

# characters.character_stats: maxhealth, maxpower1..maxpower7 (Player::_SaveStats).
STATS_ROW = (4500, 1100, 1000, 0, 100, 0, 8, 1000)


class FakeCursor:
    stats_rows = []

    def __init__(self):
        self.sql = ""

    def execute(self, sql, args=None):
        self.sql = sql

    def fetchone(self):
        return CHARACTER_ROW

    def fetchall(self):
        if "character_inventory" in self.sql:
            return INVENTORY_ROWS
        if "character_stats" in self.sql:
            return self.stats_rows
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def cursor(self):
        return FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTables:
    def zone_name(self, area_id):
        return "Eversong Woods"

    def map_name(self, map_id):
        return "Expansion01"


class FetchCharacterTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(app, "db", return_value=FakeConnection()),
                   mock.patch.object(app, "tables", return_value=FakeTables())]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.character = app.fetch_character("Rubens")

    def test_power_columns_follow_trinitycore_powers_enum(self):
        self.assertEqual(self.character["health"], 4231)
        self.assertEqual(self.character["power"], {
            "mana": 1020, "rage": 0, "focus": 0, "energy": 100,
            "happiness": 0, "rune": 8, "runic_power": 0,
        })

    def test_header_fields(self):
        c = self.character
        self.assertTrue(c["online"])
        self.assertEqual(c["class_color"], app.CLASS_COLORS[2])
        self.assertEqual(c["map_name"], "Expansion01")
        self.assertEqual(c["money"], 30)
        self.assertAlmostEqual(c["money_gold"], 0.003)

    def test_inventory_exposes_item_guid_to_resolve_bag_contents(self):
        inventory = self.character["inventory"]
        pouch = next(i for i in inventory if i["slot"] == 19)
        water = next(i for i in inventory if i["bag"] != 0)
        self.assertEqual(water["bag"], pouch["item_guid"])
        self.assertEqual(inventory[2], {"bag": 0, "slot": 24, "item_guid": 102, "item_entry": 20482,
                                        "item_name": "Torn Wyrm Scale", "count": 6})

    def test_no_character_stats_row_means_no_max_values(self):
        self.assertIsNone(self.character["max_health"])
        self.assertIsNone(self.character["max_power"])


class CharacterStatsTests(unittest.TestCase):
    def fetch(self, stats_rows):
        with mock.patch.object(app, "db", return_value=FakeConnection()), \
                mock.patch.object(app, "tables", return_value=FakeTables()), \
                mock.patch.object(FakeCursor, "stats_rows", stats_rows):
            return app.fetch_character("Rubens")

    def test_stats_row_gives_max_health_and_max_power_by_name(self):
        c = self.fetch([STATS_ROW])
        self.assertEqual(c["max_health"], 4500)
        self.assertEqual(c["max_power"], {
            "mana": 1100, "rage": 1000, "focus": 0, "energy": 100,
            "happiness": 0, "rune": 8, "runic_power": 1000,
        })
        # Current values still come from characters.characters.
        self.assertEqual(c["health"], 4231)
        self.assertEqual(c["power"]["mana"], 1020)

    def test_missing_table_degrades_to_no_max_values(self):
        class BrokenStatsCursor(FakeCursor):
            def execute(self, sql, args=None):
                super().execute(sql, args)
                if "character_stats" in sql:
                    raise RuntimeError("Table 'characters.character_stats' doesn't exist")

        class Conn(FakeConnection):
            def cursor(self):
                return BrokenStatsCursor()

        with mock.patch.object(app, "db", return_value=Conn()), \
                mock.patch.object(app, "tables", return_value=FakeTables()), \
                self.assertLogs(app.log, "WARNING"):
            c = app.fetch_character("Rubens")
        self.assertIsNone(c["max_health"])
        self.assertIsNone(c["max_power"])
        self.assertEqual(len(c["inventory"]), len(INVENTORY_ROWS))


class PageTests(unittest.TestCase):
    def test_inspect_panel_is_spliced_in(self):
        self.assertNotIn("@inspect-", app.PAGE)
        self.assertIn('id="inspect"', app.PAGE)
        self.assertIn("const Inspect", app.PAGE)

    def test_drawer_draws_bars_from_max_values(self):
        self.assertIn("function meter(", app.PAGE)
        self.assertIn("c.max_health", app.PAGE)
        self.assertIn("c.max_power", app.PAGE)


if __name__ == "__main__":
    unittest.main()
