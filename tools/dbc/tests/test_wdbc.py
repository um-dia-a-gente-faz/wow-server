import os
import pathlib
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # tools/
from dbc import names as names_mod  # noqa: E402
from dbc.names import GameNames, reputation_tier  # noqa: E402
from dbc.wdbc import WdbcError, WdbcFile  # noqa: E402


def build_wdbc(field_count, records):
    """records: list of {field_index: int | float | str}; other fields are 0."""
    strings = bytearray(b"\x00")
    offsets = {}
    body = bytearray()
    for rec in records:
        fields = [0] * field_count
        for i, v in rec.items():
            if isinstance(v, str):
                if v not in offsets:
                    offsets[v] = len(strings)
                    strings += v.encode("utf-8") + b"\x00"
                fields[i] = offsets[v]
            elif isinstance(v, float):
                fields[i] = struct.unpack("<I", struct.pack("<f", v))[0]
            else:
                fields[i] = v & 0xFFFFFFFF
        body += struct.pack(f"<{field_count}I", *fields)
    header = struct.pack("<4sIIII", b"WDBC", len(records), field_count,
                         field_count * 4, len(strings))
    return bytes(header + body + strings)


class WdbcFileTests(unittest.TestCase):
    def setUp(self):
        self.f = WdbcFile(build_wdbc(4, [
            {0: 7, 1: -5, 2: 1.5, 3: "Stormwind"},
            {0: 9, 1: 3, 2: -0.25},
        ]))

    def test_header(self):
        self.assertEqual((self.f.record_count, self.f.field_count, self.f.record_size), (2, 4, 16))

    def test_typed_fields(self):
        self.assertEqual(self.f.uint(0, 0), 7)
        self.assertEqual(self.f.int(0, 1), -5)
        self.assertEqual(self.f.uint(0, 1), 0xFFFFFFFB)
        self.assertEqual(self.f.float(0, 2), 1.5)
        self.assertEqual(self.f.float(1, 2), -0.25)

    def test_strings(self):
        self.assertEqual(self.f.string(0, 3), "Stormwind")
        self.assertEqual(self.f.string(1, 3), "")  # offset 0 is the empty string

    def test_out_of_range(self):
        with self.assertRaises(IndexError):
            self.f.uint(2, 0)
        with self.assertRaises(IndexError):
            self.f.uint(0, 4)

    def test_rejects_bad_files(self):
        good = build_wdbc(2, [{0: 1}])
        with self.assertRaises(WdbcError):
            WdbcFile(b"WDB")
        with self.assertRaises(WdbcError):
            WdbcFile(b"XDBC" + good[4:])
        with self.assertRaises(WdbcError):
            WdbcFile(good[:-2])

    def test_locale_detection_and_fallback(self):
        # Localised column at fields 1..16; slot 0 = enUS. A ptBR-style client fills
        # one other slot; records it leaves empty fall back to enUS.
        f = WdbcFile(build_wdbc(18, [
            {0: 1, 1: "Fireball", 9: "Bola de Fogo"},
            {0: 2, 9: "Raio Gélido"},
            {0: 3, 1: "Only English"},
            {0: 4, 9: "Explosão Arcana"},
        ]))
        slot = f.detect_locale(1)
        self.assertEqual(slot, 8)
        self.assertEqual(f.loc_string(0, 1, slot), "Bola de Fogo")
        self.assertEqual(f.loc_string(1, 1, slot), "Raio Gélido")
        self.assertEqual(f.loc_string(2, 1, slot), "Only English")
        self.assertEqual(f.loc_string(0, 1, 0), "Fireball")

    def test_locale_defaults_to_enus_when_empty(self):
        f = WdbcFile(build_wdbc(18, [{0: 1}]))
        self.assertEqual(f.detect_locale(1), 0)


class ReputationTierTests(unittest.TestCase):
    def test_thresholds_match_trinitycore(self):
        cases = [(-42000, "Hated"), (-6001, "Hated"), (-6000, "Hostile"),
                 (-3000, "Unfriendly"), (-1, "Unfriendly"), (0, "Neutral"),
                 (2999, "Neutral"), (3000, "Friendly"), (9000, "Honored"),
                 (21000, "Revered"), (41999, "Revered"), (42000, "Exalted"),
                 (42999, "Exalted"), (-50000, "Hated")]
        for value, tier in cases:
            self.assertEqual(reputation_tier(value), tier, value)


class GameNamesTests(unittest.TestCase):
    """Synthetic DBCs with the build-12340 field counts and the cited offsets."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        files = {
            "Spell.dbc": build_wdbc(names_mod.SPELL_FIELDS, [
                {0: 12282, 133: 132, 136: "Improved Heroic Strike", 153: "Rank 1"},
                {0: 12663, 133: 132, 136: "Improved Heroic Strike", 153: "Rank 2"},
                {0: 133, 133: 185, 136: "Fireball", 153: "Rank 1"},
            ]),
            "Talent.dbc": build_wdbc(names_mod.TALENT_FIELDS, [
                {0: 124, 1: 161, 4: 12282, 5: 12663, 6: 12664},
            ]),
            "TalentTab.dbc": build_wdbc(names_mod.TALENTTAB_FIELDS, [
                {0: 161, 1: "Arms", 20: 1, 22: 0},
            ]),
            "Faction.dbc": build_wdbc(names_mod.FACTION_FIELDS, [
                # Stormwind: Alliance races start Friendly (4000), Horde Hated.
                {0: 72, 1: 7, 2: 1101, 3: 690, 10: 4000, 11: -42000, 23: "Stormwind"},
                # class-only slot: no race mask, class mask 4 (hunter)
                {0: 500, 1: 8, 6: 4, 10: 3000, 23: "Hunters Only"},
            ]),
            "Achievement.dbc": build_wdbc(names_mod.ACHIEVEMENT_FIELDS, [
                {0: 6, 1: -1, 4: "Level 10", 38: 92, 39: 10},
            ]),
        }
        for name, data in files.items():
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)
        cls.names = GameNames(d)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_spell(self):
        self.assertEqual(self.names.spell(133), {"name": "Fireball", "rank": "Rank 1", "icon": 185})
        self.assertIsNone(self.names.spell(1))

    def test_talent_resolves_tree_and_rank(self):
        t = self.names.talent(12663)
        self.assertEqual((t["name"], t["tree"], t["rank"], t["talent"]),
                         ("Improved Heroic Strike", "Arms", 2, 124))
        # rank 3 exists in Talent.dbc but not in this Spell.dbc: tree still known
        t = self.names.talent(12664)
        self.assertEqual((t["name"], t["tree"], t["rank"]), (None, "Arms", 3))
        self.assertIsNone(self.names.talent(133))  # a spell, not a talent

    def test_faction_and_reputation_base_by_race(self):
        self.assertEqual(self.names.faction_name(72), "Stormwind")
        human = self.names.reputation(72, 0, race=1, cls=1)
        self.assertEqual(human, {"faction_name": "Stormwind", "value": 4000, "tier": "Friendly"})
        orc = self.names.reputation(72, 500, race=2, cls=1)  # 690 includes orc (bit 1)
        self.assertEqual((orc["value"], orc["tier"]), (-41500, "Hated"))
        blood_elf = self.names.reputation(72, 0, race=10, cls=2)  # 690 has bit 9 too
        self.assertEqual(blood_elf["tier"], "Hated")
        unmatched = self.names.reputation(72, 100, race=9, cls=1)  # in neither mask
        self.assertEqual((unmatched["value"], unmatched["tier"]), (100, "Neutral"))

    def test_reputation_class_only_slot(self):
        self.assertEqual(self.names.reputation(500, 0, race=1, cls=3)["value"], 3000)
        self.assertEqual(self.names.reputation(500, 0, race=1, cls=1)["value"], 0)

    def test_unknown_faction_keeps_standing(self):
        self.assertEqual(self.names.reputation(9999, 3500, 1, 1),
                         {"faction_name": None, "value": 3500, "tier": "Friendly"})

    def test_achievement(self):
        self.assertEqual(self.names.achievement(6), {"name": "Level 10", "points": 10})
        self.assertIsNone(self.names.achievement(7))

    def test_missing_and_wrong_build_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "Faction.dbc"), "wb") as f:
                f.write(build_wdbc(names_mod.FACTION_FIELDS - 1, [{0: 72}]))
            with self.assertLogs("dbc", level="WARNING"):
                n = GameNames(d)
        self.assertEqual((n.spells, n.factions, n.achievements), ({}, {}, {}))
        self.assertIsNone(n.faction_name(72))


@unittest.skipUnless(os.environ.get("DBC_TEST_DIR"), "set DBC_TEST_DIR to real 3.3.5a DBCs")
class RealDbcTests(unittest.TestCase):
    """Known ids against the real client files (e.g. a copy of /opt/wowmap-data/dbc)."""

    @classmethod
    def setUpClass(cls):
        cls.names = GameNames(os.environ["DBC_TEST_DIR"])

    def test_known_ids(self):
        n = self.names
        self.assertEqual(n.faction_name(72), "Stormwind")
        self.assertEqual(n.faction_name(911), "Silvermoon City")
        self.assertEqual(n.spell(133), {"name": "Fireball", "rank": "Rank 1", "icon": 185})
        self.assertEqual(n.achievement(6), {"name": "Level 10", "points": 10})
        t = n.talent(12282)
        self.assertEqual((t["name"], t["tree"], t["rank"]), ("Improved Heroic Strike", "Arms", 1))
        self.assertEqual(n.reputation(72, 0, race=1, cls=1)["tier"], "Friendly")
        self.assertEqual(n.reputation(72, 0, race=10, cls=2)["tier"], "Hated")


if __name__ == "__main__":
    unittest.main()
