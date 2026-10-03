"""Tooltip lines for real item_template rows (copied from the live world database)."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from item_tooltip import COLUMNS, tooltip  # noqa: E402


def row(**kw):
    """An item_template row: every COLUMNS key, 0 unless given (like the DB defaults)."""
    t = {c: 0 for c in COLUMNS}
    t.update(AllowableClass=-1, AllowableRace=-1, description="")
    t.update(kw)
    return t


def texts(lines):
    return [(l["left"], l.get("right"), l.get("money"), l["color"]) for l in lines]


CLAYMORE = row(**{"class": 2, "subclass": 8, "InventoryType": 17, "SellPrice": 9, "ItemLevel": 2,
                  "RequiredLevel": 1, "AllowableClass": 262143, "dmg_min1": 3.0, "dmg_max1": 5.0,
                  "delay": 2900, "MaxDurability": 25})
CHAIN_BOOTS = row(**{"class": 4, "subclass": 3, "InventoryType": 8, "bonding": 1, "SellPrice": 11,
                     "ItemLevel": 5, "armor": 46, "MaxDurability": 25})
JAZERAINT = row(**{"class": 4, "subclass": 3, "InventoryType": 5, "bonding": 2, "SellPrice": 10301,
                   "ItemLevel": 44, "RequiredLevel": 39, "StatsCount": 3,
                   "stat_type1": 3, "stat_value1": 17, "stat_type2": 7, "stat_value2": 10,
                   "stat_type3": 38, "stat_value3": 18, "armor": 270, "MaxDurability": 120})
MIDNIGHT_MACE = row(**{"class": 2, "subclass": 4, "InventoryType": 13, "bonding": 2,
                       "SellPrice": 11620, "ItemLevel": 38, "RequiredLevel": 33,
                       "dmg_min1": 45.0, "dmg_max1": 84.0, "dmg_min2": 1.0, "dmg_max2": 10.0,
                       "dmg_type2": 5, "shadow_res": 10, "delay": 2500, "MaxDurability": 90})
TOUGH_JERKY = row(**{"class": 0, "subclass": 5, "SellPrice": 1, "ItemLevel": 5, "RequiredLevel": 1})
TORN_WYRM_SCALE = row(**{"class": 15, "SellPrice": 4, "ItemLevel": 1})
HEARTHSTONE = row(**{"class": 15, "Flags": 64, "bonding": 1, "ItemLevel": 1, "maxcount": 1})
BACKPACK = row(**{"class": 1, "InventoryType": 18, "SellPrice": 8750, "ItemLevel": 55,
                  "ContainerSlots": 16})


class TooltipTests(unittest.TestCase):
    def test_weapon(self):
        self.assertEqual(texts(tooltip("Battleworn Claymore", CLAYMORE, 1, 0, 21)), [
            ("Battleworn Claymore", None, None, "quality"),
            ("Two-Hand", "Sword", None, "white"),
            ("3 - 5 Damage", "Speed 2.90", None, "white"),
            ("(1.4 damage per second)", None, None, "white"),
            ("Durability 21 / 25", None, None, "white"),
            ("Requires Level 1", None, None, "white"),
            ("Item Level 2", None, None, "yellow"),
            ("Sell Price:", None, 9, "white"),
        ])

    def test_weapon_with_bonus_school_damage_and_resistance(self):
        lines = texts(tooltip("Midnight Mace", MIDNIGHT_MACE))
        self.assertEqual(lines[2:7], [
            ("One-Hand", "Mace", None, "white"),
            ("45 - 84 Damage", "Speed 2.50", None, "white"),
            ("+ 1 - 10 Shadow Damage", None, None, "white"),
            # ((45 + 84) / 2 + (1 + 10) / 2) / 2.5
            ("(28.0 damage per second)", None, None, "white"),
            ("+10 Shadow Resistance", None, None, "white"),
        ])
        self.assertEqual(lines[1], ("Binds when equipped", None, None, "white"))

    def test_armor_soulbound_instance_overrides_bonding(self):
        lines = texts(tooltip("Green Chain Boots", CHAIN_BOOTS, 1, 1, 25))
        self.assertEqual(lines[1:4], [("Soulbound", None, None, "white"),
                                      ("Feet", "Mail", None, "white"),
                                      ("46 Armor", None, None, "white")])
        self.assertNotIn("Binds when picked up", [l[0] for l in lines])

    def test_primary_stats_are_white_and_ratings_are_green_equip_lines(self):
        lines = texts(tooltip("Polished Jazeraint Armor", JAZERAINT))
        self.assertIn(("+17 Agility", None, None, "white"), lines)
        self.assertIn(("+10 Stamina", None, None, "white"), lines)
        self.assertEqual(lines[-2], ("Equip: Increases attack power by 18.", None, None, "green"))
        self.assertEqual(lines[-1], ("Sell Price:", None, 10301, "white"))

    def test_consumable(self):
        self.assertEqual(texts(tooltip("Tough Jerky", TOUGH_JERKY, 5)), [
            ("Tough Jerky", None, None, "quality"),
            ("Requires Level 1", None, None, "white"),
            ("Sell Price:", None, 5, "white"),  # the whole stack, as the game shows it
        ])

    def test_vendor_trash(self):
        self.assertEqual(texts(tooltip("Torn Wyrm Scale", TORN_WYRM_SCALE, 10)), [
            ("Torn Wyrm Scale", None, None, "quality"),
            ("Sell Price:", None, 40, "white"),
        ])

    def test_unique_and_no_sell_price(self):
        self.assertEqual(texts(tooltip("Hearthstone", HEARTHSTONE, 1, 1)), [
            ("Hearthstone", None, None, "quality"),
            ("Soulbound", None, None, "white"),
            ("Unique", None, None, "white"),
        ])

    def test_bag_shows_its_size(self):
        lines = texts(tooltip("Traveler's Backpack", BACKPACK))
        self.assertEqual(lines[1], ("16 Slot Bag", None, None, "white"))

    def test_class_restriction_lists_classes_but_all_classes_prints_nothing(self):
        self.assertFalse([l for l in texts(tooltip("x", CLAYMORE)) if l[0].startswith("Classes")])
        lines = texts(tooltip("x", row(**{"class": 4, "AllowableClass": 1 | 2})))
        self.assertIn(("Classes: Warrior, Paladin", None, None, "white"), lines)

    def test_description_is_a_yellow_quote(self):
        lines = texts(tooltip("x", row(**{"class": 15, "description": "Smells odd."})))
        self.assertIn(("“Smells odd.”", None, None, "yellow"), lines)

    def test_unknown_template_gives_the_name_only(self):
        self.assertEqual(texts(tooltip("Item 99999", {c: None for c in COLUMNS})),
                         [("Item 99999", None, None, "quality")])
        self.assertEqual(texts(tooltip("Item 99999", {})), [("Item 99999", None, None, "quality")])


if __name__ == "__main__":
    unittest.main()
