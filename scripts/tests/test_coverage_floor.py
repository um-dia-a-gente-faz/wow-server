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
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

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
        self.assertEqual(cf.check(60 + cf.STALE_MARGIN - 0.01, 60), [])

    def test_stale_boundary(self):
        out = cf.check(60 + cf.STALE_MARGIN, 60)
        self.assertEqual(len(out), 1)
        self.assertIn('raise "agent" in coverage-floors.json to 61', out[0])
        self.assertIn(f"{cf.STALE_MARGIN:.2f} points above", out[0])

    def test_literal_band_for_floor_96(self):
        # the committed numbers: stale from 98.0, so 0.7 up and 1.3 down from CI's 97.30
        self.assertEqual(cf.STALE_MARGIN, 2.0)
        self.assertEqual(cf.check(97.99, 96), [])
        self.assertIn("to 97", cf.check(98.00, 96)[0])
        self.assertIn("below the floor 96%", cf.check(95.99, 96)[0])

    def test_raise_message_keeps_half_a_point_of_room(self):
        self.assertIn("to 61", cf.check(62.0, 60)[0])      # int(62.0 - 0.5), not 62
        self.assertIn("to 66", cf.check(66.99, 60)[0])
        self.assertIn("to 98", cf.check(99.0, 97)[0])


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


class MainTest(unittest.TestCase):
    """main() with measure() stubbed: the exit code and text of each outcome."""

    def run_main(self, measured, argv=(), spec=True):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "floors.json"
            p.write_text('{"agent": 60}')
            with mock.patch.object(cf, "FLOORS", p), mock.patch.object(cf, "measure", return_value=measured), \
                    mock.patch.object(cf.importlib.util, "find_spec", return_value=spec), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                code = cf.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_below_floor_exits_1(self):
        code, out, _ = self.run_main(59.0)
        self.assertEqual(code, 1)
        self.assertIn("is below the floor 60%", out)
        self.assertIn("agent coverage 59.00% (floor 60%)", out)

    def test_stale_exits_1(self):
        code, out, _ = self.run_main(60 + cf.STALE_MARGIN)
        self.assertEqual(code, 1)
        self.assertIn('raise "agent"', out)

    def test_inside_band_exits_0(self):
        for m in (60.0, 60 + cf.STALE_MARGIN - 0.01):
            self.assertEqual(self.run_main(m)[0], 0)

    def test_report_only_never_fails(self):
        for m in (10.0, 99.0):
            code, out, _ = self.run_main(m, ["--report-only"])
            self.assertEqual(code, 0)
            self.assertIn(f"agent coverage {m:.2f}%", out)

    def test_not_installed_exits_2(self):
        code, _, err = self.run_main(60.0, spec=None)
        self.assertEqual(code, 2)
        self.assertIn("coverage is not installed", err)


class RunTest(unittest.TestCase):
    def test_failed_command_exits_1(self):
        proc = mock.Mock(returncode=1, **{"communicate.return_value": ("", None)})
        with mock.patch.object(cf.subprocess, "Popen", return_value=proc), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cf._run(["run"], {})
        self.assertEqual(cm.exception.code, 1)

    def test_run_past_the_deadline_gets_sigabrt_then_exits_1(self):
        # #707: with PYTHONFAULTHANDLER=1 the SIGABRT is what dumps every thread's stack.
        proc = mock.Mock(returncode=-6)
        proc.communicate.side_effect = [subprocess.TimeoutExpired("x", 1), ("", None)]
        with mock.patch.object(cf.subprocess, "Popen", return_value=proc), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cf._run(["run"], {}, deadline=time.monotonic() - 1)
        self.assertEqual(cm.exception.code, 1)
        proc.send_signal.assert_called_once_with(signal.SIGABRT)
        proc.kill.assert_not_called()

    def test_communicate_gets_the_time_left_until_the_deadline_or_none(self):
        for deadline, want in ((time.monotonic() + 50, 50), (None, None)):
            proc = mock.Mock(returncode=0, **{"communicate.return_value": ("", None)})
            with mock.patch.object(cf.subprocess, "Popen", return_value=proc):
                cf._run(["run"], {}, deadline=deadline)
            got = proc.communicate.call_args_list[0].kwargs["timeout"]
            if want is None:
                self.assertIsNone(got)
            else:
                self.assertAlmostEqual(got, want, delta=5)

    def test_interrupt_kills_the_child_and_propagates(self):
        proc = mock.Mock()
        proc.communicate.side_effect = KeyboardInterrupt
        with mock.patch.object(cf.subprocess, "Popen", return_value=proc), self.assertRaises(KeyboardInterrupt):
            cf._run(["run"], {})
        proc.kill.assert_called_once_with()

    def test_run_that_ignores_sigabrt_is_killed(self):
        proc = mock.Mock(returncode=-9)
        proc.communicate.side_effect = [subprocess.TimeoutExpired("x", 1), subprocess.TimeoutExpired("x", 10), ("", None)]
        with mock.patch.object(cf.subprocess, "Popen", return_value=proc), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cf._run(["run"], {}, deadline=time.monotonic() - 1)
        proc.kill.assert_called_once_with()

    def test_measure_bounds_both_runs_by_one_deadline_from_the_env(self):
        deadlines = []

        def fake_run(cmd, env, **kw):
            deadlines.append(kw.get("deadline"))
            return "1\n"

        with mock.patch.dict(cf.os.environ, {"CHECK_STALL_DUMP_S": "100"}), \
                mock.patch.object(cf, "_run", fake_run), contextlib.redirect_stdout(io.StringIO()):
            cf.measure()
        self.assertEqual(deadlines[0], deadlines[1])
        self.assertAlmostEqual(deadlines[0] - time.monotonic(), 100, delta=5)

    def test_both_runs_omit_the_tests_and_keep_data_out_of_the_checkout(self):
        calls = []

        def fake(cmd, **kw):
            calls.append((cmd, kw["env"]["COVERAGE_FILE"]))
            return mock.Mock(returncode=0, **{"communicate.return_value": ("97.5\n", None)})

        with mock.patch.object(cf.subprocess, "Popen", fake), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cf.measure(), 97.5)
        runs = [c for c, _ in calls if "run" in c[:4]]
        self.assertEqual(len(runs), 2)
        for cmd in runs:
            self.assertIn("--omit=agent/tests/*", cmd)
            self.assertIn("--source=agent", cmd)
        self.assertIn("tools/world-mock/tests", runs[1])
        self.assertIn("-a", runs[1])
        for _, data in calls:
            self.assertNotIn(str(ROOT), data)


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
        self.assertRegex(job, r"(?m)^\s*exit \$rc$")   # the script's exit code must reach the step
        self.assertIn("timeout-minutes: 10", job)

    def test_path_filter_runs_the_job_for_its_inputs(self):
        line = next(l for l in CI.splitlines() if 'echo "agent=' in l)
        for path in (r"^agent/", r"^tools/world-mock/", r"^coverage-floors\.json$"):
            self.assertIn(path, line)

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
