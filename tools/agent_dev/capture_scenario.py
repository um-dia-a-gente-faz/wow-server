#!/usr/bin/env python3
"""Turn one decision-audit record into a brain scenario (gh-475).

Usage: python3 -m tools.agent_dev.capture_scenario <audit.jsonl> [--cycle N]

Prints a `"captured_<agent>_<cycle>": dict(...)` entry to paste into SCENARIOS in
agent/tests/test_brain_scenarios.py. The snapshot (and history, if the record carries one)
is a Python literal; `offered` and `chosen` are left as `...` for the author to write, with
the action the brain chose in that record as a comment. Stdlib only.

  --cycle N   the record with cycle N (default: the last record in the file)

Exits 2 with a one-line message when there is no such record or it carries no snapshot.
"""

import argparse
import json
import pprint
import sys

from tools.agent_dev.replay import load_records


def find_record(path: str, cycle: int | None):
    records = list(load_records(path))
    if cycle is None:
        return records[-1] if records else None
    matches = [r for r in records if r.get("cycle") == cycle]
    return matches[-1] if matches else None


def scenario_text(rec: dict) -> str:
    key = json.dumps(f"captured_{rec.get('agent') or 'agent'}_{rec.get('cycle')}")
    call = rec.get("tool_call") or {}
    chosen = f"{call.get('name')!r} {(call.get('args') or {})!r}"
    lines = [f"{key}: dict(",
             f"    snapshot={pprint.pformat(rec['snapshot'], width=100)},"]
    if "history" in rec:
        lines.append(f"    history={pprint.pformat(rec['history'], width=100)},")
    lines += [f"    # brain chose: {chosen}",
              "    offered=...,",
              "    chosen=...,",
              "),"]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m tools.agent_dev.capture_scenario")
    p.add_argument("file", help="JSONL audit log file")
    p.add_argument("--cycle", type=int, default=None,
                   help="cycle number of the record to capture (default: the last record)")
    args = p.parse_args(argv)

    rec = find_record(args.file, args.cycle)
    if rec is None:
        print(f"capture: no record for cycle {args.cycle if args.cycle is not None else 'last'} "
              f"in {args.file}", file=sys.stderr)
        return 2
    if not isinstance(rec.get("snapshot"), dict):
        print(f"capture: cycle {rec.get('cycle')} carries no snapshot", file=sys.stderr)
        return 2
    print(scenario_text(rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
