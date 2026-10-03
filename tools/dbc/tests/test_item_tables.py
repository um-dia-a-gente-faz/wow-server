"""Item tooltip tables (GH-119): spell text, item sets, random properties, enchantments.

The synthetic DBCs use the build-12340 field counts and the offsets cited in
names.py. The class at the bottom reads the real client files when DBC_TEST_DIR
points at them (extracted from the 3.3.5a MPQs, like the other DBC tests); the
expected texts there were checked against what the client shows.
"""
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # tools/
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from dbc import names as N  # noqa: E402
from dbc.names import GameNames  # noqa: E402
from dbc.spelltext import Effect, SpellInfo, Unresolved, format_duration, render  # noqa: E402
from test_wdbc import build_wdbc  # noqa: E402


def spell(description, duration_ms=0, *effects, **kw):
    effects = tuple(effects) + (Effect(),) * (3 - len(effects))
    return SpellInfo("Test", description, duration_ms, kw.get("proc_chance", 0),
                     kw.get("proc_charges", 0), effects)


def r(description, *a, lookup=None, **kw):
    s = spell(description, *a, **kw)
    return render(description, s, lookup)


class SpellTextTests(unittest.TestCase):
    def test_effect_value_is_base_points_plus_one_with_a_die_side(self):
        # Blessed Sunfruit: base points 9, one die side -> "Increases Strength by 10"
        self.assertEqual(r("Increases Strength by $s1 for $d.", 600000, Effect(9, 1)),
                         "Increases Strength by 10 for 10 min.")
        self.assertEqual(r("$s1", 0, Effect(9, 0)), "9")          # no die: as stored
        self.assertEqual(r("$s1", 0, Effect(-141, 1)), "140")     # absolute value

    def test_die_range_prints_as_a_range(self):
        # Minor Healing Potion's spell (439): base points 69, 21 die sides.
        heal = Effect(69, 21)
        self.assertEqual(r("Restores $s1 health.", 0, heal), "Restores 70 to 90 health.")
        self.assertEqual(r("$m1 to $M1", 0, heal), "70 to 90")
        self.assertEqual(r("$M1", 0, Effect(5, 1)), "6")
        with self.assertRaises(Unresolved):
            r("$S1", 0, heal)  # not verified for ranges

    def test_total_over_duration_uses_five_second_ticks_for_regen_auras(self):
        # Tough Jerky's spell 433: regen aura 84, base points 16, 18 s -> 61 health
        food = Effect(16, 1, aura=84)
        self.assertEqual(r("Restores $o1 health over $d.  Must remain seated while eating.",
                           18000, food),
                         "Restores 61 health over 18 sec. Must remain seated while eating.")
        periodic = Effect(9, 1, aura=8, period_ms=3000)
        self.assertEqual(r("$o1 over $d, $t1 sec apart", 12000, periodic), "40 over 12 sec, 3 sec apart")
        with self.assertRaises(Unresolved):
            r("$o1", 12000, Effect(9, 1, aura=8))      # no period, not a regen aura
        with self.assertRaises(Unresolved):
            r("$o1", 0, periodic)                      # no duration

    def test_durations_follow_the_client_strings(self):
        self.assertEqual(format_duration(18000), "18 sec")
        self.assertEqual(format_duration(90000), "1.5 min")
        self.assertEqual(format_duration(600000), "10 min")
        self.assertEqual(format_duration(3600000), "1 hour")
        self.assertEqual(format_duration(7200000), "2 hrs")
        self.assertEqual(format_duration(86400000 * 2), "2 days")
        self.assertEqual(format_duration(-1), "until cancelled")

    def test_other_variables(self):
        e = Effect(10, 1, radius=10.0, radius_max=30.0, chain_targets=3)
        self.assertEqual(r("$a1 yd, $A1 yd, $x1 targets, $h% chance, $n charges", 0, e,
                           proc_chance=15, proc_charges=2),
                         "10 yd, 30 yd, 3 targets, 15% chance, 2 charges")
        self.assertEqual(r("$/1000;s1 and $*2;s1", 0, Effect(1999, 1)), "2 and 4000")

    def test_plural_follows_the_last_number(self):
        for points, text in ((0, "1 charge"), (2, "3 charges")):
            self.assertEqual(r("$s1 $lcharge:charges;", 0, Effect(points, 1)), text)

    def test_spell_references(self):
        other = spell("", 0, Effect(24, 1))
        self.assertEqual(r("Gives $12345s1.", lookup=lambda i: other if i == 12345 else None),
                         "Gives 25.")
        with self.assertRaises(Unresolved):
            r("Gives $999s1.", lookup=lambda i: None)

    def test_hearthstone_location_is_approximated(self):
        self.assertEqual(r("Returns you to $z."), "Returns you to your home location.")

    def test_refuses_what_it_cannot_resolve(self):
        for text in ("${$m1*5} damage", "$?s123[a][b]", "$g him:her;", "$u stacks", "$s4",
                     "$d", "$a1 yards"):
            with self.assertRaises(Unresolved, msg=text):
                r(text)
        with self.assertRaises(Unresolved):
            r("$s1", 0, Effect(10, 1, per_level=1.5))  # scales with the caster's level


class ItemTablesTests(unittest.TestCase):
    """Synthetic DBCs: one record per table, fields at the documented offsets."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        spell_rec = {0: 433, 136: "Food", 40: 85, 71: 6, 74: 1, 80: 16, 95: 84,
                     170: "Restores $o1 health over $d."}
        spell_rec2 = {0: 9, 136: "Burst", 71 + 1: 6, 74 + 1: 21, 80 + 1: 69, 92 + 1: 7,
                      98 + 1: 2000, 104 + 1: 4, 35: 30, 36: 3, 170: "Hits $s2 for $a2 yards"}
        files = {
            "Spell.dbc": build_wdbc(N.SPELL_FIELDS, [spell_rec, spell_rec2]),
            "SpellDuration.dbc": build_wdbc(N.DURATION_FIELDS, [{0: 85, 1: 18000}, {0: 1, 1: -1}]),
            "SpellRadius.dbc": build_wdbc(N.RADIUS_FIELDS, [{0: 7, 1: 10.0, 3: 30.0}]),
            "ItemSet.dbc": build_wdbc(N.ITEMSET_FIELDS, [
                {0: 1, 1: "The Gladiator", 18: 11729, 19: 11726, 20: 11728,
                 35: 41864, 36: 41863, 43: 3, 44: 2},
            ]),
            "ItemRandomProperties.dbc": build_wdbc(N.RANDPROP_FIELDS, [
                {0: 605, 1: "monkey", 2: 343, 3: 353, 7: "of the Monkey"},
            ]),
            "ItemRandomSuffix.dbc": build_wdbc(N.RANDSUFFIX_FIELDS, [
                {0: 7, 1: "of the Bear", 19: 2803, 20: 2805, 24: 10000, 25: 6666},
            ]),
            "SpellItemEnchantment.dbc": build_wdbc(N.ENCHANT_FIELDS, [
                {0: 343, 2: 5, 5: 8, 11: 3, 14: "+8 Agility"},
                {0: 353, 2: 5, 5: 8, 11: 7, 14: "+8 Stamina"},
                {0: 2803, 2: 5, 11: 7, 14: "+$i Stamina"},
                {0: 2805, 2: 5, 11: 4, 14: "+$i Strength"},
            ]),
            "GemProperties.dbc": build_wdbc(N.GEMPROP_FIELDS, [{0: 2, 1: 343, 4: 2}]),
            "SkillLine.dbc": build_wdbc(N.SKILL_FIELDS, [{0: 164, 3: "Blacksmithing"}]),
            "RandPropPoints.dbc": build_wdbc(N.RANDPOINTS_FIELDS, [
                # item level 60: Epic[5], Superior[5], Good[5]
                {0: 60, 1: 40, 2: 30, 3: 20, 4: 15, 5: 10, 6: 30, 7: 22, 8: 15, 9: 11, 10: 8,
                 11: 26, 12: 20, 13: 13, 14: 10, 15: 7},
            ]),
        }
        for name, data in files.items():
            with open(os.path.join(cls.tmp.name, name), "wb") as f:
                f.write(data)
        cls.n = GameNames(cls.tmp.name)

    def test_spell_fields_are_read_from_the_cited_offsets(self):
        info = self.n.spell_info(9)
        self.assertEqual(info.effects[1], Effect(69, 21, 0.0, 0, 2000, 10.0, 30.0, 4))
        self.assertEqual((info.proc_chance, info.proc_charges), (30, 3))
        self.assertEqual(self.n.spell_info(433).duration_ms, 18000)
        self.assertEqual(self.n.spell_info(433).effects[0].aura, 84)
        self.assertIsNone(self.n.spell_info(1))

    def test_spell_text_end_to_end_and_unresolved_is_none(self):
        self.assertEqual(self.n.spell_text(433), "Restores 61 health over 18 sec.")
        self.assertEqual(self.n.spell_text(9), "Hits 70 to 90 for 10 yards")
        self.assertIsNone(self.n.spell_text(1))

    def test_item_set_bonuses_are_sorted_by_pieces(self):
        self.assertEqual(self.n.item_set(1), {
            "name": "The Gladiator", "items": [11729, 11726, 11728],
            "bonuses": [(2, 41863), (3, 41864)]})
        self.assertIsNone(self.n.item_set(2))

    def test_random_property_and_suffix_names(self):
        self.assertEqual(self.n.random_property_name(605), "of the Monkey")
        self.assertEqual(self.n.random_property_name(-7), "of the Bear")
        self.assertIsNone(self.n.random_property_name(9999))

    def test_random_property_stats_are_fixed_and_suffix_stats_scale(self):
        self.assertEqual(self.n.random_property_lines(605, 60, 2, 5), ["+8 Agility", "+8 Stamina"])
        # item level 60 uncommon chest: Good[0] = 26 points; Stamina 100%, Strength 66.66%
        self.assertEqual(self.n.random_property_lines(-7, 60, 2, 5), ["+26 Stamina", "+17 Strength"])
        # rare (Superior) neck: propIndex 2 -> 15 points
        self.assertEqual(self.n.random_property_lines(-7, 60, 3, 2), ["+15 Stamina", "+9 Strength"])
        # poor/common items and slots without points get no suffix stats
        self.assertEqual(self.n.random_property_lines(-7, 60, 1, 5), [])
        self.assertEqual(self.n.random_property_lines(-7, 60, 2, 12 + 6), [])  # a bag
        self.assertEqual(self.n.random_property_lines(-7, 61, 2, 5), [])       # no row

    def test_enchant_gem_skill_and_rank_names(self):
        self.assertEqual(self.n.enchant_name(343), "+8 Agility")
        self.assertEqual(self.n.enchant_name(2803, 12), "+12 Stamina")
        self.assertIsNone(self.n.enchant_name(1))
        self.assertEqual(self.n.gem_colors, {343: 2})
        self.assertEqual(self.n.skill_name(164), "Blacksmithing")
        self.assertEqual(self.n.reputation_rank_name(4), "Friendly")
        self.assertEqual(self.n.reputation_rank_name(0), "Hated")
        self.assertIsNone(self.n.reputation_rank_name(8))

    def test_wrong_build_tables_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "ItemSet.dbc"), "wb") as f:
                f.write(build_wdbc(N.ITEMSET_FIELDS + 1, [{0: 1}]))
            with self.assertLogs("dbc", "WARNING"):
                n = GameNames(d)
        self.assertEqual(n.item_sets, {})


@unittest.skipUnless(os.environ.get("DBC_TEST_DIR"), "set DBC_TEST_DIR to real 3.3.5a DBCs")
class RealDbcTests(unittest.TestCase):
    """Ids against the real 12340 client files (Spell.dbc, ItemSet.dbc, ItemRandom*.dbc,
    SpellItemEnchantment.dbc, GemProperties.dbc, SkillLine.dbc, RandPropPoints.dbc, ...)."""

    @classmethod
    def setUpClass(cls):
        cls.n = GameNames(os.environ["DBC_TEST_DIR"])
        if not cls.n.item_sets:
            raise unittest.SkipTest("DBC_TEST_DIR has no ItemSet.dbc")

    def test_spell_text(self):
        t = self.n.spell_text
        self.assertEqual(t(433), "Restores 61 health over 18 sec. Must remain seated while eating.")
        self.assertEqual(t(439), "Restores 70 to 90 health.")       # Minor Healing Potion
        self.assertEqual(t(18125), "Increases Strength by 10 for 10 min.")  # Blessed Sunfruit
        self.assertEqual(t(8690), "Returns you to your home location. Speak to an Innkeeper in a "
                                  "different place to change your home location.")
        self.assertIsNone(t(11556))  # level-scaled: refused, not guessed

    def test_item_set(self):
        s = self.n.item_set(1)
        self.assertEqual((s["name"], s["items"]), ("The Gladiator", [11729, 11726, 11728, 11731, 11730]))
        self.assertEqual([p for p, _ in s["bonuses"]], [2, 3, 4, 5])
        self.assertEqual(self.n.spell_text(s["bonuses"][1][1]), "Increases defense rating by 3.")

    def test_random_properties_and_suffixes(self):
        n = self.n
        self.assertEqual(n.random_property_name(605), "of the Monkey")
        self.assertEqual(n.random_property_lines(605, 1, 2, 5), ["+8 Agility", "+8 Stamina"])
        self.assertEqual(n.random_property_name(-7), "of the Bear")
        # AllocationPct 10000 / 6666 of the item's RandPropPoints
        pts = n.random_property_points(60, 2, 5)
        self.assertGreater(pts, 0)
        self.assertEqual(n.random_property_lines(-7, 60, 2, 5),
                         [f"+{pts} Stamina", f"+{6666 * pts // 10000} Strength"])
        self.assertEqual(n.random_property_points(60, 1, 5), 0)  # common items: none

    def test_enchants_gems_and_skills(self):
        n = self.n
        self.assertEqual(n.enchant_name(2564), "+15 Agility")
        self.assertEqual(n.enchant_name(2686), "+8 Strength")
        self.assertEqual(n.gem_colors[2686], 2)  # red
        self.assertEqual(n.skill_name(164), "Blacksmithing")
        self.assertEqual(n.skill_name(333), "Enchanting")


if __name__ == "__main__":
    unittest.main()
