#!/usr/bin/env python3
"""Turn one decision-audit record into a brain scenario (gh-475).

Usage: python3 -m tools.agent_dev.capture_scenario <audit.jsonl> [--cycle N] [--me HANDLE]

Prints a `"captured_<agent>_<cycle>": dict(...)` entry to paste into SCENARIOS in
agent/tests/test_brain_scenarios.py. The record keeps GUIDs as handle strings ("u1", "p3") and
the audit log does not keep the handle table, so the entry carries its own: `handles` maps each
handle the snapshot shows to an integer (numbered in order of first appearance), and `my_guid`
is the number of the player's own handle, --me. `offered` and `chosen` are left as `...` for the
author, with the action the brain chose in that record as a comment. Stdlib only.

  --cycle N   the record with cycle N (default: the last record in the file)
  --me H      the player's own handle (the one the attacking mobs target); without it my_guid is `...`

Exits 2 with a one-line message when the file cannot be read, a line is not a JSON object, there
is no such record, the record has no snapshot or a non-object tool_call, or --me is not a handle
in the snapshot (exact match).
"""

import argparse
import json
import pprint
import sys

from agent.audit_schema import AuditRecord


class CaptureError(Exception):
    pass


def read_records(path: str) -> list[dict]:
    """Typed records of a JSONL audit file. A line that is not JSON is skipped with a warning, as
    replay does; a line that is JSON but not an object is an error."""
    out = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"warning: {path}:{lineno}: skipping malformed line: {e}", file=sys.stderr)
                continue
            if not isinstance(raw, dict):
                raise CaptureError(f"{path}:{lineno}: line is not a JSON object")
            out.append(AuditRecord.from_json(raw).to_json())
    return out


def find_record(records: list[dict], cycle: int | None):
    if cycle is None:
        return records[-1] if records else None
    matches = [r for r in records if r.get("cycle") == cycle]
    return matches[-1] if matches else None


def _handles_in(value, out: dict) -> dict:
    """Handle strings in GUID-valued fields (`guid`, `*_guid`, `*_guids`), numbered from 1 in order
    of first appearance. Mirrors agent.handles.is_guid_key, which this tool may not import."""
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and (k == "guid" or k.endswith("_guid")) and isinstance(v, str):
                out.setdefault(v, len(out) + 1)
            elif isinstance(k, str) and k.endswith("_guids") and isinstance(v, list):
                for g in v:
                    if isinstance(g, str):
                        out.setdefault(g, len(out) + 1)
            else:
                _handles_in(v, out)
    elif isinstance(value, list):
        for v in value:
            _handles_in(v, out)
    return out


def scenario_text(rec: dict, me: str | None = None) -> str:
    key = json.dumps(f"captured_{rec.get('agent') or 'agent'}_{rec.get('cycle')}")
    call = rec.get("tool_call") or {}
    chosen = f"{call.get('name')!r} {(call.get('args') or {})!r}"
    handles = _handles_in(rec["snapshot"], {})
    if me is not None and me not in handles:
        raise CaptureError(f"--me {me!r} is not a handle in cycle {rec.get('cycle')}'s snapshot "
                           "(exact match; use the player's handle, the target of the units attacking you)")
    lines = [f"{key}: dict(",
             f"    snapshot={pprint.pformat(rec['snapshot'], width=100)},"]
    if handles:
        lines.append(f"    handles={pprint.pformat(handles, width=100)},")
        lines.append(f"    my_guid={handles[me] if me is not None else '...'},")
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
    p.add_argument("--me", default=None, metavar="HANDLE",
                   help="the player's own handle, e.g. p3 (becomes my_guid)")
    args = p.parse_args(argv)

    try:
        records = read_records(args.file)
    except CaptureError as e:
        print(f"capture: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"capture: cannot read {args.file}: {e.strerror or e}", file=sys.stderr)
        return 2
    except UnicodeDecodeError:
        print(f"capture: {args.file} is not UTF-8 text", file=sys.stderr)
        return 2

    rec = find_record(records, args.cycle)
    if rec is None:
        print(f"capture: no record for cycle {args.cycle if args.cycle is not None else 'last'} "
              f"in {args.file}", file=sys.stderr)
        return 2
    if not isinstance(rec.get("snapshot"), dict):
        print(f"capture: cycle {rec.get('cycle')} carries no snapshot", file=sys.stderr)
        return 2
    if rec.get("tool_call") is not None and not isinstance(rec["tool_call"], dict):
        print(f"capture: cycle {rec.get('cycle')} tool_call is not an object", file=sys.stderr)
        return 2
    try:
        print(scenario_text(rec, args.me))
    except CaptureError as e:
        print(f"capture: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
