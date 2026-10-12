"""Guard (#707): the agent and coverage jobs dump all thread stacks before their time limit.

A stalled test must name itself in the log instead of ending as a silent cancel at
`timeout-minutes`. Each job sets PYTHONFAULTHANDLER=1 and CHECK_STALL_DUMP_S (read by
scripts/check.sh and scripts/coverage_floor.py, which send SIGABRT), and the figure stays
at least MARGIN_S under the job's limit so the dump reaches the log first. Reads the workflow
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
MARGIN_S = 30


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
        ci = CI.replace('CHECK_STALL_DUMP_S: "240"', 'CHECK_STALL_DUMP_S: "271"')
        self.assertEqual(len(problems(ci)), 1)
        self.assertEqual(problems(CI.replace('CHECK_STALL_DUMP_S: "240"', 'CHECK_STALL_DUMP_S: "270"')), [])

    def test_check_sh_and_coverage_floor_honour_the_bound(self):
        # The wiring: the env var is only a promise if the two entry points read it.
        check = (ROOT / "scripts" / "check.sh").read_text()
        self.assertRegex(check, r"timeout --signal=ABRT -k \d+ \"\$CHECK_STALL_DUMP_S\" bash -c \"exec ")
        self.assertIn("CHECK_STALL_DUMP_S", (ROOT / "scripts" / "coverage_floor.py").read_text())


if __name__ == "__main__":
    unittest.main()
