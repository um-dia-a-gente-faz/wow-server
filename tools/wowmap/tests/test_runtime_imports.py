"""Every runtime module imports with only the runtime dir (plus tools/ for dbc) on the path (#392).

A module that quietly depends on pipeline/ or a stray file shows up here, in CI, not on the VM.
"""
import pathlib
import subprocess
import sys
import unittest

RUNTIME = pathlib.Path(__file__).resolve().parents[1] / "wowmap"
TOOLS = RUNTIME.parents[1]


class RuntimeImports(unittest.TestCase):
    def test_every_runtime_module_imports_in_a_clean_interpreter(self):
        mods = sorted(p.stem for p in RUNTIME.glob("*.py")) + ["repo.players", "repo.characters"]
        code = "import importlib\n" + "".join(f"importlib.import_module({m!r})\n" for m in mods)
        r = subprocess.run([sys.executable, "-c", code], cwd=RUNTIME, capture_output=True,
                           text=True, env={"PYTHONPATH": f"{RUNTIME}:{TOOLS}"})
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
