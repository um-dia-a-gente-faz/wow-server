"""Unit tests for agent.item_compare: the v1 scoring heuristic (primary
stat for class + item level tiebreak) and the usability check, against a
table of known item stat combos. No live server needed — see
agent/tests/test_actions_loot.py for the compare_items/equip_item Action tests."""

import unittest

from agent import item_compare as ic


def item(name, class_=2, subclass=7, item_level=1, allowable_class=-1,
         stats=(), armor=0):
    return {
        "entry": 1, "found": True, "name": name, "class_": class_, "subclass": subclass,
        "inventory_type": 13, "item_level": item_level, "allowable_class": allowable_class,
        "stats": [{"type": t, "value": v} for t, v in stats], "armor": armor,
        "damage": [], "spells": [],
    }


class PrimaryStatValueTest(unittest.TestCase):
    def test_sums_matching_stat_entries(self):
        it = item("Sword", stats=((ic.ITEM_MOD_STRENGTH, 10), (ic.ITEM_MOD_STRENGTH, 5),
                                   (ic.ITEM_MOD_AGILITY, 20)))
        self.assertEqual(ic.primary_stat_value(it, ic.CLASS_WARRIOR), 15)

    def test_zero_when_class_has_no_matching_stat(self):
        it = item("Staff", stats=((ic.ITEM_MOD_INTELLECT, 30),))
        self.assertEqual(ic.primary_stat_value(it, ic.CLASS_WARRIOR), 0)

    def test_zero_for_unknown_class(self):
        it = item("Trinket", stats=((ic.ITEM_MOD_STRENGTH, 10),))
        self.assertEqual(ic.primary_stat_value(it, 999), 0)


class ScoreItemTest(unittest.TestCase):
    KNOWN_COMBOS = [
        # (class, stats, item_level, expected_score)
        (ic.CLASS_WARRIOR, ((ic.ITEM_MOD_STRENGTH, 10),), 20, 10 * ic.PRIMARY_STAT_WEIGHT + 20),
        (ic.CLASS_HUNTER, ((ic.ITEM_MOD_AGILITY, 7),), 5, 7 * ic.PRIMARY_STAT_WEIGHT + 5),
        (ic.CLASS_MAGE, ((ic.ITEM_MOD_INTELLECT, 3),), 1, 3 * ic.PRIMARY_STAT_WEIGHT + 1),
        (ic.CLASS_WARRIOR, ((ic.ITEM_MOD_INTELLECT, 100),), 50, 50),  # off-stat: item level only
        (ic.CLASS_WARRIOR, (), 0, 0),
    ]

    def test_known_combos(self):
        for class_id, stats, item_level, expected in self.KNOWN_COMBOS:
            with self.subTest(class_id=class_id, stats=stats, item_level=item_level):
                it = item("X", stats=stats, item_level=item_level)
                self.assertEqual(ic.score_item(it, class_id), expected)

    def test_item_level_only_breaks_near_ties_not_dominates(self):
        low_ilvl_more_stat = item("A", stats=((ic.ITEM_MOD_STRENGTH, 10),), item_level=1)
        high_ilvl_less_stat = item("B", stats=((ic.ITEM_MOD_STRENGTH, 9),), item_level=99)
        self.assertGreater(ic.score_item(low_ilvl_more_stat, ic.CLASS_WARRIOR),
                            ic.score_item(high_ilvl_less_stat, ic.CLASS_WARRIOR))


class CompareTest(unittest.TestCase):
    def test_higher_primary_stat_wins(self):
        a = item("Sword A", stats=((ic.ITEM_MOD_STRENGTH, 20),), item_level=10)
        b = item("Sword B", stats=((ic.ITEM_MOD_STRENGTH, 5),), item_level=10)
        result = ic.compare(a, b, ic.CLASS_WARRIOR)
        self.assertEqual(result["winner"], "a")
        self.assertIn("Sword A", result["reason"])
        self.assertIn("Sword B", result["reason"])

    def test_item_level_breaks_tie_in_primary_stat(self):
        a = item("Sword A", stats=((ic.ITEM_MOD_STRENGTH, 10),), item_level=5)
        b = item("Sword B", stats=((ic.ITEM_MOD_STRENGTH, 10),), item_level=50)
        result = ic.compare(a, b, ic.CLASS_WARRIOR)
        self.assertEqual(result["winner"], "b")

    def test_true_tie_reports_tie(self):
        a = item("Sword A", stats=((ic.ITEM_MOD_STRENGTH, 10),), item_level=5)
        b = item("Sword B", stats=((ic.ITEM_MOD_STRENGTH, 10),), item_level=5)
        result = ic.compare(a, b, ic.CLASS_WARRIOR)
        self.assertEqual(result["winner"], "tie")

    def test_class_changes_which_item_wins(self):
        # A has strength, B has intellect: warrior prefers A, mage prefers B.
        a = item("Str Item", stats=((ic.ITEM_MOD_STRENGTH, 15),), item_level=10)
        b = item("Int Item", stats=((ic.ITEM_MOD_INTELLECT, 15),), item_level=10)
        self.assertEqual(ic.compare(a, b, ic.CLASS_WARRIOR)["winner"], "a")
        self.assertEqual(ic.compare(a, b, ic.CLASS_MAGE)["winner"], "b")


class UsabilityErrorTest(unittest.TestCase):
    def test_allowable_class_mask_blocks_other_classes(self):
        mage_only = item("Mage Robe", class_=ic.ITEM_CLASS_ARMOR, subclass=ic.ARMOR_SUBCLASS_CLOTH,
                          allowable_class=1 << (ic.CLASS_MAGE - 1))
        self.assertIsNone(ic.usability_error(mage_only, ic.CLASS_MAGE))
        err = ic.usability_error(mage_only, ic.CLASS_WARRIOR)
        self.assertIsNotNone(err)
        self.assertIn("not usable by class", err)

    def test_no_restriction_mask_allows_everyone(self):
        for mask in (-1, 0):
            it = item("Generic Cloak", class_=ic.ITEM_CLASS_ARMOR, subclass=ic.ARMOR_SUBCLASS_CLOTH,
                       allowable_class=mask)
            self.assertIsNone(ic.usability_error(it, ic.CLASS_WARRIOR))

    def test_armor_subclass_beyond_proficiency_blocked(self):
        plate = item("Plate Helm", class_=ic.ITEM_CLASS_ARMOR, subclass=ic.ARMOR_SUBCLASS_PLATE)
        self.assertIsNone(ic.usability_error(plate, ic.CLASS_WARRIOR))
        err = ic.usability_error(plate, ic.CLASS_MAGE)
        self.assertIsNotNone(err)
        self.assertIn("proficiency", err)

    def test_cloth_always_wearable(self):
        cloth = item("Cloth Shirt", class_=ic.ITEM_CLASS_ARMOR, subclass=ic.ARMOR_SUBCLASS_CLOTH)
        for class_id in (ic.CLASS_WARRIOR, ic.CLASS_MAGE, ic.CLASS_ROGUE, ic.CLASS_HUNTER):
            self.assertIsNone(ic.usability_error(cloth, class_id))

    def test_non_armor_non_weapon_items_only_checked_by_allowable_class(self):
        trinket = item("Trinket", class_=15)  # ITEM_CLASS_MISC or similar — not armor
        self.assertIsNone(ic.usability_error(trinket, ic.CLASS_WARRIOR))


if __name__ == '__main__':
    unittest.main()
