"""get_spell_info with a world bundle (ADR 0016 further slice 1, #742): the bundle corrects the
numbers of SPELL_TABLE entries, never widens the set, never does worse than the table."""
import logging
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent import root
from agent import spells as sp
from agent import worldknowledge as wk
from agent.config import SETTINGS, Config

# Fireball (133) in the table: Fireball, mage, 1, 30, MANA, 2500, 30.0
FIREBALL = (133, "Fireball Bundle", "Rank 1", 3, 3, 28, 0, 2400, 0.0, 36.0, 36.0)


def make_bundle(path, rows, version=wk.SCHEMA_VERSION):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(wk.SCHEMA)
    for r in rows:
        conn.execute("INSERT INTO spell VALUES (?,?,?,?,?,?,?,?,?,?,?)", r)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(sp.set_world_knowledge, None)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "bundle.sqlite")

    def open(self, rows, **kw):
        make_bundle(self.path, rows, **kw)
        reader = wk.WorldKnowledge(self.path)
        self.addCleanup(reader.close)
        sp.set_world_knowledge(reader.spell)
        return reader


class GetSpellInfoTest(Base):
    def test_no_bundle_is_the_table(self):
        for sid, entry in sp.SPELL_TABLE.items():
            self.assertEqual(sp.get_spell_info(sid), entry)

    def test_bundle_numbers_replace_and_table_fields_stay(self):
        self.open([FIREBALL])
        got, base = sp.get_spell_info(133), sp.SPELL_TABLE[133]
        self.assertEqual((got.min_level, got.power_cost, got.power_type, got.cast_time_ms,
                          got.range_yd), (3, 28, 3, 2400, 36.0))
        self.assertIsInstance(got.range_yd, float)
        self.assertEqual((got.spell_id, got.class_, got.description, got.name),
                         (base.spell_id, base.class_, base.description, base.name))

    def test_percent_cost_keeps_table_cost(self):
        self.open([FIREBALL[:5] + (99, 5) + FIREBALL[7:]])
        self.assertEqual(sp.get_spell_info(133).power_cost, 30)

    def test_spell_level_zero_keeps_table_min_level(self):
        self.open([(133, "Fireball", "", 0) + FIREBALL[4:]])
        self.assertEqual(sp.get_spell_info(133).min_level, 1)

    def test_id_outside_the_table_stays_none(self):
        self.open([(999999, "Passive", "", 1, 0, 0, 0, 0, 0.0, 0.0, 0.0)])
        self.assertIsNone(sp.get_spell_info(999999))

    def test_id_missing_from_bundle_is_the_table_entry(self):
        self.open([FIREBALL])
        self.assertEqual(sp.get_spell_info(116), sp.SPELL_TABLE[116])

    def test_unavailable_bundle_is_the_table(self):
        sp.set_world_knowledge(wk.WorldKnowledge(os.path.join(self.dir.name, "missing.sqlite")).spell)
        self.assertEqual(sp.get_spell_info(133), sp.SPELL_TABLE[133])
        self.open([FIREBALL], version=99)
        self.assertEqual(sp.get_spell_info(133), sp.SPELL_TABLE[133])

    def test_reader_that_raises_is_guarded_by_the_roots_wrapper(self):
        # spells.py holds no broad except (the architecture rule); root's wrapper is the guard.
        reader = mock.Mock(spell=mock.Mock(side_effect=RuntimeError("boom")))
        sp.set_world_knowledge(root.guarded_lookup(reader, logging.getLogger("test.742")))
        with self.assertLogs("test.742", "ERROR"):
            self.assertEqual(sp.get_spell_info(133), sp.SPELL_TABLE[133])

    def test_wrong_typed_fields_keep_table_values(self):
        row = dict(zip(wk.SPELL_COLUMNS, FIREBALL, strict=True))
        row.update(name=None, power_type="x", cast_time_ms=None, range_max_yd="far", spell_level=None,
                   mana_cost="many")
        sp.set_world_knowledge(lambda _id: row)
        self.assertEqual(sp.get_spell_info(133), sp.SPELL_TABLE[133])
        sp.set_world_knowledge(lambda _id: "junk")
        self.assertEqual(sp.get_spell_info(133), sp.SPELL_TABLE[133])

    def test_lookups_are_cached(self):
        reader = self.open([FIREBALL])
        with mock.patch.object(reader, "spell", wraps=reader.spell) as spy:
            sp.set_world_knowledge(reader.spell)
            self.assertIs(sp.get_spell_info(133), sp.get_spell_info(133))
        self.assertEqual(spy.call_count, 1)

    def test_setter_clears_the_cache_and_none_restores_the_table(self):
        self.open([FIREBALL])
        self.assertEqual(sp.get_spell_info(133).power_cost, 28)
        other = os.path.join(self.dir.name, "b2.sqlite")
        make_bundle(other, [FIREBALL[:5] + (11,) + FIREBALL[6:]])
        r2 = wk.WorldKnowledge(other)
        self.addCleanup(r2.close)
        sp.set_world_knowledge(r2.spell)
        self.assertEqual(sp.get_spell_info(133).power_cost, 11)
        sp.set_world_knowledge(None)
        self.assertEqual(sp.get_spell_info(133), sp.SPELL_TABLE[133])


class RealRowTest(Base):
    def test_fractional_values_in_int_columns_keep_the_table(self):
        # SQLite keeps 1.5 in an INTEGER column; a float power_type raised in CastSpellAction.check.
        self.open([(133, "Fireball", "", 1.5, 1.5, 1.5, 0, 1.5, 0.0, 36.0, 36.0)])
        got, base = sp.get_spell_info(133), sp.SPELL_TABLE[133]
        self.assertEqual((got.power_type, got.power_cost, got.cast_time_ms, got.min_level),
                         (base.power_type, base.power_cost, base.cast_time_ms, base.min_level))
        self.assertEqual(got.range_yd, 36.0)

    def test_fractional_percent_keeps_table_cost(self):
        self.open([FIREBALL[:6] + (0.5,) + FIREBALL[7:]])   # SQLite keeps 0.5; it is not the int 0
        self.assertEqual(sp.get_spell_info(133).power_cost, 30)

    def test_negative_spell_level_keeps_min_level(self):
        self.open([FIREBALL[:3] + (-2,) + FIREBALL[4:]])
        self.assertEqual(sp.get_spell_info(133).min_level, 1)

    def test_name_is_never_applied(self):
        self.open([(133, "") + FIREBALL[2:], (143,) + FIREBALL[1:]])
        self.assertEqual(sp.get_spell_info(133).name, "Fireball")
        self.assertEqual(sp.get_spell_info(143).name, "Fireball Rank 2")


class StartupTest(Base):
    def cfg(self, path):
        return mock.Mock(world_bundle=path)

    def test_empty_setting_touches_nothing(self):
        log = logging.getLogger("test.742")
        with mock.patch.object(root, "WorldKnowledge", side_effect=AssertionError("constructed")), \
                mock.patch("sqlite3.connect", side_effect=AssertionError("opened")), \
                self.assertNoLogs(log, level=0), self.assertNoLogs("agent", level=0):
            root.load_world_bundle(self.cfg(""), log)
            root.load_world_bundle(object(), log)   # a config with no such field
        self.assertIsNone(sp._world[0])
        self.assertEqual(sp.get_spell_info(133), sp.SPELL_TABLE[133])

    def test_set_path_wires_the_reader_and_logs_the_comparison(self):
        make_bundle(self.path, [FIREBALL])
        log = logging.getLogger("test.742")
        opened = []

        def real(path):
            opened.append(wk.WorldKnowledge(path))
            self.addCleanup(opened[-1].close)
            return opened[-1]
        with mock.patch.object(root, "WorldKnowledge", side_effect=real), \
                self.assertLogs(log, level="INFO") as cm:
            root.load_world_bundle(self.cfg(self.path), log)
        self.assertEqual(sp.get_spell_info(133).power_cost, 28)
        self.assertEqual({r.levelname for r in cm.records}, {"INFO"})
        msgs = [r.getMessage() for r in cm.records]
        self.assertEqual(msgs[0], f"world bundle has 1 of {len(sp.SPELL_TABLE)} SPELL_TABLE ids")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1], "world bundle differs for spell 133: name table 'Fireball' bundle "
                         "'Fireball Bundle' rank 'Rank 1'; min_level table 1 bundle 3; "
                         "power_cost table 30 bundle 28; power_type table 0 bundle 3; "
                         "cast_time_ms table 2500 bundle 2400; range_yd table 30.0 bundle 36.0")

    def test_root_installs_the_guarded_lookup_not_the_raw_reader(self):
        reader = mock.Mock(spell=mock.Mock(side_effect=RuntimeError("boom")))
        log = logging.getLogger("test.742")
        with mock.patch.object(root, "WorldKnowledge", return_value=reader), \
                self.assertLogs(log, level="INFO"):
            root.load_world_bundle(self.cfg("x"), log)   # must not raise
        self.assertEqual(sp.get_spell_info(133), sp.SPELL_TABLE[133])

    def test_identical_rows_log_no_difference(self):
        base = sp.SPELL_TABLE[133]
        self.open([(133, base.name, "", base.min_level, base.power_type, base.power_cost, 0,
                    base.cast_time_ms, 0.0, base.range_yd, base.range_yd)])
        self.assertEqual(sp.compare_with_table(sp._world[0]),
                         [f"world bundle has 1 of {len(sp.SPELL_TABLE)} SPELL_TABLE ids"])

    def test_name_difference_is_logged_not_applied(self):
        self.open([(143,) + FIREBALL[1:]])
        self.assertEqual(sp.get_spell_info(143).name, "Fireball Rank 2")
        self.assertIn("name table 'Fireball Rank 2' bundle 'Fireball Bundle' rank 'Rank 1'",
                      sp.compare_with_table(sp._world[0])[1])

    def test_comparison_without_rows(self):
        self.assertEqual(sp.compare_with_table(lambda _id: None),
                         [f"world bundle has 0 of {len(sp.SPELL_TABLE)} SPELL_TABLE ids"])


class BuildAgentTest(Base):
    """A real Config from the environment and a real build_agent: the setting reaches get_spell_info."""

    def build(self, env):
        audit = tempfile.TemporaryDirectory()
        self.addCleanup(audit.cleanup)
        env = {"AGENT_AUDIT_DIR": audit.name, "AGENT_HTTP_PORT": "0", **env}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = Config()
        made = []

        def real(path):
            made.append(wk.WorldKnowledge(path))
            self.addCleanup(made[-1].close)
            return made[-1]
        with mock.patch.object(root, "WorldKnowledge", side_effect=real):
            agent = root.build_agent(cfg, logging.getLogger("test.742.build"))
        self.addCleanup(agent.close)

    def test_setting_in_the_environment_corrects_the_spell(self):
        make_bundle(self.path, [FIREBALL])
        self.build({"AGENT_WORLD_BUNDLE": self.path})
        self.assertEqual(sp.get_spell_info(133).power_cost, 28)

    def test_setting_empty_leaves_the_table(self):
        self.build({})
        self.assertEqual(sp.get_spell_info(133), sp.SPELL_TABLE[133])


class SettingTest(unittest.TestCase):
    def test_setting_is_off_by_default_and_not_a_compose_variable(self):
        s = next(x for x in SETTINGS if x.name == "AGENT_WORLD_BUNDLE")
        self.assertEqual((s.default, s.compose), ("", False))
