#!/usr/bin/env python3
"""Replay viewer for the decision audit log (UM-51, agent.audit).

Usage: python3 -m agent.tools.replay <file.jsonl> [--from CYCLE] [--failures]

Prints a readable, one-line-per-cycle timeline from a JSONL audit log
written by agent.audit.AuditLogger — stdlib only, no new dependencies.

  --from N       only show cycles with cycle >= N
  --failures     only show cycles where valid is false or result.ok is false
"""

import argparse
import json
import sys
import time

from agent.audit_schema import AuditRecord


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return str(ts)


def load_records(path: str):
    """Yield parsed records from a JSONL audit file, skipping (and warning
    about) any line that fails to parse rather than aborting the whole
    replay — a partially-written last line (process killed mid-write)
    shouldn't make the rest of the file unreadable."""
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"warning: {path}:{lineno}: skipping malformed line: {e}", file=sys.stderr)
                continue
            yield AuditRecord.from_json(raw).to_json()


def is_failure(rec: dict) -> bool:
    return not rec.get("valid", False) or not rec.get("result", {}).get("ok", False)


def format_record(rec: dict) -> str:
    cycle = rec.get("cycle")
    ts = _fmt_ts(rec.get("ts"))
    agent = rec.get("agent") or "?"
    tool_call = rec.get("tool_call") or {}
    name = tool_call.get("name") or "(none)"
    args = tool_call.get("args") or {}
    valid = rec.get("valid", False)
    result = rec.get("result") or {}
    ok = result.get("ok", False)
    error = result.get("error")

    status = "OK" if (valid and ok) else "INVALID" if not valid else "FAILED"
    latency = rec.get("latency_ms")
    latency_s = f"{latency:.0f}ms" if isinstance(latency, (int, float)) else "?"
    tokens = ""
    pt, ct = rec.get("prompt_tokens"), rec.get("completion_tokens")
    if pt is not None or ct is not None:
        tokens = f" tokens={pt or 0}+{ct or 0}"

    line = (f"[{ts}] cycle={cycle:<5} {agent:<16} {status:<7} "
            f"action={name}({args}) latency={latency_s}{tokens}")
    if error:
        line += f"  error={error!r}"
    reflex = rec.get("reflex") or {}
    if reflex:
        line += f"  reflex={reflex}"
    goal = rec.get("goal")
    if goal:
        line += f"  goal={goal!r}"
    if rec.get("snapshot") is not None:
        line += "  [full snapshot attached]"
    return line


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m agent.tools.replay")
    p.add_argument("file", help="JSONL audit log file")
    p.add_argument("--from", dest="from_cycle", type=int, default=None,
                   help="only show cycles >= this cycle number")
    p.add_argument("--failures", action="store_true",
                   help="only show cycles that were invalid or failed")
    args = p.parse_args(argv)

    shown = 0
    total = 0
    failures = 0
    for rec in load_records(args.file):
        total += 1
        if is_failure(rec):
            failures += 1
        if args.from_cycle is not None and rec.get("cycle", 0) < args.from_cycle:
            continue
        if args.failures and not is_failure(rec):
            continue
        print(format_record(rec))
        shown += 1

    print(f"--- {shown}/{total} cycle(s) shown, {failures} failure(s)/invalid in file ---",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
