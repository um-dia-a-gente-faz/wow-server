"""Guard (#378): the legacy tier of mypy.ini may not gain errors (scripts/mypy_legacy_baseline.py).

The checkers take text and dicts, so these tests feed synthetic mypy output. No test runs mypy.
The module-name tests use real paths from the repo and the real mypy.ini.
"""
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import mypy_legacy_baseline as mlb  # noqa: E402

REAL_INI = (ROOT / "mypy.ini").read_text()
LEGACY = mlb.legacy_modules(REAL_INI)
BASES = mlb.mypy_path_bases(REAL_INI)


class ModuleNames(unittest.TestCase):
    """Mypy names a file by the longest mypy_path base holding it (explicit_package_bases)."""

    def test_real_paths_map_to_mypy_ini_section_names(self):
        cases = {
            "agent/session.py": "agent.session",
            "agent/state.py": "agent.state",
            "agent/actions/chat.py": "agent.actions.chat",
            "tools/dbc/names.py": "dbc.names",
            "tools/loadtest/soak.py": "loadtest.soak",
            "tools/wowmap/wowmap/app.py": "app",
            "tools/wowmap/wowmap/state.py": "state",
            "tools/wowmap/pipeline/calibrate.py": "calibrate",
            "tools/wowmap/wowmap/repo/players.py": "repo.players",
        }
        for path, module in cases.items():
            with self.subTest(path=path):
                self.assertEqual(mlb.module_of(path, BASES), module)

    def test_real_legacy_examples_are_legacy(self):
        for module in ("agent.session", "dbc.names", "loadtest.soak", "app", "state"):
            with self.subTest(module=module):
                self.assertIn(module, LEGACY)

    def test_package_init_is_named_by_its_package(self):
        self.assertEqual(mlb.module_of("tools/dbc/__init__.py", BASES), "dbc")

    def test_same_basename_gets_distinct_names(self):
        self.assertNotEqual(mlb.module_of("agent/state.py", BASES),
                            mlb.module_of("tools/wowmap/wowmap/state.py", BASES))


class LegacyTier(unittest.TestCase):
    def test_legacy_is_the_ignore_errors_sections(self):
        ini = "[mypy]\nfiles = agent\n\n[mypy-a.b]\nignore_errors = True\n\n[mypy-c]\ndisallow_untyped_defs = True\n"
        self.assertEqual(mlb.legacy_modules(ini), {"a.b"})

    def test_real_legacy_set_is_read_from_mypy_ini(self):
        self.assertIn("agent.names", LEGACY)
        self.assertNotIn("agent.httpjson", LEGACY)  # strict tier

    def test_unlegacy_config_drops_only_the_ignore_errors_lines(self):
        stripped = mlb.unlegacy_config(REAL_INI)
        self.assertEqual(mlb.legacy_modules(stripped), set())
        kept = [line for line in REAL_INI.splitlines(keepends=True)
                if not mlb.IGNORE_ERRORS.match(line)]
        self.assertEqual(stripped.splitlines(keepends=True), kept)
        self.assertIn("follow_imports = silent", stripped)


class CountErrors(unittest.TestCase):
    def count(self, output):
        return mlb.count_legacy_errors(output, LEGACY, BASES)

    def test_errors_in_a_legacy_module_are_counted(self):
        out = ("agent/names.py:3: error: Incompatible types in assignment  [assignment]\n"
               "agent/names.py:9: error: Name \"x\" is not defined  [name-defined]\n")
        self.assertEqual(self.count(out)["agent.names"], 2)

    def test_non_legacy_errors_and_notes_are_ignored(self):
        out = ("agent/httpjson.py:1: error: strict module  [no-untyped-def]\n"
               "agent/fresh.py:1: error: new module  [misc]\n"
               "agent/names.py:4: note: see docs\n"
               "Found 3 errors in 2 files (checked 133 source files)\n")
        counts = self.count(out)
        self.assertEqual(counts["agent.names"], 0)
        self.assertEqual(set(counts), LEGACY)

    def test_every_legacy_module_is_present_even_with_no_errors(self):
        self.assertEqual(self.count(""), dict.fromkeys(LEGACY, 0))

    def test_wowmap_and_agent_state_are_counted_separately(self):
        out = ("tools/wowmap/wowmap/state.py:1: error: a  [misc]\n"
               "agent/state.py:1: error: b  [misc]\n"
               "agent/state.py:2: error: c  [misc]\n")
        counts = self.count(out)
        self.assertEqual(counts["state"], 1)
        self.assertEqual(counts["agent.state"], 2)


class FullRun(unittest.TestCase):
    """A run that did not finish must not be counted as zero errors."""

    def test_clean_and_error_runs_are_accepted(self):
        mlb.full_run(0, "Success: no issues found in 133 source files\n")
        out = "agent/names.py:1: error: a  [misc]\nFound 1 error in 1 file (checked 2 source files)\n"
        self.assertEqual(mlb.full_run(1, out), out)

    def test_crash_or_missing_summary_is_refused(self):
        with self.assertRaises(SystemExit):
            mlb.full_run(2, "Success: no issues found in 133 source files\n")
        with self.assertRaises(SystemExit):
            mlb.full_run(1, "Traceback (most recent call last):\nModuleNotFoundError: mypy\n")


class Compare(unittest.TestCase):
    def test_module_that_gains_an_error_fails_with_its_name(self):
        failures, notes = mlb.compare({"agent.names": 12}, {"agent.names": 13})
        self.assertEqual(failures, ["agent.names: baseline 12, now 13"])
        self.assertEqual(notes, [])

    def test_legacy_module_missing_from_baseline_fails(self):
        failures, _ = mlb.compare({}, {"agent.names": 0})
        self.assertEqual(len(failures), 1)
        self.assertIn("agent.names", failures[0])

    def test_stale_key_fails_and_says_remove_it(self):
        failures, _ = mlb.compare({"agent.names": 0, "agent.gone": 0}, {"agent.names": 0})
        self.assertEqual(len(failures), 1)
        self.assertIn("agent.gone", failures[0])
        self.assertIn("remove", failures[0])

    def test_decrease_passes_with_a_note(self):
        failures, notes = mlb.compare({"agent.names": 12}, {"agent.names": 10})
        self.assertEqual(failures, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("agent.names", notes[0])
        self.assertIn("--update", notes[0])

    def test_unchanged_passes_silently(self):
        self.assertEqual(mlb.compare({"agent.names": 3}, {"agent.names": 3}), ([], []))


class CommittedBaseline(unittest.TestCase):
    def test_json_has_one_key_per_real_legacy_module(self):
        path = ROOT / "mypy-legacy-baseline.json"
        self.assertTrue(path.exists(), "mypy-legacy-baseline.json missing; run --update")
        data = json.loads(path.read_text())
        self.assertEqual(set(data), LEGACY)
        self.assertTrue(all(isinstance(v, int) and v >= 0 for v in data.values()))


if __name__ == "__main__":
    unittest.main()
