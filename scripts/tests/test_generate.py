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


if __name__ == "__main__":
    unittest.main()
