#!/usr/bin/env python3
"""Ratcheting coverage gate for agent/ (#384).

Runs the agent and world_mock suites under `coverage`, compares the total with the floor in
coverage-floors.json and fails when the total is below it (tests lost) or at least
STALE_MARGIN above it (floor not raised). Same idiom as mypy-legacy-baseline.json.

  scripts/check.sh coverage                         # the gate, what CI runs
  python3 scripts/coverage_floor.py --report-only   # table and result, always exit 0
  python3 scripts/coverage_floor.py --print-modules # "agent_modules" for coverage-floors.json, as JSON

stdlib only: it shells out to `python -m coverage` and never imports it.
"""
import contextlib
import importlib.util
import json
import re
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import early_failures   # scripts/ is sys.path[0] when this runs as a script

ROOT = Path(__file__).resolve().parents[1]
FLOORS = ROOT / "coverage-floors.json"
# The floor is int(total - 0.5): at least half a point of room below the measured total.
# Timing-dependent branches in the world_mock tests and the Python version move the total by a
# fraction of a point; a floor that lags by less than this is not stale.
STALE_MARGIN = 2.0
# Per-module floors (#700): files with fewer statements move by whole points per line, which is noise.
MODULE_MIN_STMTS = 50
MODULE_ROOM = 2      # a module's floor is int(percent - MODULE_ROOM); better never fails, only worse does
RANGES_CAP = 100     # characters of missing line ranges in one message


def check(measured: float, floor: int) -> list[str]:
    if measured < floor:
        return [f"agent coverage {measured:.2f}% is below the floor {floor}%: add tests or restore the ones removed"]
    if measured >= floor + STALE_MARGIN:
        return [f'agent coverage {measured:.2f}% is {measured - floor:.2f} points above the floor {floor}%: '
                f'raise "agent" in coverage-floors.json to {int(measured - 0.5)}']
    return []


def line_ranges(lines: list[int]) -> str:
    """[12, 13, 14, 40] -> '12-14, 40', cut at RANGES_CAP characters."""
    parts: list[str] = []
    nums = sorted(lines)
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    out = ", ".join(parts)
    if len(out) > RANGES_CAP:
        out = out[:RANGES_CAP].rsplit(", ", 1)[0] + ", ..."
    return out


def module_floor(percent: float) -> int:
    return int(percent - MODULE_ROOM)


def check_modules(measured: dict[str, tuple[int, float, list[int]]], floors: dict[str, int]) -> list[str]:
    """measured[path] = (statements, percent, missing lines). Lower bound only: no stale failure."""
    problems = []
    for path in sorted(measured):
        stmts, pct, missing = measured[path]
        if path in floors:
            if pct < floors[path]:
                problems.append(f"{path} coverage {pct:.2f}% is below its floor {floors[path]}%: "
                                f"missing lines {line_ranges(missing)}; add tests or restore the ones removed")
        elif stmts >= MODULE_MIN_STMTS:
            problems.append(f'{path} has {stmts} statements and no floor: add "{path}": {module_floor(pct)} '
                            f'to "agent_modules" in coverage-floors.json (scripts/coverage_floor.py --print-modules)')
    for path in sorted(floors):
        if path not in measured:
            problems.append(f'{path} is in "agent_modules" but no longer exists: remove that line from coverage-floors.json')
    return problems


def modules_table(measured: dict[str, tuple[int, float, list[int]]]) -> dict[str, int]:
    return {p: module_floor(m[1]) for p, m in sorted(measured.items()) if m[0] >= MODULE_MIN_STMTS}


def running_python() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def load_floors(path: Path) -> tuple[int, dict[str, int], str]:
    def bad(why: str) -> None:
        print(f"coverage-floors.json: {why}", file=sys.stderr)
        sys.exit(2)

    data: object = None
    try:
        data = json.loads(path.read_text())
    except OSError as e:
        bad(f"cannot read {path}: {e}")
    except ValueError as e:
        bad(f"not valid JSON: {e}")
    if not isinstance(data, dict) or "agent" not in data or "agent_modules" not in data:
        bad('expected {"agent": <int>, "agent_modules": {<path>: <int>}}')
        raise AssertionError  # unreachable: bad() exits
    floor = data["agent"]
    if type(floor) is not int or not 0 <= floor <= 100:
        bad('"agent" must be an integer from 0 to 100')
    mods = data["agent_modules"]
    if not isinstance(mods, dict) or any(
            not isinstance(k, str) or type(v) is not int or not 0 <= v <= 100 for k, v in mods.items()):
        bad('"agent_modules" must map file paths to integers from 0 to 100')
    py = data.get("python")
    if not isinstance(py, str) or not re.fullmatch(r"\d+\.\d+", py):
        bad('"python" must be a "major.minor" string, the interpreter the numbers were measured on')
    return floor, mods, py


def _run(cmd: list[str], env: dict[str, str], *, capture: bool = False, deadline: float | None = None) -> str:
    # Test output goes to stderr so stdout stays the table and the result (CI tees it).
    p = subprocess.Popen([sys.executable, "-m", "coverage", *cmd], cwd=ROOT, env=env, text=True,
                         stdout=subprocess.PIPE if capture else sys.stderr)
    try:
        out, _ = p.communicate(timeout=None if deadline is None else max(deadline - time.monotonic(), 1))
    except subprocess.TimeoutExpired:
        # #714: dump, soft stop (unittest's summary), SIGABRT, kill: early_failures.stall_stop
        print(f"\ncoverage {cmd[0]} ran past CHECK_STALL_DUMP_S", file=sys.stderr, flush=True)
        early_failures.stall_stop(p)
        sys.exit(1)
    except BaseException:
        p.kill()   # what subprocess.run did: Ctrl-C must not leave the test run alive
        p.wait()
        raise
    if p.returncode != 0:
        print(f"coverage {cmd[0]} failed (exit {p.returncode})", file=sys.stderr)
        sys.exit(1)
    return out or ""


def measure() -> tuple[float, dict[str, tuple[int, float, list[int]]]]:
    """Total and per-file (statements, percent, missing lines) of agent/ over the one measurement; prints the table."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "COVERAGE_FILE": str(Path(tmp) / "data")}
        # Production code only: the test files are most of the statements and run by definition.
        src = ["--source=agent", "--omit=agent/tests/*",
               "scripts/early_failures.py", "discover", "-s"]
        # #707: CHECK_STALL_DUMP_S (CI) bounds both test runs together.
        limit = os.environ.get("CHECK_STALL_DUMP_S")
        deadline = time.monotonic() + float(limit) if limit else None
        _run(["run", *src, "agent/tests"], env, deadline=deadline)
        _run(["run", "-a", *src, "tools/world_mock/tests"], env, deadline=deadline)
        print(_run(["report", "--skip-empty", "--sort=cover"], env, capture=True), end="")
        total = float(_run(["report", "--format=total", "--precision=2"], env, capture=True))
        report = Path(tmp) / "report.json"
        _run(["json", "-o", str(report)], env)
        files = json.loads(report.read_text())["files"]
        return total, {f: (v["summary"]["num_statements"], v["summary"]["percent_covered"], v["missing_lines"])
                       for f, v in files.items()}


def main(argv: list[str]) -> int:
    floor, mod_floors, py = load_floors(FLOORS)
    if importlib.util.find_spec("coverage") is None:
        print("coverage is not installed: pip install --require-hashes -r requirements-dev.txt", file=sys.stderr)
        return 2
    if running_python() != py:
        note = (f"coverage-floors.json: the floors were measured on Python {py}, this is {running_python()}; "
                f"per-module numbers differ by interpreter")
        if "--print-modules" in argv:
            note += ": do NOT paste this output into coverage-floors.json, take the numbers from the CI job's log"
        print(note, file=sys.stderr)
    if "--print-modules" in argv:
        with contextlib.redirect_stdout(sys.stderr):   # stdout is the JSON and nothing else
            _, files = measure()
        print(json.dumps(modules_table(files), indent=2))
        return 0
    measured, files = measure()
    # Compact, so the CI log carries the numbers the table is made from.
    for path, (stmts, pct, _) in sorted(files.items()):
        if stmts >= MODULE_MIN_STMTS:
            print(f"module {path} stmts={stmts} cover={pct:.2f} floor={mod_floors.get(path)}")
    print(f"agent coverage {measured:.2f}% (floor {floor}%)")
    if "--report-only" in argv:
        return 0
    problems = check(measured, floor) + check_modules(files, mod_floors)
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
