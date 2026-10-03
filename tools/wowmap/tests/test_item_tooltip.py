"""Tooltip lines for real item_template rows (copied from the live world database)."""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # tools/
from dbc.names import GameNames  # noqa: E402
from dbc.spelltext import Effect, SpellInfo  # noqa: E402
from item_tooltip import COLUMNS, parse_enchantments, set_piece_entries, tooltip  # noqa: E402


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


def game_names():
    """A GameNames with hand-filled tables. Spell, set, suffix, enchantment and gem rows
    carry values read from the real build-12340 DBCs (see tools/dbc/tests/test_item_tables.py
    for the ones checked against the client files); the spell ids and enchantment
    ids are the real ones where it says so."""
    with mock.patch("dbc.names.log"):
        n = GameNames("/nonexistent")
    spells = {
        433: SpellInfo("Food", "Restores $o1 health over $d.  Must remain seated while eating.",
                       18000, 101, 0, (Effect(16, 1, aura=84), Effect(), Effect())),   # real 433
        8690: SpellInfo("Hearthstone", "Returns you to $z.  Speak to an Innkeeper in a different "
                        "place to change your home location.", 0, 101, 0,
                        (Effect(-1, 1), Effect(), Effect())),                          # real 8690
        41719: SpellInfo("Set", "Increases attack power by $s1.", 0, 0, 0,
                         (Effect(49, 1), Effect(), Effect())),                          # real 41719
        41863: SpellInfo("Set", "Increases defense rating by $s1.", 0, 0, 0,
                         (Effect(1, 1), Effect(), Effect())),
        41864: SpellInfo("Set", "Increases defense rating by $s1.", 0, 0, 0,
                         (Effect(2, 1), Effect(), Effect())),                           # real 41864: 3
        100: SpellInfo("Proc", "Blasts your enemy for $s1 to $S1 damage.", 0, 0, 0,
                       (Effect(9, 11), Effect(), Effect())),                            # unresolved $S
        101: SpellInfo("Proc", "Deals $s1 Fire damage.", 0, 0, 0, (Effect(13, 9), Effect(), Effect())),
        102: SpellInfo("Learn", "Teaches you something.", 0, 0, 0, (Effect(), Effect(), Effect())),
        103: SpellInfo("Silent", "", 0, 0, 0, (Effect(), Effect(), Effect())),
    }
    n._spell_cache.update(spells)
    n.item_sets = {1: ("The Gladiator", (11729, 11726, 11728, 11731, 11730),
                       ((2, 41863), (3, 41864), (4, 41719), (5, 100)))}
    n.random_properties = {605: ("of the Monkey", (343, 353))}
    n.random_suffixes = {7: ("of the Bear", ((2803, 10000), (2805, 6666)))}
    n.enchants = {
        343: ("+8 Agility", ((5, 8, 3),)), 353: ("+8 Stamina", ((5, 8, 7),)),
        2803: ("+$i Stamina", ((5, 0, 7),)), 2805: ("+$i Strength", ((5, 0, 4),)),
        2686: ("+8 Strength", ((5, 8, 4),)),           # real: a red gem's enchantment
        2564: ("+15 Agility", ((5, 15, 3),)),          # real: glove enchantment
        3225: ("Executioner", ((1, 0, 42976),)),       # real: a weapon enchantment
        2687: ("+8 Agility", ((5, 8, 3),)),
        3318: ("+4 Stamina", ((5, 4, 7),)),            # a socket bonus
        3723: ("Prismatic Socket", ((8, 0, 0),)),
        2680: ("Poison", ((1, 0, 1),)),
    }
    n.gem_colors = {2686: 2, 2687: 8, 343: 2}       # 2 red, 8 blue (SocketColor)
    n.skills = {164: "Blacksmithing"}
    n.factions = {932: ("The Aldor", 0, (0,) * 4, (0,) * 4, (0,) * 4, 0)}
    n.rand_prop_points = {60: ((40, 30, 20, 15, 10), (30, 22, 15, 11, 8), (26, 20, 13, 10, 7))}
    return n


NAMES = game_names()
HELM = row(**{"class": 4, "subclass": 3, "InventoryType": 1, "Quality": 2, "ItemLevel": 60,
              "RequiredLevel": 55, "armor": 300, "itemset": 1})
GLOVES = row(**{"class": 4, "subclass": 2, "InventoryType": 10, "Quality": 3, "ItemLevel": 60,
                "socketColor_1": 2, "socketColor_2": 8, "socketBonus": 3318})


def lines_of(*a, **kw):
    return texts(tooltip(*a, names=NAMES, **kw))


class SpellLineTests(unittest.TestCase):
    def test_use_line_of_a_food(self):
        t = row(**{"class": 0, "subclass": 5, "SellPrice": 1, "ItemLevel": 5, "RequiredLevel": 1,
                   "spellid_1": 433, "spelltrigger_1": 0})
        self.assertEqual(lines_of("Tough Jerky", t, 5), [
            ("Tough Jerky", None, None, "quality"),
            ("Requires Level 1", None, None, "white"),
            ("Use: Restores 61 health over 18 sec. Must remain seated while eating.",
             None, None, "green"),
            ("Sell Price:", None, 5, "white"),
        ])

    def test_hearthstone_use_line(self):
        t = row(**{"class": 15, "Flags": 64, "bonding": 1, "ItemLevel": 1, "maxcount": 1,
                   "spellid_1": 8690, "spelltrigger_1": 0})
        self.assertIn(("Use: Returns you to your home location. Speak to an Innkeeper in a "
                       "different place to change your home location.", None, None, "green"),
                      lines_of("Hearthstone", t, 1, 1))

    def test_triggers_equip_and_chance_on_hit_keep_slot_order(self):
        t = row(**{"class": 2, "InventoryType": 13, "delay": 2000, "dmg_min1": 1.0, "dmg_max1": 2.0,
                   "spellid_1": 41719, "spelltrigger_1": 1, "spellid_2": 101, "spelltrigger_2": 2,
                   "spellid_3": 433, "spelltrigger_3": 0})
        got = [l[0] for l in lines_of("Sword", t)][-3:]
        self.assertEqual(got, ["Equip: Increases attack power by 50.",
                               "Chance on hit: Deals 14 to 22 Fire damage.",
                               "Use: Restores 61 health over 18 sec. Must remain seated while eating."])

    def test_skips_unverified_triggers_unresolved_text_and_empty_descriptions(self):
        t = row(**{"class": 0, "spellid_1": 102, "spelltrigger_1": 6,   # learn spell
                   "spellid_2": 100, "spelltrigger_2": 1,               # $S of a range
                   "spellid_3": 103, "spelltrigger_3": 0,               # empty description
                   "spellid_4": 99999, "spelltrigger_4": 0,             # not in Spell.dbc
                   "spellid_5": 102, "spelltrigger_5": 4})              # soulstone
        self.assertEqual(lines_of("x", t), [("x", None, None, "quality")])

    def test_without_names_nothing_changes(self):
        t = row(**{"class": 0, "spellid_1": 433, "spelltrigger_1": 0})
        self.assertEqual(texts(tooltip("x", t)), [("x", None, None, "quality")])


class ItemSetTests(unittest.TestCase):
    PIECES = {11729: "Gladiator Helm", 11726: "Gladiator Chain", 11728: "Gladiator Grips",
              11731: "Gladiator Boots", 11730: "Gladiator Leggings"}  # fixture names

    def set_lines(self, equipped):
        lines = lines_of("Gladiator Helm", HELM, equipped=equipped, item_names=self.PIECES)
        return lines[lines.index(("The Gladiator (%d/5)" % len(set(equipped) & set(self.PIECES)),
                                  None, None, "yellow")):]

    def test_progress_pieces_and_bonuses(self):
        self.assertEqual(self.set_lines({11729, 11728, 111}), [
            ("The Gladiator (2/5)", None, None, "yellow"),
            ("Gladiator Helm", None, None, "yellow"),
            ("Gladiator Chain", None, None, "gray"),
            ("Gladiator Grips", None, None, "yellow"),
            ("Gladiator Boots", None, None, "gray"),
            ("Gladiator Leggings", None, None, "gray"),
            ("(2) Set: Increases defense rating by 2.", None, None, "green"),
            ("(3) Set: Increases defense rating by 3.", None, None, "gray"),
            ("(4) Set: Increases attack power by 50.", None, None, "gray"),
            # the 5-piece bonus has a spell text we refuse ($S), so it is left out
        ])

    def test_set_with_nothing_equipped_and_unknown_piece_names(self):
        lines = lines_of("x", HELM)
        self.assertIn(("The Gladiator (0/5)", None, None, "yellow"), lines)
        self.assertIn(("Item 11729", None, None, "gray"), lines)

    def test_set_block_comes_after_the_description_and_before_the_sell_price(self):
        t = dict(HELM, description="Fancy.", SellPrice=10)
        lines = [l[0] for l in lines_of("x", t, equipped={11729})]
        self.assertLess(lines.index("“Fancy.”"), lines.index("The Gladiator (1/5)"))
        self.assertEqual(lines[-1], "Sell Price:")

    def test_set_piece_entries(self):
        self.assertEqual(set_piece_entries({1, 999}, NAMES), [11726, 11728, 11729, 11730, 11731])
        self.assertEqual(set_piece_entries(set(), NAMES), [])


class RandomPropertyAndEnchantTests(unittest.TestCase):
    def test_suffix_name_and_scaled_stats(self):
        lines = lines_of("Chestguard", HELM, random_property=-7)
        self.assertEqual(lines[0], ("Chestguard of the Bear", None, None, "quality"))
        # item level 60 uncommon head: Good[0] = 26 points
        self.assertIn(("+26 Stamina", None, None, "green"), lines)
        self.assertIn(("+17 Strength", None, None, "green"), lines)

    def test_random_property_with_fixed_stats(self):
        lines = lines_of("Cloak", HELM, random_property=605)
        self.assertEqual(lines[0][0], "Cloak of the Monkey")
        self.assertEqual([l for l in lines if l[0].startswith("+")],
                         [("+8 Agility", None, None, "green"), ("+8 Stamina", None, None, "green")])

    def test_permanent_and_temporary_enchants(self):
        # slot 0 permanent, slot 1 temporary with 3,540,000 ms left (59 min)
        ench = "2564 0 0 2680 3540000 0 " + "0 0 0 " * 10
        lines = lines_of("Gloves", row(**{"class": 4, "InventoryType": 10}), enchantments=ench)
        self.assertEqual([l[0] for l in lines[2:]], ["+15 Agility", "Poison (59 min)"])
        self.assertEqual({l[3] for l in lines[2:]}, {"green"})

    def test_sockets_gems_and_an_active_bonus(self):
        # red socket holds a red gem, blue socket a blue gem
        ench = "0 0 0 0 0 0 2686 0 0 2687 0 0 " + "0 0 0 " * 8
        got = lines_of("Gloves", GLOVES, enchantments=ench)
        self.assertEqual([l for l in got if "Socket" in l[0] or l[0].startswith("+")], [
            ("+8 Strength", None, None, "white"), ("+8 Agility", None, None, "white"),
            ("Socket Bonus: +4 Stamina", None, None, "green")])

    def test_wrong_colour_gem_and_empty_socket_make_the_bonus_inactive(self):
        wrong = "0 0 0 0 0 0 2687 0 0 2687 0 0 " + "0 0 0 " * 8   # blue gem in the red socket
        bonus = lambda ls: [l for l in ls if l[0].startswith("Socket Bonus")]  # noqa: E731
        self.assertEqual(bonus(lines_of("G", GLOVES, enchantments=wrong)),
                         [("Socket Bonus: +4 Stamina", None, None, "gray")])
        empty = "0 0 0 0 0 0 2686 0 0 " + "0 0 0 " * 9
        got = lines_of("G", GLOVES, enchantments=empty)
        self.assertIn(("Blue Socket", None, None, "gray"), got)
        self.assertEqual(bonus(got), [("Socket Bonus: +4 Stamina", None, None, "gray")])

    def test_extra_prismatic_socket(self):
        t = row(**{"class": 4, "InventoryType": 6})
        buckle = "3723 0 0 " + "0 0 0 " * 11
        self.assertEqual([l[0] for l in lines_of("Belt", t, enchantments=buckle)[2:]],
                         ["Prismatic Socket"])
        gem = "3723 0 0 " + "0 0 0 " * 5 + "2686 0 0 " + "0 0 0 " * 5
        self.assertEqual([l[0] for l in lines_of("Belt", t, enchantments=gem)[2:]], ["+8 Strength"])

    def test_parse_enchantments(self):
        self.assertEqual(parse_enchantments("5 0 0 0 0 0 7 100 2 " + "0 0 0 " * 9),
                         {0: (5, 0, 0), 2: (7, 100, 2)})
        self.assertEqual(parse_enchantments(""), {})
        self.assertEqual(parse_enchantments("junk"), {})
        self.assertEqual(parse_enchantments(None), {})


class RequirementTests(unittest.TestCase):
    def test_required_skill_and_reputation_follow_requires_level(self):
        t = row(**{"class": 4, "InventoryType": 5, "ItemLevel": 70, "RequiredLevel": 68,
                   "RequiredSkill": 164, "RequiredSkillRank": 350,
                   "RequiredReputationFaction": 932, "RequiredReputationRank": 6})
        self.assertEqual([l[0] for l in lines_of("x", t)][1:6], [
            "Chest", "Requires Level 68", "Requires Blacksmithing (350)",
            "Requires The Aldor - Revered", "Item Level 70"])

    def test_unknown_skill_or_faction_prints_nothing(self):
        t = row(**{"class": 15, "RequiredSkill": 999, "RequiredReputationFaction": 1,
                   "RequiredReputationRank": 4})
        self.assertEqual(lines_of("x", t), [("x", None, None, "quality")])


if __name__ == "__main__":
    unittest.main()
