#!/usr/bin/env python3
"""N-sample run of the brain scenarios (#541, the model tier of #388 and #475).

Usage: AGENT_BRAIN=jev python3 -m tools.agent_dev.scenario_samples [--n 5] [--threshold 0.2]

For each scenario in SCENARIOS (agent/tests/test_brain_scenarios.py) that is not a known_gap, it
asks the real Brain N times and prints the share of decisions that break the scenario's `chosen`
predicate. Exit 0 when every share is within --threshold, 1 when one is above it (or a decision
fails), 2 when no Jev is configured and selected.

MANUAL ONLY. This calls a paid model: cost = scenarios x N calls, Jev only (any LLM is dropped).
Run it by hand before a brain change is merged. It is never run by scripts/check.sh or CI, and it
needs JEV_BASE_URL and JEV_API_KEY (or OPENROUTER_API_KEY) from the environment. Stdlib only.
"""

import argparse
import sys
from typing import NamedTuple

from agent.brain import Brain, BrainError
from agent.config import load_config
from agent.tests.test_brain_scenarios import ME, SCENARIOS, _Handles


class Row(NamedTuple):
    name: str
    n: int
    violations: int
    share: float


def sample(scenarios: dict, brain, n: int) -> list[Row]:
    """Decide each enforced scenario n times; count the decisions its `chosen` predicate rejects."""
    rows = []
    for name, s in scenarios.items():
        if s.get("known_gap") or "chosen" not in s:
            continue
        handles = _Handles(s["handles"]) if "handles" in s else None
        bad = 0
        for _ in range(n):
            d = brain.decide(s["snapshot"], my_guid=s.get("my_guid", ME), history=s.get("history"),
                             handles=handles)
            if not s["chosen"](d.action, d.params):
                bad += 1
        rows.append(Row(name, n, bad, bad / n))
    return rows


def format_table(rows) -> str:
    width = max([len("scenario")] + [len(r.name) for r in rows])
    lines = [f"{'scenario':<{width}}  {'n':>3}  {'violations':>10}  {'share':>5}"]
    for r in rows:
        lines.append(f"{r.name:<{width}}  {r.n:>3}  {r.violations:>10}  {r.share:>5.2f}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m tools.agent_dev.scenario_samples",
                                description="N decisions per brain scenario against Jev (#541).")
    p.add_argument("--n", type=int, default=5, help="decisions per scenario (default 5)")
    p.add_argument("--threshold", type=float, default=0.2,
                   help="exit 1 when a scenario's violation share is above this (default 0.2)")
    args = p.parse_args(argv)
    if args.n < 1:
        p.error("--n must be at least 1")

    brain = Brain.from_config(load_config())
    if brain is None or brain.jev is None:
        print("scenario_samples: no Jev brain: set AGENT_BRAIN=jev with JEV_BASE_URL and JEV_API_KEY "
              "(or OPENROUTER_API_KEY)", file=sys.stderr)
        return 2
    brain.llm = None   # Jev only: every sample is a Jev decision, never an LLM stand-in

    try:
        rows = sample(SCENARIOS, brain, args.n)
    except BrainError as e:
        print(f"scenario_samples: a decision failed: {e}", file=sys.stderr)
        return 1
    print(format_table(rows))
    return 1 if any(r.share > args.threshold for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
