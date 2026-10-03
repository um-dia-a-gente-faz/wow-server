import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402
from dbc.names import GameNames  # noqa: E402

CHARACTER_ROW = (
    7, "Rubens", 2, 10, 2, 0, 3430, 530,
    10350.301, -6348.671, 31.791, 4.6691, 30,
    19281, 1789510926, 1,
    4231, 1020, 0, 0, 100, 0, 8, 0,
)
# bag, slot, item_guid, itemEntry, name, count, item_template.displayid, Quality
INVENTORY_ROWS = [
    (0, 3, 101, 45, "Initiate's Shirt", 1, 36789, 0),
    (0, 19, 200, 4496, "Small Brown Pouch", 1, 1168, 1),
    (0, 24, 102, 20482, "Torn Wyrm Scale", 6, 26375, 0),
    (200, 0, 103, 159, "Refreshing Spring Water", 5, None, None),
]
TALENT_ROWS = [(12663, 0), (20262, 1), (99999, 0)]
# faction, standing, flags (ReputationFlags: 0x01 Visible, 0x10 Peaceful, 0x06 AtWar|Hidden)
REPUTATION_ROWS = [(911, 500, 17), (72, 0, 6), (4242, -100, 0)]
ACHIEVEMENT_ROWS = [(6, 1789000000), (7777, 1788000000)]


def fake_names():
    """A real GameNames with hand-filled tables (no DBC files needed)."""
    with mock.patch("dbc.names.log"):
        n = GameNames("/nonexistent")
    n.spells = {12663: ("Improved Heroic Strike", "Rank 2", 132),
                20262: ("Divine Strength", "Rank 1", 1)}
    n.talent_tabs = {161: ("Arms", 1, 0), 383: ("Protection", 2, 1)}
    n.talent_spells = {12663: (124, 161, 2), 20262: (2185, 383, 1)}
    # race 10 (Blood Elf) is bit 9 = 512 of the Horde mask 690
    n.factions = {911: ("Silvermoon City", 4, (690, 1101, 0, 0), (0, 0, 0, 0),
                        (4000, -42000, 0, 0), 1118),
                  72: ("Stormwind", 7, (1101, 690, 0, 0), (0, 0, 0, 0),
                       (4000, -42000, 0, 0), 469)}
    n.achievements = {6: ("Level 10", 10, 92, -1)}
    return n

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
        for table, rows in (("character_inventory", INVENTORY_ROWS),
                            ("character_talent", TALENT_ROWS),
                            ("character_reputation", REPUTATION_ROWS),
                            ("character_achievement", ACHIEVEMENT_ROWS),
                            ("character_stats", self.stats_rows)):
            if table in self.sql:
                return rows
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

    def continent_name(self, map_id, zone_id=None):
        return "Eastern Kingdoms" if (map_id, zone_id) == (530, 3430) else "Outland"

    def game_coords(self, area_id, world_x, world_y):
        return (37.84, 23.16)

    area_parent = {3431: 3430}


class FakeGridAreas:
    def area_id(self, map_id, world_x, world_y):
        return 3431


class FetchCharacterTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(app, "db", return_value=FakeConnection()),
                   mock.patch.object(app, "tables", return_value=FakeTables()),
                   mock.patch.object(app, "names", return_value=fake_names()),
                   mock.patch.object(app, "grid_areas", return_value=FakeGridAreas()),
                   mock.patch.object(app, "icon_url", lambda d: f"/icons/{d}.png" if d else None)]
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

    def test_position_block(self):
        c = self.character
        self.assertEqual(c["continent_name"], "Eastern Kingdoms")
        self.assertEqual(c["zone_name"], "Eversong Woods")
        self.assertEqual(c["subzone"], 3431)
        self.assertEqual(c["map_coords"], {"x": 37.8, "y": 23.2})
        self.assertEqual((c["position_x"], c["position_y"], c["position_z"]),
                         (10350.3, -6348.67, 31.79))

    def test_subzone_must_belong_to_the_saved_zone(self):
        t = FakeTables()
        with mock.patch.object(app, "grid_areas", return_value=FakeGridAreas()):
            self.assertIsNone(app.position_fields(t, 530, 3433, 1.0, 2.0)["subzone"])
            self.assertIsNone(app.position_fields(t, 530, 3431, 1.0, 2.0)["subzone"])

    def test_inventory_exposes_item_guid_to_resolve_bag_contents(self):
        inventory = self.character["inventory"]
        pouch = next(i for i in inventory if i["slot"] == 19)
        water = next(i for i in inventory if i["bag"] != 0)
        self.assertEqual(water["bag"], pouch["item_guid"])
        self.assertEqual(inventory[2], {"bag": 0, "slot": 24, "item_guid": 102, "item_entry": 20482,
                                        "item_name": "Torn Wyrm Scale", "count": 6,
                                        "quality": 0, "icon": "/icons/26375.png"})

    def test_inventory_icon_and_quality(self):
        inventory = self.character["inventory"]
        self.assertEqual(inventory[1]["quality"], 1)
        self.assertEqual(inventory[1]["icon"], "/icons/1168.png")
        # An item missing from item_template has no display id: no icon, no quality.
        self.assertIsNone(inventory[3]["icon"])
        self.assertIsNone(inventory[3]["quality"])

    def test_talents_carry_name_tree_and_rank(self):
        talents = self.character["talents"]
        self.assertEqual(talents[0], {"spell": 12663, "spec": 0, "name": "Improved Heroic Strike",
                                      "tree": "Arms", "tree_order": 0, "rank": 2})
        self.assertEqual((talents[1]["tree"], talents[1]["spec"]), ("Protection", 1))
        self.assertEqual(talents[2], {"spell": 99999, "spec": 0, "name": None, "tree": None,
                                      "tree_order": None, "rank": None})

    def test_reputation_adds_race_base_and_tier(self):
        reps = {r["faction"]: r for r in self.character["reputation"]}
        self.assertEqual(reps[911], {"faction": 911, "standing": 500, "flags": 17,
                                     "faction_name": "Silvermoon City",
                                     "value": 4500, "tier": "Friendly"})
        self.assertEqual((reps[72]["value"], reps[72]["tier"]), (-42000, "Hated"))
        self.assertEqual(reps[4242], {"faction": 4242, "standing": -100, "flags": 0,
                                      "faction_name": None,
                                      "value": -100, "tier": "Unfriendly"})

    def test_reputation_panel_lists_only_visible_factions(self):
        # 911 is visible; Stormwind (hidden) and the unflagged unknown id are not.
        self.assertEqual(self.character["reputation_panel"], [
            {"faction": 911, "name": "Silvermoon City", "header": False,
             "rep": {"value": 4500, "rank": "Friendly", "rank_id": 5, "bar_value": 1500,
                     "bar_max": 6000, "at_war": False}}])

    def test_achievements_carry_name_and_points(self):
        achs = self.character["achievements"]
        self.assertEqual(achs[0], {"achievement": 6, "date": 1789000000,
                                   "name": "Level 10", "points": 10})
        self.assertEqual(achs[1], {"achievement": 7777, "date": 1788000000,
                                   "name": None, "points": None})

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

    def test_reputation_section_draws_the_panel_with_game_colours(self):
        self.assertIn("reputationSection(c.reputation_panel", app.PAGE)
        # FACTION_BAR_COLORS (FrameXML ReputationFrame.lua): Neutral 0.9/0.7/0,
        # Friendly..Exalted 0/0.6/0.1, Hated/Hostile 0.8/0.3/0.22, Unfriendly 0.75/0.27/0.
        for colour in ("4: '#e6b300'", "5: '#00991a'", "1: '#cc4d38'", "3: '#bf4500'"):
            self.assertIn(colour, app.PAGE)
        self.assertNotIn("`faction ${", app.PAGE)  # no raw faction ids in the UI

    def test_position_text_helpers_are_shared_with_the_page(self):
        for name in ("function placeText", "function mapCoordsText", "function worldText"):
            self.assertIn(name, app.PAGE)
        self.assertIn("placeText(p)", app.PAGE)   # marker tooltip
        self.assertIn("mapCoordsText(p)", app.PAGE)   # player list

    def test_page_is_english(self):
        self.assertIn('<html lang="en">', app.PAGE)
        self.assertNotIn("pt-BR", app.PAGE)
        self.assertIn("Intl.NumberFormat('en-US')", app.PAGE)
        for label in ("Health", "Position", "Reputation", "Zone", "Calibrate: off"):
            self.assertIn(label, app.PAGE)


if __name__ == "__main__":
    unittest.main()
