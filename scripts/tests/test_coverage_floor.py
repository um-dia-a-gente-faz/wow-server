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


def mod(stmts, pct, missing=()):
    return (stmts, pct, list(missing))


class LineRangesTest(unittest.TestCase):
    def test_compressed(self):
        self.assertEqual(cf.line_ranges([12, 13, 14, 15, 16, 17, 18, 40, 77, 78, 79]), "12-18, 40, 77-79")
        self.assertEqual(cf.line_ranges([9, 3, 4]), "3-4, 9")
        self.assertEqual(cf.line_ranges([]), "")

    def test_capped(self):
        out = cf.line_ranges(list(range(1, 2000, 2)))
        self.assertLessEqual(len(out), cf.RANGES_CAP + 5)
        self.assertTrue(out.endswith(", ..."))
        self.assertTrue(out.startswith("1, 3, 5"))


class CheckModulesTest(unittest.TestCase):
    def test_exactly_at_floor_passes_just_below_fails(self):
        self.assertEqual(cf.check_modules({"agent/a.py": mod(100, 90.0)}, {"agent/a.py": 90}), [])
        out = cf.check_modules({"agent/a.py": mod(100, 89.99, [5, 6, 9])}, {"agent/a.py": 90})
        self.assertEqual(len(out), 1)
        for text in ("agent/a.py", "89.99%", "floor 90%", "5-6, 9"):
            self.assertIn(text, out[0])

    def test_improvement_never_fails(self):
        for pct in (90.01, 92.0, 95.0, 100.0):
            self.assertEqual(cf.check_modules({"agent/a.py": mod(100, pct)}, {"agent/a.py": 90}), [], pct)

    def test_fifty_statements_need_an_entry_forty_nine_do_not(self):
        self.assertEqual(cf.check_modules({"agent/a.py": mod(49, 80.0)}, {}), [])
        out = cf.check_modules({"agent/a.py": mod(50, 87.9)}, {})
        self.assertEqual(len(out), 1)
        self.assertIn('add "agent/a.py": 85 to "agent_modules"', out[0])

    def test_listed_module_that_is_gone_says_remove(self):
        out = cf.check_modules({}, {"agent/gone.py": 90})
        self.assertEqual(len(out), 1)
        self.assertIn("agent/gone.py", out[0])
        self.assertIn("remove", out[0])

    def test_small_listed_module_is_still_checked(self):
        self.assertEqual(len(cf.check_modules({"agent/a.py": mod(10, 50.0)}, {"agent/a.py": 90})), 1)

    def test_table_is_sorted_floored_and_only_50_plus(self):
        got = cf.modules_table({"agent/b.py": mod(50, 99.99), "agent/a.py": mod(60, 90.0), "agent/c.py": mod(49, 100.0)})
        self.assertEqual(list(got.items()), [("agent/a.py", 88), ("agent/b.py", 97)])


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
        for content in (None, "not json", "[]", "{}", '{"agent": 60}', '{"agent_modules": {}}',
                        '{"agent": "60", "agent_modules": {}}', '{"agent": 60.5, "agent_modules": {}}',
                        '{"agent": true, "agent_modules": {}}', '{"agent": -1, "agent_modules": {}}',
                        '{"agent": 101, "agent_modules": {}}', '{"agent": 60, "agent_modules": []}',
                        '{"agent": 60, "agent_modules": {"agent/a.py": "90"}}',
                        '{"agent": 60, "agent_modules": {"agent/a.py": 101}}'):
            if content and content.startswith('{"agent'):
                content = content.replace("{", '{"python": "3.12", ', 1)   # each case fails for its own key
            with self.subTest(content=content):
                code, err = self.run_main(content)
                self.assertEqual(code, 2)
                self.assertIn("coverage-floors.json", err)

    def test_python_key_is_required_and_major_minor(self):
        ok = '"agent": 60, "agent_modules": {}'
        for content in ("{" + ok + "}", "{" + ok + ', "python": 3.12}', "{" + ok + ', "python": "3"}',
                        "{" + ok + ', "python": "3.12.1"}', "{" + ok + ', "python": "x.y"}', "{" + ok + ', "python": ""}'):
            with self.subTest(content=content):
                code, err = self.run_main(content)
                self.assertEqual(code, 2)
                self.assertIn('"python"', err)

    def test_committed_file_is_valid(self):
        data = json.loads((ROOT / "coverage-floors.json").read_text())
        self.assertEqual(list(data), ["python", "agent", "agent_modules"])
        self.assertEqual(data["python"], "3.12")
        self.assertEqual(cf.load_floors(ROOT / "coverage-floors.json"), (data["agent"], data["agent_modules"], "3.12"))
        self.assertEqual(list(data["agent_modules"]), sorted(data["agent_modules"]))
        for path in data["agent_modules"]:
            self.assertTrue((ROOT / path).is_file(), path)


class MainTest(unittest.TestCase):
    """main() with measure() stubbed: the exit code and text of each outcome."""

    FILES = {"agent/a.py": mod(100, 90.0, [3]), "agent/small.py": mod(10, 10.0)}

    def run_main(self, measured, argv=(), spec=True, files=None, python="3.12"):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "floors.json"
            p.write_text('{"python": "3.12", "agent": 60, "agent_modules": {"agent/a.py": 88}}')
            result = (measured, self.FILES if files is None else files)
            with mock.patch.object(cf, "FLOORS", p), mock.patch.object(cf, "measure", return_value=result), \
                    mock.patch.object(cf, "running_python", return_value=python), \
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
        code, _, _ = self.run_main(10.0, ["--report-only"], files={"agent/a.py": mod(100, 10.0), "agent/new.py": mod(60, 90.0)})
        self.assertEqual(code, 0)

    def test_module_below_its_floor_exits_1_with_its_lines(self):
        code, out, _ = self.run_main(61.0, files={"agent/a.py": mod(100, 80.0, [7, 8])})
        self.assertEqual(code, 1)
        self.assertIn("agent/a.py coverage 80.00% is below its floor 88%", out)
        self.assertIn("7-8", out)

    def test_module_better_than_its_floor_exits_0(self):
        self.assertEqual(self.run_main(61.0, files={"agent/a.py": mod(100, 100.0)})[0], 0)

    def test_total_and_module_failures_add_up(self):
        code, out, _ = self.run_main(50.0, files={"agent/a.py": mod(100, 80.0), "agent/new.py": mod(60, 90.0)})
        self.assertEqual(code, 1)
        self.assertIn("agent coverage 50.00% is below the floor", out)
        self.assertIn("agent/a.py coverage 80.00% is below its floor", out)
        self.assertIn("agent/new.py has 60 statements and no floor", out)

    def test_log_carries_the_per_module_numbers(self):
        _, out, _ = self.run_main(61.0)
        self.assertIn("module agent/a.py stmts=100 cover=90.00 floor=88", out)
        self.assertNotIn("small.py", out)

    def test_print_modules_is_only_the_json_on_stdout(self):
        files = {"agent/b.py": mod(60, 93.5), "agent/a.py": mod(100, 90.0), "agent/small.py": mod(10, 10.0)}
        code, out, _ = self.run_main(61.0, ["--print-modules"], files=files)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"agent/a.py": 88, "agent/b.py": 91})
        self.assertEqual(list(json.loads(out)), ["agent/a.py", "agent/b.py"])

    def test_print_modules_keeps_the_measure_table_off_stdout(self):
        def noisy():
            print("TABLE")
            return 61.0, {"agent/a.py": mod(100, 90.0)}

        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "floors.json"
            p.write_text('{"python": "3.12", "agent": 60, "agent_modules": {}}')
            with mock.patch.object(cf, "FLOORS", p), mock.patch.object(cf, "measure", noisy), mock.patch.object(cf, "running_python", return_value="3.12"), \
                    mock.patch.object(cf.importlib.util, "find_spec", return_value=True), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                cf.main(["--print-modules"])
        self.assertEqual(json.loads(out.getvalue()), {"agent/a.py": 88})
        self.assertIn("TABLE", err.getvalue())

    def test_other_interpreter_prints_one_notice_and_still_gates(self):
        code, out, err = self.run_main(50.0, python="3.14")
        self.assertEqual(code, 1)
        self.assertIn("is below the floor", out)
        self.assertEqual(err.count("Python 3.12"), 1)
        self.assertIn("this is 3.14", err)
        self.assertIn("differ by interpreter", err)
        self.assertNotIn("Python 3.12", out)

    def test_matching_interpreter_prints_no_notice(self):
        _, out, err = self.run_main(61.0)
        self.assertEqual(err, "")
        self.assertNotIn("interpreter", out)

    def test_print_modules_on_another_interpreter_warns_on_stderr_and_keeps_stdout_json(self):
        code, out, err = self.run_main(61.0, ["--print-modules"], python="3.14")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"agent/a.py": 88})
        self.assertIn("do NOT paste", err)
        self.assertIn("coverage-floors.json", err)
        self.assertEqual(err.count("Python 3.12"), 1)

    def test_print_modules_on_the_matching_interpreter_does_not_warn(self):
        self.assertNotIn("do NOT paste", self.run_main(61.0, ["--print-modules"])[2])

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

    def test_run_past_the_deadline_gets_the_stall_sequence_then_exits_1(self):
        # #714: early_failures.stall_stop (dump, dump, SIGINT, SIGABRT, kill; tested in test_early_failures)
        proc = mock.Mock(returncode=-6)
        proc.communicate.side_effect = subprocess.TimeoutExpired("x", 1)
        with mock.patch.object(cf.subprocess, "Popen", return_value=proc), \
                mock.patch.object(cf.early_failures, "stall_stop") as stop, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cf._run(["run"], {}, deadline=time.monotonic() - 1)
        self.assertEqual(cm.exception.code, 1)
        stop.assert_called_once_with(proc)
        proc.send_signal.assert_not_called()   # the sequence is stall_stop's, not repeated here

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

    def test_measure_bounds_both_runs_by_one_deadline_from_the_env(self):
        deadlines = []

        def fake_run(cmd, env, **kw):
            deadlines.append(kw.get("deadline"))
            if cmd[0] == "json":
                pathlib.Path(cmd[cmd.index("-o") + 1]).write_text('{"files": {}}')
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
            if "json" in cmd[:4]:
                pathlib.Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps({"files": {"agent/x.py": {
                    "summary": {"num_statements": 60, "percent_covered": 91.5}, "missing_lines": [4, 5]}}}))
            return mock.Mock(returncode=0, **{"communicate.return_value": ("97.5\n", None)})

        with mock.patch.object(cf.subprocess, "Popen", fake), contextlib.redirect_stdout(io.StringIO()):
            total, files = cf.measure()
        self.assertEqual(total, 97.5)
        self.assertEqual(files, {"agent/x.py": (60, 91.5, [4, 5])})
        # the JSON report is written next to the data file, in the temporary directory
        json_cmd = next(c for c, _ in calls if "json" in c[:4])
        self.assertEqual(len({data for _, data in calls}), 1)
        self.assertEqual(pathlib.Path(json_cmd[json_cmd.index("-o") + 1]).parent, pathlib.Path(calls[0][1]).parent)
        runs = [c for c, _ in calls if "run" in c[:4]]
        self.assertEqual(len(runs), 2)
        for cmd in runs:
            self.assertIn("--omit=agent/tests/*", cmd)
            self.assertIn("--source=agent", cmd)
            self.assertIn("scripts/early_failures.py", cmd)   # #714: failures print as they happen, stall stop
            self.assertNotIn("unittest", cmd)
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
