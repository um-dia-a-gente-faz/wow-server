"""tools/worldknowledge/build.py against agent/worldknowledge.py (ADR 0016, slice 1): the contract test.

The WDBC bytes are built here, small, with the field counts of build 12340. Field indexes are the
ones cited in build.py (TrinityCore 3.3.5 DBCStructure.h).
"""
import contextlib
import hashlib
import json
import os
import pathlib
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools"
BUILD_PY = TOOLS / "worldknowledge" / "build.py"
sys.path.insert(0, str(ROOT / "tools" / "worldknowledge"))
import build  # noqa: E402
from agent.worldknowledge import WorldKnowledge  # noqa: E402


def wdbc(field_count, records):
    """records: list of {field: int | float | str}; other fields are 0."""
    strings, offsets, body = bytearray(b"\x00"), {}, bytearray()
    for rec in records:
        fields = [0] * field_count
        for i, v in rec.items():
            if isinstance(v, str):
                if v not in offsets:
                    offsets[v] = len(strings)
                    strings += v.encode() + b"\x00"
                fields[i] = offsets[v]
            elif isinstance(v, float):
                fields[i] = struct.unpack("<I", struct.pack("<f", v))[0]
            else:
                fields[i] = v & 0xFFFFFFFF
        body += struct.pack(f"<{field_count}I", *fields)
    head = struct.pack("<4sIIII", b"WDBC", len(records), field_count, field_count * 4, len(strings))
    return head + bytes(body) + bytes(strings)


def write_dbcs(d, spell_fields=234, cast_times_fields=4, range_fields=40, cast_base=1500, mana=30,
               power_type=0):
    spells = [
        # ID, CastingTimeIndex 28, SpellLevel 39, PowerType 41, ManaCost 42, RangeIndex 46,
        # Name 136 (enUS slot), NameSubtext 153, ManaCostPct 204
        {0: 133, 28: 4, 39: 1, 41: power_type, 42: mana, 46: 7, 136: "Fireball", 153: "Rank 1", 204: 0},
        {0: 2098, 28: 0, 39: 12, 41: 3, 42: 40, 46: 0, 136: "Eviscerate", 153: "Rank 2", 204: 5},
    ]
    for name, data in {
        "Spell.dbc": wdbc(spell_fields, spells),
        "SpellCastTimes.dbc": wdbc(cast_times_fields, [{0: 4, 1: cast_base}]),
        # ID, RangeMin[hostile, friendly] 1-2, RangeMax[hostile, friendly] 3-4
        "SpellRange.dbc": wdbc(range_fields, [{0: 7, 1: 5.0, 2: 2.0, 3: 35.0, 4: 40.0}]),
    }.items():
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)


class BuildTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.dbc = os.path.join(t.name, "dbc")
        os.mkdir(self.dbc)
        self.out = os.path.join(t.name, "world.sqlite3")

    def test_bundle_is_read_back_column_by_column(self):
        write_dbcs(self.dbc)
        self.assertEqual(build.build(self.dbc, self.out), {"spell": 2})
        r = WorldKnowledge(self.out)
        self.addCleanup(r.close)
        self.assertTrue(r.available)
        self.assertEqual(r.spell(133), {
            "id": 133, "name": "Fireball", "rank": "Rank 1", "spell_level": 1, "power_type": 0,
            "mana_cost": 30, "mana_cost_pct": 0, "cast_time_ms": 1500,
            "range_min_yd": 5.0, "range_max_yd": 35.0, "range_max_friendly_yd": 40.0})
        # index 0 on both tables means instant and no range
        self.assertEqual(r.spell(2098), {
            "id": 2098, "name": "Eviscerate", "rank": "Rank 2", "spell_level": 12, "power_type": 3,
            "mana_cost": 40, "mana_cost_pct": 5, "cast_time_ms": 0,
            "range_min_yd": 0.0, "range_max_yd": 0.0, "range_max_friendly_yd": 0.0})
        self.assertEqual(r.meta()["client_build"], "12340")
        self.assertEqual(len(r.meta()["sha256:Spell.dbc"]), 64)

    def test_wrong_field_count_fails_and_keeps_the_previous_output(self):
        write_dbcs(self.dbc)
        build.build(self.dbc, self.out)
        with open(self.out, "rb") as f:
            before = f.read()
        write_dbcs(self.dbc, spell_fields=233)
        with self.assertRaises(ValueError):
            build.build(self.dbc, self.out)
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.out))), ["dbc", "world.sqlite3"])

    def test_rebuild_replaces_the_output(self):
        write_dbcs(self.dbc)
        build.build(self.dbc, self.out)
        self.assertEqual(build.build(self.dbc, self.out), {"spell": 2})

    def test_negative_cast_time_is_read_signed(self):
        write_dbcs(self.dbc, cast_base=-1000)   # SpellCastTimesEntry.Base is int32 (DBCStructure.h:1508)
        build.build(self.dbc, self.out)
        r = WorldKnowledge(self.out)
        self.addCleanup(r.close)
        self.assertEqual(r.spell(133)["cast_time_ms"], -1000)

    def test_power_type_is_read_unsigned(self):
        # SpellEntry.PowerType is uint32 (DBCStructure.h:1432); POWER_HEALTH (-2) is stored as 0xFFFFFFFE
        write_dbcs(self.dbc, power_type=0xFFFFFFFE)
        build.build(self.dbc, self.out)
        r = WorldKnowledge(self.out)
        self.addCleanup(r.close)
        self.assertEqual(r.spell(133)["power_type"], 4294967294)

    def test_source_hashes_are_of_the_source_files(self):
        write_dbcs(self.dbc)
        build.build(self.dbc, self.out)
        meta = self.meta()
        for n in ("Spell.dbc", "SpellCastTimes.dbc", "SpellRange.dbc"):
            with open(os.path.join(self.dbc, n), "rb") as f:
                self.assertEqual(meta[f"sha256:{n}"], hashlib.sha256(f.read()).hexdigest())
        first = meta["sha256:Spell.dbc"]
        write_dbcs(self.dbc, mana=31)
        build.build(self.dbc, self.out)
        self.assertNotEqual(self.meta()["sha256:Spell.dbc"], first)

    def test_output_is_world_readable_whatever_the_umask(self):
        write_dbcs(self.dbc)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        build.build(self.dbc, self.out)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o644)

    def test_tables_row_marks_spell_as_client_tier(self):
        write_dbcs(self.dbc)
        build.build(self.dbc, self.out)
        with contextlib.closing(sqlite3.connect(self.out)) as conn:
            self.assertEqual(conn.execute("SELECT name, tier FROM tables").fetchall(), [("spell", "client")])

    def test_wrong_field_count_in_each_table_fails_and_keeps_output(self):
        for name, bad in {"SpellCastTimes.dbc": {"cast_times_fields": 3},
                          "SpellRange.dbc": {"range_fields": 39}}.items():
            with self.subTest(name):
                write_dbcs(self.dbc)
                build.build(self.dbc, self.out)
                with open(self.out, "rb") as f:
                    before = f.read()
                write_dbcs(self.dbc, **bad)
                with self.assertRaises(ValueError):
                    build.build(self.dbc, self.out)
                with open(self.out, "rb") as f:
                    self.assertEqual(f.read(), before)

    def test_import_appends_to_sys_path_and_leaves_its_head_alone(self):
        code = ("import importlib.util, json, sys; before = list(sys.path); "
                f"spec = importlib.util.spec_from_file_location('build', {str(BUILD_PY)!r}); "
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                "print(json.dumps([before, sys.path]))")
        out = subprocess.run([sys.executable, "-c", code], cwd="/", capture_output=True, text=True,
                             check=True).stdout
        before, after = json.loads(out)
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(after[len(before):], [str(TOOLS), str(ROOT)])

    def meta(self):
        r = WorldKnowledge(self.out)
        self.addCleanup(r.close)
        return r.meta()


if __name__ == "__main__":
    unittest.main()
