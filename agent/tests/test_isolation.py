"""#437: every agent test module must pass when run on its own. A module that only
passes in suite order hides import-order bugs (test_candidates relied on another
module registering reclaim_corpse). Each module runs in a fresh interpreter by
dotted name, the same way a developer runs it."""

import pathlib
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
TESTS = pathlib.Path(__file__).resolve().parent
SELF = pathlib.Path(__file__).stem


def modules() -> list:
    return sorted(p.stem for p in TESTS.glob("test_*.py") if p.stem != SELF)


class ModuleIsolationTest(unittest.TestCase):
    def test_each_module_passes_alone(self):
        for name in modules():
            with self.subTest(module=name):
                proc = subprocess.run(
                    [sys.executable, "-m", "unittest", f"agent.tests.{name}"],
                    cwd=REPO, capture_output=True, text=True, timeout=300)
                self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
