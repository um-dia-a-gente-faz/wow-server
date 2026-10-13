"""#297: every generated file is up to date, and drift names the generator to run."""
import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
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

    def _tree(self) -> Path:
        """#717: a throwaway copy of what the generators read and write, laid out like the repo.
        Every generator finds its files from its own path, so running the copy's generate.py
        keeps the checkout untouched (a stale agent/__pycache__ or a half-edit survives nothing)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tree = Path(tmp.name)
        skip = shutil.ignore_patterns("tests", "__pycache__", "wow-agents", "*.pyc")
        for d in ("scripts", "agent", "agents"):
            shutil.copytree(ROOT / d, tree / d, ignore=skip)
        (tree / "monitoring").mkdir()
        for f in ROOT.glob("monitoring/grafana-dashboard-*.json"):
            shutil.copy(f, tree / "monitoring")
        for f in ("docker-compose.agents.yml", "docs/AGENT-API.md"):
            (tree / f).parent.mkdir(exist_ok=True)
            shutil.copy(ROOT / f, tree / f)
        return tree

    @staticmethod
    def _generate(tree: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(tree / "scripts/generate.py"), *args],
                              cwd=tree, capture_output=True, text=True)

    def test_write_run_repairs_drift(self):
        tree = self._tree()
        doc = tree / "docs/AGENT-API.md"
        good = doc.read_text()
        doc.write_text(good + "hand edit\n")
        self.assertEqual(self._generate(tree, "--check").returncode, 1)
        self._generate(tree)
        self.assertEqual(doc.read_text(), good)
        self.assertEqual(self._generate(tree, "--check").returncode, 0)

    def test_protocol_tables_generator_is_registered(self):
        """Verify that scripts/gen_protocol_tables.py is in GENERATORS (#381)."""
        self.assertIn(["scripts/gen_protocol_tables.py"], generate.GENERATORS)

    def test_protocol_table_drift_is_detected(self):
        """Verify that generate --check fails when agent/opcodes.py is hand-edited (#381)."""
        tree = self._tree()
        opcodes = tree / "agent" / "opcodes.py"
        good = opcodes.read_text()
        edited = good.replace("= 0x10A", "= 0x0A8")
        self.assertNotEqual(edited, good, "drift anchor '= 0x10A' not found in agent/opcodes.py")
        opcodes.write_text(edited)
        run = self._generate(tree, "--check")
        self.assertEqual(run.returncode, 1)
        self.assertIn("gen_protocol_tables.py", run.stderr)
        self._generate(tree)
        self.assertEqual(opcodes.read_text(), good)


if __name__ == "__main__":
    unittest.main()
