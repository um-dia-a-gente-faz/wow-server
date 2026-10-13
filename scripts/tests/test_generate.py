"""#297: every generated file is up to date, and drift names the generator to run."""
import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import generate  # noqa: E402
import genlib  # noqa: E402

END = "# --- END GENERATED ---"  # agent/opcodes.py: the marker closing the generated block

# #725: one stale output per generator in GENERATORS, keyed by the name generate.py reports it
# under. A line or a space is appended; agent/opcodes.py is edited inside its marked block, since
# text after END is hand-written and not drift. gen_agent_api_urls.py writes nothing: it compares
# the ports in the compose file, so its row moves one port (gen_agents_compose.py fails as well).
STALE = {
    "scripts/gen_agents_compose.py": ("docker-compose.agents.yml", lambda t: t + " "),
    "scripts/gen_agent_api_urls.py": ("docker-compose.agents.yml",
                                      lambda t: t.replace("AGENT_HTTP_PORT: 9601\n", "AGENT_HTTP_PORT: 9699\n", 1)),
    "scripts/gen-live-map-dash.py": ("monitoring/grafana-dashboard-wow-live-map.json", lambda t: t + " "),
    "scripts/gen-wow-dashboard.py": ("monitoring/grafana-dashboard-wow-server-host.json", lambda t: t + " "),
    "scripts/gen-wow-game-dashboards.py": ("monitoring/grafana-dashboard-wow-players.json", lambda t: t + " "),
    "scripts/gen_protocol_tables.py": ("agent/opcodes.py", lambda t: t.replace(END, " \n" + END)),
    "-m agent.api_contract": ("docs/AGENT-API.md", lambda t: t + " "),
}


def copy_tree(tree: Path) -> Path:
    """#717: a throwaway copy of what the generators read and write, laid out like the repo.
    Every generator finds its files from its own path, so running the copy's generate.py
    keeps the checkout untouched (a stale agent/__pycache__ or a half-edit survives nothing)."""
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


class GenerateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.tree = copy_tree(Path(tmp.name))  # shared by the stale table; each row restores its file

    def test_committed_files_are_up_to_date(self):
        self.assertEqual(generate.main(["--check"]), 0)

    def test_every_generator_fails_check_when_its_output_is_stale(self):
        self.assertEqual(set(STALE), {" ".join(g) for g in generate.GENERATORS},
                         "every entry of GENERATORS needs one row in STALE")
        for name, (rel, edit) in STALE.items():
            path = self.tree / rel
            good = path.read_bytes()
            stale = edit(good.decode()).encode()
            self.assertNotEqual(stale, good, f"the stale edit for {name} changed nothing")
            path.write_bytes(stale)
            try:
                run = self._generate(self.tree, "--check")
            finally:
                path.write_bytes(good)
            with self.subTest(generator=name):
                failed = run.stderr.partition("Failed: ")[2].strip().split("; ")
                self.assertEqual(run.returncode, 1, run.stderr)
                self.assertIn(name, failed)

    def test_clean_check_runs_exactly_the_registered_generators(self):
        # generate.py prints nothing per generator on success, so this records the commands it runs.
        ran = []

        def fake_run(cmd, **kwargs):
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(generate.subprocess, "run", side_effect=fake_run):
            self.assertEqual(generate.main(["--check"]), 0)
        self.assertTrue(all(cmd[-1] == "--check" for cmd in ran), ran)
        self.assertEqual([cmd[1:-1] for cmd in ran], [list(g) for g in generate.GENERATORS])

    def test_drift_names_the_generator(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = genlib.sync("monitoring/grafana-dashboard-wow-players.json", "hand edit", True, "scripts/gen-x.py")
        self.assertEqual(rc, 1)
        self.assertIn("scripts/generate.py", err.getvalue())
        self.assertIn("scripts/gen-x.py", err.getvalue())

    def _tree(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return copy_tree(Path(tmp.name))

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
