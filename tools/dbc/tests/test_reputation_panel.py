"""GameNames.reputation_panel / reputation_bar against Rubens' real reputation.

Faction rows are copied from the 3.3.5a (12340) Faction.dbc: (name, ReputationIndex,
ReputationRaceMask[4], ReputationClassMask[4], ReputationBase[4], ParentFactionID).
Standings are Rubens' (Blood Elf Paladin) from the live /api/character/Rubens;
flags follow the issue (17 = Visible|Peaceful on the city rows, 25 on the Horde
header) and Faction.dbc ReputationFlags for the rest.
"""
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # tools/
from dbc.names import GameNames, reputation_bar  # noqa: E402

HORDE_CITY = ((0xa2, 0x44d, 0x200, 0x10), (0, 0, 0, 0), (400, -42000, 4000, 3100))
FACTIONS = {
    1118: ("Classic", 96, (0x6ff, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0), 0),
    980: ("The Burning Crusade", 43, (0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0), 0),
    1097: ("Wrath of the Lich King", 89, (0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0), 0),
    67: ("Horde", 12, (0x2b2, 0x44d, 0, 0), (0, 0, 0, 0), (3500, -42000, 0, 0), 1118),
    68: ("Undercity", 17, (0xa2, 0x44d, 0x10, 0x200), (0, 0, 0, 0), (500, -42000, 4000, 3100), 67),
    76: ("Orgrimmar", 14, (0xa0, 0x44d, 0x2, 0x210), (0, 0, 0, 0), (3100, -42000, 4000, 500), 67),
    81: ("Thunder Bluff", 16, (0x82, 0x44d, 0x210, 0x20), (0, 0, 0, 0), (3100, -42000, 500, 4000), 67),
    530: ("Darkspear Trolls", 15, (0x22, 0x44d, 0x210, 0x80), (0, 0, 0, 0), (3100, -42000, 500, 4000), 67),
    911: ("Silvermoon City", 55, *HORDE_CITY, 67),
    469: ("Alliance", 11, (0x44d, 0x2b2, 0, 0), (0, 0, 0, 0), (3300, -42000, 0, 0), 1118),
    72: ("Stormwind", 19, (0x44c, 0x2b2, 0x1, 0), (0, 0, 0, 0), (3100, -42000, 4000, 0), 469),
    169: ("Steamwheedle Cartel", 10, (0x6ff, 0, 0, 0), (0, 0, 0, 0), (500, 0, 0, 0), 1118),
    21: ("Booty Bay", 1, (0x6ff, 0, 0, 0), (0, 0, 0, 0), (500, 0, 0, 0), 169),
    70: ("Syndicate", 6, (0x6ff, 0, 0, 0), (0, 0, 0, 0), (-10000, 0, 0, 0), 0),
    922: ("Tranquillien", 56, (0x2b2, 0x44d, 0, 0), (0, 0, 0, 0), (0, -42000, 0, 0), 1118),
    1037: ("Alliance Vanguard", 88, (0x44d, 0x2b2, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0), 1097),
    1052: ("Horde Expedition", 75, (0x2b2, 0x44d, 0, 0), (0, 0, 0, 0), (0, -42000, 0, 0), 1097),
    1067: ("The Hand of Vengeance", 77, (0x2b2, 0x44d, 0, 0), (0, 0, 0, 0), (0, -42000, 0, 0), 1052),
    1091: ("The Wyrmrest Accord", 83, (0x6ff, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0), 1097),
    1005: ("Friendly, Hidden", 68, (0x7fff, 0, 0, 0), (0x5df, 0, 0, 0), (3000, 0, 0, 0), 0),
}
# (faction, standing, flags) as character_reputation stores them for Rubens.
RUBENS = [
    (1118, 0, 0x0c), (980, 0, 0x18), (1097, 0, 0x0c),
    (67, 0, 25), (68, 62, 17), (76, 62, 17), (81, 62, 17), (530, 62, 17), (911, 250, 17),
    (469, 0, 0x0e), (72, 0, 0x06), (169, 0, 0x0c), (21, 0, 0x40), (70, 0, 0x02),
    (922, 0, 0x10), (1037, 0, 0x06), (1052, 0, 0x98), (1067, 0, 0x10), (1091, 0, 0x10),
    (1005, 0, 0x04),
]
BLOOD_ELF, PALADIN = 10, 2


def names():
    with mock.patch("dbc.names.log"):
        n = GameNames("/nonexistent")
    n.factions = dict(FACTIONS)
    return n


def flat(panel, depth=0):
    """[(depth, name, rank, "bar_value/bar_max")] in display order."""
    out = []
    for node in panel:
        r = node["rep"]
        out.append((depth, node["name"], r and r["rank"],
                    r and f'{r["bar_value"]}/{r["bar_max"]}'))
        out += flat(node.get("children") or [], depth + 1)
    return out


class ReputationBarTests(unittest.TestCase):
    def test_ranks_and_progress_follow_trinitycore_thresholds(self):
        cases = {
            -42000: ("Hated", 1, 0, 36000), -6001: ("Hated", 1, 35999, 36000),
            -6000: ("Hostile", 2, 0, 3000), -3000: ("Unfriendly", 3, 0, 3000),
            -1: ("Unfriendly", 3, 2999, 3000), 0: ("Neutral", 4, 0, 3000),
            562: ("Neutral", 4, 562, 3000), 4250: ("Friendly", 5, 1250, 6000),
            9000: ("Honored", 6, 0, 12000), 21000: ("Revered", 7, 0, 21000),
            42000: ("Exalted", 8, 0, 1000), 42999: ("Exalted", 8, 999, 1000),
        }
        for value, (rank, rank_id, bar, top) in cases.items():
            self.assertEqual(reputation_bar(value), {"rank": rank, "rank_id": rank_id,
                                                     "bar_value": bar, "bar_max": top}, value)

    def test_values_outside_the_cap_are_clamped(self):
        self.assertEqual(reputation_bar(-50000)["bar_value"], 0)
        self.assertEqual(reputation_bar(50000)["bar_value"], 999)


class ReputationPanelTests(unittest.TestCase):
    def test_rubens_window(self):
        panel = names().reputation_panel(RUBENS, BLOOD_ELF, PALADIN)
        self.assertEqual(flat(panel), [
            (0, "Classic", None, None),
            (1, "Horde", None, None),
            (2, "Darkspear Trolls", "Neutral", "562/3000"),
            (2, "Orgrimmar", "Neutral", "562/3000"),
            (2, "Silvermoon City", "Friendly", "1250/6000"),
            (2, "Thunder Bluff", "Neutral", "562/3000"),
            (2, "Undercity", "Friendly", "162/6000"),
        ])

    def test_hidden_and_undiscovered_factions_are_left_out(self):
        listed = {name for _, name, _, _ in flat(names().reputation_panel(RUBENS, BLOOD_ELF, PALADIN))}
        # Not Visible (Syndicate only has AtWar; Booty Bay, Tranquillien), Hidden, and
        # headers with nothing visible under them.
        for name in ("Syndicate", "Booty Bay", "Tranquillien", "Friendly, Hidden", "Alliance",
                     "Stormwind", "Steamwheedle Cartel", "The Burning Crusade",
                     "Wrath of the Lich King"):
            self.assertNotIn(name, listed)

    def test_discovered_faction_at_zero_is_listed_and_at_war_is_flagged(self):
        rows = RUBENS + [(1091, 0, 0x11), (70, 0, 0x03)]
        panel = names().reputation_panel(rows, BLOOD_ELF, PALADIN)
        self.assertIn((1, "The Wyrmrest Accord", "Neutral", "0/3000"), flat(panel))
        syndicate = next(n for n in panel if n["name"] == "Syndicate")
        self.assertEqual(syndicate["rep"]["rank"], "Hated")
        self.assertTrue(syndicate["rep"]["at_war"])
        self.assertEqual([n["name"] for n in panel], ["Classic", "Syndicate", "Wrath of the Lich King"])

    def test_header_with_its_own_bar(self):
        # Horde Expedition is HeaderShowsBar (0x80): visible, it shows a bar even
        # before any child is discovered.
        rows = [r for r in RUBENS if r[0] != 1052] + [(1052, 1500, 0x99)]
        panel = names().reputation_panel(rows, BLOOD_ELF, PALADIN)
        wotlk = next(n for n in panel if n["name"] == "Wrath of the Lich King")
        expedition = wotlk["children"][0]
        self.assertEqual((expedition["name"], expedition["header"], expedition["children"]),
                         ("Horde Expedition", True, []))
        self.assertEqual(expedition["rep"]["rank"], "Neutral")
        self.assertEqual(expedition["rep"]["bar_value"], 1500)

    def test_inactive_factions_move_to_a_trailing_group(self):
        rows = [r for r in RUBENS if r[0] != 76] + [(76, 62, 0x31)]
        panel = names().reputation_panel(rows, BLOOD_ELF, PALADIN)
        self.assertEqual(flat(panel)[-2:], [(0, "Inactive", None, None),
                                            (1, "Orgrimmar", "Neutral", "562/3000")])
        self.assertNotIn((2, "Orgrimmar", "Neutral", "562/3000"), flat(panel))

    def test_no_names_still_lists_visible_rows_without_ids(self):
        with mock.patch("dbc.names.log"):
            n = GameNames("/nonexistent")
        panel = n.reputation_panel([(76, 62, 17), (21, 0, 0x40)], BLOOD_ELF, PALADIN)
        self.assertEqual(flat(panel), [(0, "Unknown faction", "Neutral", "62/3000")])

    def test_nothing_discovered(self):
        self.assertEqual(names().reputation_panel([], BLOOD_ELF, PALADIN), [])


@unittest.skipUnless(os.environ.get("DBC_TEST_DIR"), "set DBC_TEST_DIR to real 3.3.5a DBCs")
class RealFactionDbcTests(unittest.TestCase):
    """The fixture above is the real Faction.dbc (e.g. a copy of /opt/wowmap-data/dbc)."""

    def test_fixture_matches_faction_dbc(self):
        real = GameNames(os.environ["DBC_TEST_DIR"]).factions
        for faction, row in FACTIONS.items():
            self.assertEqual(real[faction], row, faction)

    def test_rubens_window_from_the_real_tree(self):
        n = GameNames(os.environ["DBC_TEST_DIR"])
        self.assertEqual(flat(n.reputation_panel(RUBENS, BLOOD_ELF, PALADIN)),
                         flat(names().reputation_panel(RUBENS, BLOOD_ELF, PALADIN)))


if __name__ == "__main__":
    unittest.main()
