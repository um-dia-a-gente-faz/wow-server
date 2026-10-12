#!/usr/bin/env python3
"""Ratcheting coverage gate for agent/ (#384).

Runs the agent and world-mock suites under `coverage`, compares the total with the floor in
coverage-floors.json and fails when the total is below it (tests lost) or at least
STALE_MARGIN above it (floor not raised). Same idiom as mypy-legacy-baseline.json.

  scripts/check.sh coverage                         # the gate, what CI runs
  python3 scripts/coverage_floor.py --report-only   # table and result, always exit 0

stdlib only: it shells out to `python -m coverage` and never imports it.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLOORS = ROOT / "coverage-floors.json"
# The floor is int(total - 0.5): at least half a point of room below the measured total.
# Timing-dependent branches in the world-mock tests and the Python version move the total by a
# fraction of a point; a floor that lags by less than this is not stale.
STALE_MARGIN = 1.5


def check(measured: float, floor: int) -> list[str]:
    if measured < floor:
        return [f"agent coverage {measured:.2f}% is below the floor {floor}%: add tests or restore the ones removed"]
    if measured >= floor + STALE_MARGIN:
        return [f'agent coverage {measured:.2f}% is {measured - floor:.2f} points above the floor {floor}%: '
                f'raise "agent" in coverage-floors.json to {int(measured - 0.5)}']
    return []


def load_floor(path: Path) -> int:
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
    if not isinstance(data, dict) or "agent" not in data:
        bad('expected {"agent": <int>}')
        raise AssertionError  # unreachable: bad() exits
    floor = data["agent"]
    if type(floor) is not int or not 0 <= floor <= 100:
        bad('"agent" must be an integer from 0 to 100')
    return floor


def _run(cmd: list[str], env: dict[str, str], *, capture: bool = False) -> str:
    # Test output goes to stderr so stdout stays the table and the result (CI tees it).
    p = subprocess.run([sys.executable, "-m", "coverage", *cmd], cwd=ROOT, env=env, text=True,
                       stdout=subprocess.PIPE if capture else sys.stderr)
    if p.returncode != 0:
        print(f"coverage {cmd[0]} failed (exit {p.returncode})", file=sys.stderr)
        sys.exit(1)
    return p.stdout or ""


def measure() -> float:
    """Total coverage of agent/ over the same two runs the CI job always made; prints the table."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "COVERAGE_FILE": str(Path(tmp) / "data")}
        # Production code only: the test files are most of the statements and run by definition.
        src = ["--source=agent", "--omit=agent/tests/*", "-m", "unittest", "discover", "-s"]
        _run(["run", *src, "agent/tests"], env)
        _run(["run", "-a", *src, "tools/world-mock/tests"], env)
        print(_run(["report", "--skip-empty", "--sort=cover"], env, capture=True), end="")
        return float(_run(["report", "--format=total", "--precision=2"], env, capture=True))


def main(argv: list[str]) -> int:
    floor = load_floor(FLOORS)
    if importlib.util.find_spec("coverage") is None:
        print("coverage is not installed: pip install -r requirements-dev.txt", file=sys.stderr)
        return 2
    measured = measure()
    print(f"agent coverage {measured:.2f}% (floor {floor}%)")
    if "--report-only" in argv:
        return 0
    problems = check(measured, floor)
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
