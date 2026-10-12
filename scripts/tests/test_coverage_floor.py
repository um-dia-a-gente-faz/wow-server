"""Guard (#384): the agent coverage floor. check() is pure; the config errors exit 2.

A real measurement runs the whole agent suite under tracing, too slow for this suite: the
CI job (and `scripts/check.sh coverage`) does that. Here the workflow and check.sh are read
as text to prove both go through scripts/coverage_floor.py and read coverage-floors.json.
"""
import contextlib
import io
import json
import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import coverage_floor as cf  # noqa: E402

CHECK = (ROOT / "scripts" / "check.sh").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()


def job_text(name):
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", CI, re.M | re.S)
    assert m, name
    return m.group(1)


class CheckTest(unittest.TestCase):
    def test_below_floor(self):
        out = cf.check(59.99, 60)
        self.assertEqual(len(out), 1)
        self.assertIn("59.99% is below the floor 60%", out[0])

    def test_at_floor_and_inside_margin(self):
        self.assertEqual(cf.check(60.0, 60), [])
        self.assertEqual(cf.check(61.49, 60), [])

    def test_stale_boundary(self):
        out = cf.check(61.5, 60)
        self.assertEqual(len(out), 1)
        self.assertIn('raise "agent" in coverage-floors.json to 61', out[0])
        self.assertIn("1.50 points above", out[0])

    def test_far_above_names_int_total(self):
        self.assertIn("to 66", cf.check(66.99, 60)[0])


class ConfigTest(unittest.TestCase):
    def run_main(self, content):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "floors.json"
            if content is not None:
                p.write_text(content)
            old, cf.FLOORS = cf.FLOORS, p
            try:
                with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as cm:
                    cf.main([])
            finally:
                cf.FLOORS = old
        return cm.exception.code, err.getvalue()

    def test_bad_configs_exit_2(self):
        for content in (None, "not json", "[]", "{}", '{"agent": "60"}', '{"agent": 60.5}',
                        '{"agent": true}', '{"agent": -1}', '{"agent": 101}'):
            with self.subTest(content=content):
                code, err = self.run_main(content)
                self.assertEqual(code, 2)
                self.assertIn("coverage-floors.json", err)

    def test_committed_file_is_valid(self):
        data = json.loads((ROOT / "coverage-floors.json").read_text())
        self.assertEqual(list(data), ["agent"])
        self.assertEqual(cf.load_floor(ROOT / "coverage-floors.json"), data["agent"])


class WiringTest(unittest.TestCase):
    def test_check_sh_runs_script_outside_all(self):
        self.assertRegex(CHECK, r"(?m)^\s*coverage\)\s*python3 scripts/coverage_floor\.py")
        self.assertNotIn("coverage_floor", CHECK.split("all)", 1)[1].splitlines()[0])
        self.assertNotIn("coverage", "".join(l for l in CHECK.splitlines() if "SUITES" in l or l.lstrip().startswith('"')))

    def test_ci_job_is_a_gate_through_check_sh(self):
        job = job_text("coverage")
        self.assertIn("name: coverage (agent floor)", job)
        self.assertNotIn("continue-on-error", job)
        self.assertIn("pip install -r requirements-dev.txt", job)
        self.assertIn("scripts/check.sh coverage", job)
        self.assertIn("pipefail", job)
        self.assertIn("timeout-minutes: 10", job)

    def test_ci_ok_needs_coverage(self):
        m = re.search(r"ci-ok:.*?needs: \[([^\]]*)\]", CI, re.S)
        self.assertIn("coverage", [n.strip() for n in m.group(1).split(",")])

    def test_both_read_the_one_floor_file(self):
        self.assertIn("coverage-floors.json", (ROOT / "scripts" / "coverage_floor.py").read_text())
        # CI reaches it only through the script (check.sh coverage); a change to it reruns the job
        self.assertIn(r"coverage-floors\.json", CI)

    def test_script_does_not_import_coverage(self):
        src = (ROOT / "scripts" / "coverage_floor.py").read_text()
        self.assertNotRegex(src, r"(?m)^\s*(import|from)\s+coverage\b")


if __name__ == "__main__":
    unittest.main()
