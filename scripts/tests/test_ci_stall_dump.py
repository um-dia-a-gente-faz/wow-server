"""Guard (#707): the agent and coverage jobs dump all thread stacks before their time limit.

A stalled test must name itself in the log instead of ending as a silent cancel at
`timeout-minutes`. Each job sets PYTHONFAULTHANDLER=1 and CHECK_STALL_DUMP_S (read by
scripts/check.sh and scripts/coverage_floor.py, through scripts/early_failures.py: dump, soft stop,
SIGABRT, kill), and the figure stays at least MARGIN_S under the job's
limit so the dump and the summary reach the log first. Reads the workflow
as text, so the mutant tests feed a copy with the dump removed.
"""
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ci_suite_jobs import job_spans  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
JOBS = ("agent", "coverage")
sys.path.insert(0, str(ROOT / "scripts"))

import early_failures  # noqa: E402

# 44 s: the kill comes 24 s after the bound at the latest (SOFT_STOP_S + the 10 s wait for SIGABRT), leaving 20 s
# for job setup (measured 5 to 13 s) and the log: 420 + 24 + 7 < 480, 540 + 24 + 13 < 600.
MARGIN_S = early_failures.SOFT_STOP_S + 10 + 20   # SIGABRT, + the wait before the kill, + setup and log


def problems(ci_text):
    lines, spans = job_spans(ci_text)
    out = []
    for job in JOBS:
        first, last = spans[job]
        body = "\n".join(l for l in lines[first:last] if not l.strip().startswith("#"))
        limit = re.search(r"^    timeout-minutes: (\d+)$", body, re.M)
        secs = re.search(r'^\s+CHECK_STALL_DUMP_S: "(\d+)"$', body, re.M)
        if not re.search(r'^\s+PYTHONFAULTHANDLER: "1"$', body, re.M):
            out.append(f"job {job}: PYTHONFAULTHANDLER is not set to 1")
        if not secs:
            out.append(f"job {job}: CHECK_STALL_DUMP_S is not set")
        elif not limit:
            out.append(f"job {job}: no timeout-minutes")
        elif int(secs.group(1)) > int(limit.group(1)) * 60 - MARGIN_S:
            out.append(f"job {job}: CHECK_STALL_DUMP_S {secs.group(1)} is not {MARGIN_S} s under "
                       f"timeout-minutes {limit.group(1)}")
    return out


def without(ci_text, job, pattern):
    lines, spans = job_spans(ci_text)
    first, last = spans[job]
    keep = [l for i, l in enumerate(lines) if not (first <= i < last and re.search(pattern, l))]
    assert len(keep) < len(lines), pattern
    return "\n".join(keep) + "\n"


class CiStallDumpTest(unittest.TestCase):
    def test_both_jobs_keep_the_dump(self):
        self.assertEqual(problems(CI), [])

    def test_removing_the_fault_handler_from_either_job_is_reported(self):
        for job in JOBS:
            found = problems(without(CI, job, r"PYTHONFAULTHANDLER"))
            self.assertEqual(found, [f"job {job}: PYTHONFAULTHANDLER is not set to 1"])

    def test_removing_the_bound_from_either_job_is_reported(self):
        for job in JOBS:
            found = problems(without(CI, job, r"CHECK_STALL_DUMP_S"))
            self.assertEqual(found, [f"job {job}: CHECK_STALL_DUMP_S is not set"])

    def test_a_bound_too_close_to_the_job_limit_is_reported(self):
        # agent: 8 min limit; coverage: 10 min. The most is the limit less MARGIN_S.
        for old, limit in (("420", 8 * 60), ("540", 10 * 60)):
            most = limit - MARGIN_S
            over = CI.replace(f'CHECK_STALL_DUMP_S: "{old}"', f'CHECK_STALL_DUMP_S: "{most + 1}"')
            self.assertEqual(len(problems(over)), 1)
            self.assertEqual(problems(CI.replace(f'CHECK_STALL_DUMP_S: "{old}"', f'CHECK_STALL_DUMP_S: "{most}"')), [])

    def test_check_sh_and_coverage_floor_honour_the_bound(self):
        # The wiring: the env var is only a promise if the two entry points read it and hand it to the
        # one supervisor (scripts/early_failures.py).
        check = (ROOT / "scripts" / "check.sh").read_text()
        self.assertRegex(check, r'if \[ -n "\$\{CHECK_STALL_DUMP_S:-\}" \]; then\n\s+'
                                r'cmd="\$\{cmd/early_failures\.py/early_failures\.py --stall-after \$CHECK_STALL_DUMP_S --\}"')
        cf = (ROOT / "scripts" / "coverage_floor.py").read_text()
        self.assertIn("CHECK_STALL_DUMP_S", cf)
        self.assertIn("early_failures.stall_stop(p)", cf)

    def test_every_unittest_suite_goes_through_the_supervisor(self):
        check = (ROOT / "scripts" / "check.sh").read_text()
        self.assertNotIn("-m unittest", check)
        suites = re.findall(r'^\s+"[\w-]+\|(.*)"$', check, re.M)
        self.assertGreater(len(suites), 10)
        for cmd in suites:   # the one exception: node's own runner
            self.assertTrue(cmd.startswith("node --test ") or cmd.startswith("python3 scripts/early_failures.py discover "), cmd)


if __name__ == "__main__":
    unittest.main()
