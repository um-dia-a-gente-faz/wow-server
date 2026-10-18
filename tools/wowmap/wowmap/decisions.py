"""A character's recent decisions, read from the end of its agent's audit day files (#326).

`<audit_dir>/<agent>/<day>.jsonl` holds `agent.audit_schema.AuditRecord.to_json()` lines. Only the
top-level day files are read (lifecycle/ and unmet/ are subdirectories), the newest 2, and only the
last MAX_BYTES of each, so a busy agent never makes this load a whole file.
"""
import json
import os
import re

MAX_FILES = 2
MAX_BYTES = 512 * 1024
_DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl(?:\.(\d+))?$")   # the pattern activity.py uses


def _agent_dir(audit_dir, name):
    try:
        for d in os.listdir(audit_dir):
            if d.lower() == name.lower() and os.path.isdir(os.path.join(audit_dir, d)):
                return os.path.join(audit_dir, d)
    except OSError:
        pass
    return None


def _tail_lines(path):
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - MAX_BYTES))
        data = f.read()
    lines = data.split(b"\n")
    return lines[1:] if size > MAX_BYTES else lines   # the first line is cut mid-way


def _item(rec):
    call = rec.get("tool_call") or {}
    res = rec.get("result") or {}
    ts, args = rec["ts"], call.get("args") or {}
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not isinstance(args, dict):
        raise TypeError("not a decision record")
    return {"t": ts, "action": call.get("name"), "args": args,
            "ok": bool(res.get("ok")), "error": res.get("error"),
            "confidence": rec.get("confidence"), "brain": rec.get("brain"),
            # The audit record carries no rationale for the chosen option (history_notes explains
            # dropped options only), so there is nothing to put here yet.
            "reason": None}


def recent(audit_dir, name, limit):
    """Newest-first decisions of the agent called `name` (any case); [] for an unknown agent."""
    base = _agent_dir(audit_dir, name)
    if base is None:
        return []
    found = [(m.group(1), int(m.group(2) or 0), fn) for fn in os.listdir(base)
             if (m := _DAY_FILE_RE.match(fn)) and os.path.isfile(os.path.join(base, fn))]
    # newest day first; within a day the live file (index 0), then .1, .2 ... .10 numerically
    found.sort(key=lambda f: f[1])
    found.sort(key=lambda f: f[0], reverse=True)
    days = [f[2] for f in found[:MAX_FILES]]
    out = []
    for fn in days:
        try:
            lines = _tail_lines(os.path.join(base, fn))
        except OSError:
            continue
        for line in lines:
            try:
                out.append(_item(json.loads(line)))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue   # malformed line, or not an audit record
    out.sort(key=lambda d: d["t"], reverse=True)
    return out[:limit]
