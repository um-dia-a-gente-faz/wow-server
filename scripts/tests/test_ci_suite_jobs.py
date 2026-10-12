"""Guard (#624): each suite in scripts/check.sh SUITES runs in exactly one CI job.

Reads scripts/check.sh and .github/workflows/ci.yml as text. The suite-array parsing is
reused from test_check_registry (the guard that every tests dir is registered, #298).
The checks take text, so the tests can feed a reverted workflow without touching the repo.
"""
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_check_registry import _suite_lines  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = (ROOT / "scripts" / "check.sh").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

JOB_HEAD = re.compile(r"^  ([a-z][a-z0-9-]*):\s*$")
CHECK_TARGETS = re.compile(r"scripts/check\.sh((?:[ \t]+[A-Za-z0-9_*-]+)*)")
# Targets that run no suite (dispatch arms in scripts/check.sh that call a function).
NO_SUITE_TARGETS = {"compile", "lint", "types", "types-legacy", "generated", "list", "coverage"}


def suite_names(check_text):
    lines = check_text.splitlines()
    return [lines[i].strip().strip('"').split("|", 1)[0] for i in _suite_lines(check_text)]


def stdlib_suites(check_text):
    m = re.search(r'^STDLIB_SUITES="([^"]*)"', check_text, re.M)
    if not m:
        raise ValueError("STDLIB_SUITES=\"...\" not found in scripts/check.sh")
    return set(m.group(1).split())


def expand(target, suites, stdlib):
    """Suites that `scripts/check.sh <target>` runs, following the dispatch in check.sh."""
    if target in ("test", "all"):
        return set(suites)
    if target == "test-stdlib":
        return set(stdlib)
    if target == "test-tools":
        return set(suites) - set(stdlib)
    if target.startswith("test-") and target[len("test-"):] in suites:
        return {target[len("test-"):]}
    if target in NO_SUITE_TARGETS:
        return set()
    raise ValueError(f"unknown scripts/check.sh target {target!r}")


def job_spans(ci_text):
    lines = ci_text.splitlines()
    start = lines.index("jobs:")
    heads = [(i, m.group(1)) for i in range(start + 1, len(lines)) if (m := JOB_HEAD.match(lines[i]))]
    spans = {}
    for k, (i, name) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        spans[name] = (i + 1, end)
    return lines, spans


def job_if(ci_text, job):
    lines, spans = job_spans(ci_text)
    first, last = spans[job]
    found = [m.group(1).strip() for line in lines[first:last] if (m := re.match(r"^    if: (.+)$", line))]
    return found[0] if found else None


def job_targets(ci_text, job):
    lines, spans = job_spans(ci_text)
    first, last = spans[job]
    targets = []
    for line in lines[first:last]:
        if line.strip().startswith("#"):
            continue
        m = CHECK_TARGETS.search(line)
        if m:
            targets.extend(m.group(1).split())
    return targets


def with_job_if(ci_text, job, expr):
    """The job-level `if:` of `job` replaced by `expr` (the job must have one)."""
    lines, spans = job_spans(ci_text)
    first, last = spans[job]
    hits = [i for i in range(first, last) if re.match(r"^    if: .+$", lines[i])]
    if len(hits) != 1:
        raise AssertionError(f"expected one job-level if in job {job}, found {len(hits)}")
    lines[hits[0]] = f"    if: {expr}"
    return "\n".join(lines) + "\n"


def with_tools_check(ci_text, targets):
    """The tools job's check step, re-pointed at `scripts/check.sh generated <targets>`."""
    lines, spans = job_spans(ci_text)
    first, last = spans["tools"]
    hits = [i for i in range(first, last) if lines[i].startswith("      - run: scripts/check.sh generated ")]
    if len(hits) != 1:
        raise AssertionError(f"expected one generated check step in job tools, found {len(hits)}")
    lines[hits[0]] = f"      - run: scripts/check.sh generated {targets}"
    return "\n".join(lines) + "\n"


def arm_problems(check_text):
    """test-tools) must be an arm of its own, placed before the test-*) catch-all."""
    tools_arm = re.search(r"^\s*test-tools\)", check_text, re.M)
    glob_arm = re.search(r"^\s*test-\*\)", check_text, re.M)
    if not tools_arm:
        return ["scripts/check.sh has no test-tools) dispatch arm"]
    if glob_arm and tools_arm.start() > glob_arm.start():
        return ["the test-tools) arm comes after test-*), so test-tools runs a suite named 'tools'"]
    return []


def problems(ci_text, check_text):
    """Every way the CI split breaks the one-job-per-suite rule. Empty when it holds."""
    out = arm_problems(check_text)
    suites = suite_names(check_text)
    stdlib = stdlib_suites(check_text)

    try:
        t_tools = expand("test-tools", suites, stdlib)
        t_stdlib = expand("test-stdlib", suites, stdlib)
    except ValueError as e:
        return out + [str(e)]
    if (t_tools | t_stdlib) != set(suites) or (t_tools & t_stdlib):
        out.append("test-tools and test-stdlib do not cover SUITES, each suite exactly once")

    job_suites = {}
    try:
        for job in ("agent", "tools"):
            if job not in job_spans(ci_text)[1]:
                out.append(f"ci.yml has no job {job!r}")
                continue
            job_suites[job] = set()
            for target in job_targets(ci_text, job):
                job_suites[job] |= expand(target, suites, stdlib)
    except ValueError as e:
        out.append(f"ci.yml: {e}")
    for s in suites:
        running = [j for j in ("agent", "tools") if s in job_suites.get(j, set())]
        if len(running) == 2:
            out.append(f"the {s} suite is run by both jobs (agent, tools); exactly one job must run it")
        elif len(running) == 0:
            out.append(f"the {s} suite is run by no job")
    for s in stdlib:
        if s not in job_suites.get("agent", set()):
            out.append(f"the agent job does not run the stdlib suite {s}")

    # The agent job must run whenever the tools job runs, so the suites tools skips still run.
    tools_if, agent_if = job_if(ci_text, "tools"), job_if(ci_text, "agent")
    if tools_if is None:
        if agent_if is not None:
            out.append("the tools job always runs but the agent job is gated")
    elif agent_if is not None and tools_if not in [t.strip() for t in agent_if.split("||")]:
        out.append(f"the agent job's if ({agent_if}) is not true whenever the tools job runs ({tools_if})")
    return out


class CiSuiteJobsTest(unittest.TestCase):
    def test_each_suite_runs_in_exactly_one_job(self):
        self.assertEqual(problems(CI, CHECK), [])

    def test_agent_if_reverted_while_tools_runs_test_tools_is_reported(self):
        # The gap #624 would open: the tools job skips the stdlib suite, the agent job is gated
        # on its own path filter only, so a tools-only PR runs the stdlib suite nowhere.
        ci = with_job_if(CI, "agent", "needs.changes.outputs.agent == 'true'")
        ci = with_tools_check(ci, "test-tools")
        found = problems(ci, CHECK)
        self.assertTrue(found, "a reverted agent if must be reported")
        self.assertTrue(any("agent job's if" in p for p in found), found)

    def test_tools_running_test_all_is_reported(self):
        found = problems(with_tools_check(CI, "test"), CHECK)
        self.assertTrue(any("run by both jobs" in p for p in found), found)

    def test_test_tools_arm_must_precede_glob_arm(self):
        glob_line = '    test-*) suite "${t#test-}" ;;'
        tools_line = '    test-tools) suite "tools" ;;'
        check = f"case x in\n{glob_line}\n{tools_line}\nesac\n"
        self.assertTrue(arm_problems(check))
        self.assertEqual(arm_problems(f"case x in\n{tools_line}\n{glob_line}\nesac\n"), [])


if __name__ == "__main__":
    unittest.main()
