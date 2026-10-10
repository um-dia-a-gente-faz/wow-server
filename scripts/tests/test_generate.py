"""#297: every generated file is up to date, and drift names the generator to run."""
import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import generate  # noqa: E402
import genlib  # noqa: E402


class GenerateTest(unittest.TestCase):
    def test_committed_files_are_up_to_date(self):
        self.assertEqual(generate.main(["--check"]), 0)

    def test_drift_names_the_generator(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = genlib.sync("monitoring/grafana-dashboard-wow-players.json", "hand edit", True, "scripts/gen-x.py")
        self.assertEqual(rc, 1)
        self.assertIn("scripts/generate.py", err.getvalue())
        self.assertIn("scripts/gen-x.py", err.getvalue())

    def test_write_run_repairs_drift(self):
        doc = ROOT / "docs/AGENT-API.md"
        good = doc.read_text()
        try:
            doc.write_text(good + "hand edit\n")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(generate.main(["--check"]), 1)
            generate.main([])
            self.assertEqual(doc.read_text(), good)
            self.assertEqual(generate.main(["--check"]), 0)
        finally:
            doc.write_text(good)

    def test_protocol_tables_generator_is_registered(self):
        """Verify that scripts/gen_protocol_tables.py is in GENERATORS (#381)."""
        self.assertIn(["scripts/gen_protocol_tables.py"], generate.GENERATORS)

    def test_protocol_table_drift_is_detected(self):
        """Verify that generate --check fails when agent/opcodes.py is hand-edited (#381)."""
        opcodes = ROOT / "agent" / "opcodes.py"
        good = opcodes.read_text()
        try:
            # Introduce a drift in the generated section by editing a value
            edited = good.replace("= 0x10A", "= 0x0A8")
            self.assertNotEqual(edited, good, "drift anchor '= 0x10A' not found in agent/opcodes.py")
            opcodes.write_text(edited)
            # Verify that generate --check detects the drift
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = generate.main(["--check"])
            self.assertEqual(rc, 1)
            self.assertIn("gen_protocol_tables.py", err.getvalue())
        finally:
            opcodes.write_text(good)


if __name__ == "__main__":
    unittest.main()
