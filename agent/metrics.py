#!/usr/bin/env python3
"""Derive Prometheus-style counters/histograms from the decision audit log
(agent.audit) — no dependency on prometheus_client (agent/ is stdlib-only,
per CONTRIBUTING.md), so this builds the exposition-format text itself.

Used by monitoring/agent_metrics_textfile.py (a textfile-collector script; see that
module's docstring for why textfile over an HTTP /metrics server) and
unit-tested directly (agent/tests/test_metrics.py) without touching the
filesystem beyond the fixtures it's given.
"""

import glob
import json
import logging
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field

from . import opcodes
from .audit_schema import AuditRecord
from .transport import ERROR_LOG_INTERVAL_S, _ErrorThrottle

# Bucket upper bounds (ms) for the LLM latency histogram. Chosen to span
# "fast local model" (~200ms) to "slow free-tier API, rate limited" (~20s).
LATENCY_BUCKETS_MS = (100, 250, 500, 1000, 2000, 5000, 10000, 20000)


@dataclass
class AgentMetrics:
    agent: str
    cycles_total: int = 0
    valid_tool_calls_total: int = 0
    invalid_tool_calls_total: int = 0
    actions_total: dict = field(default_factory=dict)       # (name, outcome) -> count
    prompt_tokens_total: int = 0
    completion_tokens_total: int = 0
    latency_bucket_counts: dict = field(default_factory=lambda: {b: 0 for b in LATENCY_BUCKETS_MS})
    latency_count: int = 0
    latency_sum_ms: float = 0.0
    deaths_total: int = 0
    levelups_total: int = 0
    first_ts: float | None = None
    last_ts: float | None = None
    last_xp: int | None = None
    first_xp: int | None = None
    jev_calls_total: int = 0
    jev_errors: dict = field(default_factory=lambda: {s: 0 for s in ("http_4xx", "http_5xx", "error", "budget_exhausted")})
    jev_fallback_total: int = 0
    jev_prompt_tokens_total: int = 0
    jev_completion_tokens_total: int = 0
    jev_cost_usd_total: float = 0.0
    jev_cost_24h_usd: float = 0.0
    jev_confidence: float | None = None
    jev_low_confidence_total: int = 0
    jev_latency_count: int = 0
    jev_latency_sum_ms: float = 0.0
    jev_latency_buckets: dict = field(default_factory=lambda: {b: 0 for b in LATENCY_BUCKETS_MS})
    prompt_stats: dict = field(default_factory=dict)  # prompt_id -> [count, last ts, last line] (#476)
    prompt_none: int = 0                               # cycles with no usable prompt_id
    bad_lines: int = 0                                 # audit lines skipped while reading this agent's files (#513)

    def outcome_pairs(self):
        return sorted(self.actions_total.items())

    @property
    def decisions_by_prompt(self) -> dict:
        """label -> count: the MAX_PROMPT_IDS ids with the newest record keep their own
        label, older ones are summed under `other`, no usable id is `none`. A literal id
        `other` or `none` adds to that label (summed, never overwritten, #513)."""
        newest = sorted(self.prompt_stats, key=lambda k: self.prompt_stats[k][1:], reverse=True)
        out: dict[str, int] = {}
        for i, k in enumerate(newest):
            label = k if i < MAX_PROMPT_IDS else "other"
            out[label] = out.get(label, 0) + self.prompt_stats[k][0]
        if self.prompt_none:
            out["none"] = out.get("none", 0) + self.prompt_none
        return out


MAX_PROMPT_IDS = 8  # distinct prompt_id labels per agent; the older ones are "other" (#476)
DECISIONS_BY_PROMPT_NAME = "wow_agent_decisions_by_prompt_total"
DECISIONS_BY_PROMPT_HELP = "Decision cycles by prompt id (newest 8 ids per agent, then other; none = no LLM prompt)"


def _count_prompt(m: AgentMetrics, rec: dict) -> None:
    pid = rec.get("prompt_id")
    if not isinstance(pid, str) or not pid:
        m.prompt_none += 1
        return
    ts = rec.get("ts")
    ts = ts if _finite(ts) else float("-inf")
    stat = m.prompt_stats.setdefault(pid, [0, float("-inf"), 0])
    stat[0] += 1
    # newest first by record ts; the line number breaks ties and covers records without ts
    stat[1:] = max(stat[1:], [ts, m.cycles_total])


def _finite(v) -> bool:
    """A JSON number usable as a count or a latency (NaN, Infinity and an int too large
    for a float, all from a bad line, are not)."""
    try:
        return isinstance(v, (int, float)) and math.isfinite(v)
    except OverflowError:
        return False


def _label_value(v) -> str:
    """Escape one label value per the Prometheus text format (backslash, quote, newline).
    What UTF-8 cannot encode (a lone surrogate, from a JSON "\\ud800") is written as its
    backslash-u text, so the .prom file can always be written and two distinct surrogates
    stay two distinct series (#513). Every label value is written through here."""
    text = str(v).encode("utf-8", "backslashreplace").decode("utf-8")
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels) -> str:
    """`{k="v",...}` in the order given, every value through _label_value."""
    return "{" + ",".join(f'{k}="{_label_value(v)}"' for k, v in labels.items()) + "}"


def _bucket_for(latency_ms: float) -> int | None:
    for b in LATENCY_BUCKETS_MS:
        if latency_ms <= b:
            return b
    return None  # +Inf bucket, handled separately


def iter_records(path: str, on_bad=None):
    """Yield parsed JSON records from one JSONL file. A line that is not UTF-8, not JSON,
    or JSON but not an object is skipped and `on_bad()` is called for it (#513); blank
    lines are skipped silently. Never raises on file content."""
    with open(path, "rb") as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):  # bad UTF-8 and bad JSON are both ValueError
                obj = None
            if not isinstance(obj, dict):
                if on_bad is not None:
                    on_bad()
                continue
            yield AuditRecord.from_json(obj).to_json()


def read_audit(paths) -> tuple[list[dict], dict[str, int]]:
    """Records of every file, and the lines skipped per agent. A file's agent is its
    directory name (`<base>/<agent>/<day>.jsonl`), since a bad line says nothing about itself."""
    records, bad = [], {}
    for path in paths:
        agent = os.path.basename(os.path.dirname(path))

        def on_bad(agent=agent):
            bad[agent] = bad.get(agent, 0) + 1

        records.extend(iter_records(path, on_bad))
    return records, bad


def find_audit_files(base_dir: str, agent: str | None = None) -> list[str]:
    """All `<base_dir>/<agent-or-*>/*.jsonl` files (rotated `.jsonl.N` files
    are included too, since a metrics pass should see everything retained,
    not just the live file)."""
    pattern_dir = agent if agent else "*"
    files = glob.glob(os.path.join(base_dir, pattern_dir, "*.jsonl"))
    files += glob.glob(os.path.join(base_dir, pattern_dir, "*.jsonl.*"))
    return sorted(files)


def _derive_jev(m: AgentMetrics, rec: dict, ts, now: float) -> None:
    """Fold one audit record's Jev status/usage/confidence/latency into `m`."""
    status = rec.get("jev_status")
    if status == "success":
        m.jev_calls_total += 1
    elif isinstance(status, str) and status in m.jev_errors:
        m.jev_errors[status] += 1
    if rec.get("brain") == "llm" and rec.get("fallback"):
        m.jev_fallback_total += 1
    usage = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
    for key, attr in (("input_tokens", "jev_prompt_tokens_total"),
                      ("output_tokens", "jev_completion_tokens_total")):
        value = usage.get(key)
        if _finite(value):
            setattr(m, attr, getattr(m, attr) + int(value))
    cost = usage.get("cost")
    if _finite(cost):
        m.jev_cost_usd_total += float(cost)
        if _finite(ts) and ts >= now - 86400:
            m.jev_cost_24h_usd += float(cost)
    if rec.get("confidence_rule") == "low_confidence_safe_fallback":
        m.jev_low_confidence_total += 1
    confidence = rec.get("confidence")
    if rec.get("brain") == "jev" and _finite(confidence):
        m.jev_confidence = float(confidence)
    latency = rec.get("latency_ms")
    if _finite(latency) and status is not None:
        m.jev_latency_count += 1
        m.jev_latency_sum_ms += latency
        bucket = _bucket_for(latency)
        if bucket is not None:
            for bound in LATENCY_BUCKETS_MS:
                if bound >= bucket:
                    m.jev_latency_buckets[bound] += 1


def _as_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _derive_record(m: AgentMetrics, rec: dict, now: float) -> None:
    """Fold one audit record into `m`. Every field is type-checked: a wrong type reads as
    absent, never raises (#513)."""
    m.cycles_total += 1
    ts = rec.get("ts")
    if _finite(ts):
        m.first_ts = ts if m.first_ts is None else min(m.first_ts, ts)
        m.last_ts = ts if m.last_ts is None else max(m.last_ts, ts)

    _derive_jev(m, rec, ts, now)
    _count_prompt(m, rec)

    valid = bool(rec.get("valid", False))
    result = _as_dict(rec.get("result"))
    ok = bool(result.get("ok", False))
    if valid:
        m.valid_tool_calls_total += 1
    else:
        m.invalid_tool_calls_total += 1

    name = _as_dict(rec.get("tool_call")).get("name")
    name = name if isinstance(name, str) and name else "none"
    outcome = "ok" if (valid and ok) else "invalid" if not valid else "failed"
    key = (name, outcome)
    m.actions_total[key] = m.actions_total.get(key, 0) + 1

    pt, ct = rec.get("prompt_tokens"), rec.get("completion_tokens")
    if _finite(pt):
        m.prompt_tokens_total += int(pt)
    if _finite(ct):
        m.completion_tokens_total += int(ct)

    latency = rec.get("latency_ms")
    if _finite(latency):
        m.latency_count += 1
        m.latency_sum_ms += latency
        b = _bucket_for(latency)
        if b is not None:
            # Cumulative histogram: every bucket >= this one gets +1.
            for bucket in LATENCY_BUCKETS_MS:
                if bucket >= b:
                    m.latency_bucket_counts[bucket] += 1

    detail = _as_dict(result.get("detail"))
    if detail.get("died") or detail.get("death"):
        m.deaths_total += 1
    if detail.get("leveled_up") or detail.get("level_up"):
        m.levelups_total += 1

    xp = detail.get("xp")
    if xp is None:
        xp = _as_dict(rec.get("snapshot")).get("xp")
    if _finite(xp):
        m.last_xp = int(xp)
        if m.first_xp is None:
            m.first_xp = int(xp)


def derive_metrics(records, bad_lines: dict | None = None) -> dict:
    """records: iterable of parsed audit-log dicts (any agent mixed in is
    fine — grouped by the `agent` field). Returns {agent_name: AgentMetrics}.
    `bad_lines` ({agent: count}, from read_audit) sets AgentMetrics.bad_lines, and gives
    an agent with only bad lines a row (#513).

    Deaths/level-ups/XP are derived from `result.detail`/`snapshot` fields
    when present rather than a dedicated schema field, since UM-51's record
    shape doesn't reserve one — this reads best-effort, defaulting to 0/None
    when the data isn't there (e.g. synthetic fixtures)."""
    by_agent: dict[str, AgentMetrics] = {}
    now = time.time()

    for rec in records:
        agent = rec.get("agent")
        agent = agent if isinstance(agent, str) and agent else "unknown"
        _derive_record(by_agent.setdefault(agent, AgentMetrics(agent=agent)), rec, now)
    for agent, count in (bad_lines or {}).items():
        by_agent.setdefault(agent, AgentMetrics(agent=agent)).bad_lines += count
    return by_agent


def xp_per_hour(m: AgentMetrics) -> float | None:
    if m.first_xp is None or m.last_xp is None or m.first_ts is None or m.last_ts is None:
        return None
    try:
        elapsed_hours = (m.last_ts - m.first_ts) / 3600.0
        if elapsed_hours <= 0:
            return None
        return (m.last_xp - m.first_xp) / elapsed_hours
    except OverflowError:  # two ints, each finite, whose difference a float cannot hold (#513)
        return None


def _emit(lines: list[str], name: str, help_text: str, mtype: str) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {mtype}")


def _jev_sample_lines(m: AgentMetrics) -> list[str]:
    lines = []
    label = _labels(agent=m.agent)
    for name, value in (("wow_agent_jev_calls_total", m.jev_calls_total),
                        ("wow_agent_jev_fallback_total", m.jev_fallback_total),
                        ("wow_agent_jev_prompt_tokens_total", m.jev_prompt_tokens_total),
                        ("wow_agent_jev_completion_tokens_total", m.jev_completion_tokens_total),
                        ("wow_agent_jev_cost_usd_total", m.jev_cost_usd_total),
                        ("wow_agent_jev_cost_usd_24h", m.jev_cost_24h_usd),
                        ("wow_agent_jev_low_confidence_total", m.jev_low_confidence_total)):
        lines.append(f"{name}{label} {value}")
    for status, count in m.jev_errors.items():
        lines.append(f"wow_agent_jev_errors_total{_labels(agent=m.agent, status=status)} {count}")
    if m.jev_confidence is not None:
        lines.append(f"wow_agent_jev_confidence{label} {m.jev_confidence}")
    for bucket in LATENCY_BUCKETS_MS:
        lines.append(f"wow_agent_jev_latency_ms_bucket{_labels(agent=m.agent, le=bucket)} {m.jev_latency_buckets[bucket]}")
    lines.append(f"wow_agent_jev_latency_ms_bucket{_labels(agent=m.agent, le='+Inf')} {m.jev_latency_count}")
    lines.append(f"wow_agent_jev_latency_ms_sum{label} {m.jev_latency_sum_ms}")
    lines.append(f"wow_agent_jev_latency_ms_count{label} {m.jev_latency_count}")
    return lines


def _core_sample_lines(by_agent: dict) -> list[str]:
    """The audit-derived families up to and including wow_agent_xp_per_hour."""
    lines: list[str] = []
    _emit(lines, "wow_agent_audit_cycles_total", "Think cycles recorded in the audit log", "counter")
    for m in by_agent.values():
        lines.append(f"wow_agent_audit_cycles_total{_labels(agent=m.agent)} {m.cycles_total}")

    _emit(lines, "wow_agent_audit_tool_calls_total", "Tool calls by validity", "counter")
    for m in by_agent.values():
        lines.append(f"wow_agent_audit_tool_calls_total{_labels(agent=m.agent, valid='true')} {m.valid_tool_calls_total}")
        lines.append(f"wow_agent_audit_tool_calls_total{_labels(agent=m.agent, valid='false')} {m.invalid_tool_calls_total}")

    _emit(lines, "wow_agent_action_total", "Actions taken, by action name and outcome (ok/invalid/failed)", "counter")
    for m in by_agent.values():
        for (name, outcome), count in m.outcome_pairs():
            lines.append(f"wow_agent_action_total{_labels(agent=m.agent, action=name, outcome=outcome)} {count}")

    _emit(lines, "wow_agent_llm_prompt_tokens_total", "Prompt tokens consumed", "counter")
    for m in by_agent.values():
        lines.append(f"wow_agent_llm_prompt_tokens_total{_labels(agent=m.agent)} {m.prompt_tokens_total}")

    _emit(lines, "wow_agent_llm_completion_tokens_total", "Completion tokens consumed", "counter")
    for m in by_agent.values():
        lines.append(f"wow_agent_llm_completion_tokens_total{_labels(agent=m.agent)} {m.completion_tokens_total}")

    _emit(lines, "wow_agent_llm_latency_ms", "LLM call latency histogram", "histogram")
    for m in by_agent.values():
        for bucket in LATENCY_BUCKETS_MS:
            lines.append(f"wow_agent_llm_latency_ms_bucket{_labels(agent=m.agent, le=bucket)} {m.latency_bucket_counts[bucket]}")
        lines.append(f"wow_agent_llm_latency_ms_bucket{_labels(agent=m.agent, le='+Inf')} {m.latency_count}")
        lines.append(f"wow_agent_llm_latency_ms_sum{_labels(agent=m.agent)} {m.latency_sum_ms}")
        lines.append(f"wow_agent_llm_latency_ms_count{_labels(agent=m.agent)} {m.latency_count}")

    _emit(lines, "wow_agent_deaths_total", "Deaths observed in the audit log", "counter")
    for m in by_agent.values():
        lines.append(f"wow_agent_deaths_total{_labels(agent=m.agent)} {m.deaths_total}")

    _emit(lines, "wow_agent_levelups_total", "Level-ups observed in the audit log", "counter")
    for m in by_agent.values():
        lines.append(f"wow_agent_levelups_total{_labels(agent=m.agent)} {m.levelups_total}")

    _emit(lines, "wow_agent_xp_per_hour", "XP gained per hour, over the audit log's time span", "gauge")
    for m in by_agent.values():
        rate = xp_per_hour(m)
        if rate is not None:
            lines.append(f"wow_agent_xp_per_hour{_labels(agent=m.agent)} {rate}")
    return lines


def _tail_sample_lines(by_agent: dict) -> list[str]:
    """The Jev families, decisions by prompt id, and bad audit lines (appended last, #513)."""
    lines: list[str] = []
    for name, help_text, mtype in (
        ("wow_agent_jev_calls_total", "Successful Jev Decisions API calls", "counter"),
        ("wow_agent_jev_errors_total", "Jev decision errors by bounded status class", "counter"),
        ("wow_agent_jev_fallback_total", "Cycles where Jev fell back to another brain", "counter"),
        ("wow_agent_jev_prompt_tokens_total", "Jev input tokens consumed", "counter"),
        ("wow_agent_jev_completion_tokens_total", "Jev output tokens consumed", "counter"),
        ("wow_agent_jev_cost_usd_total", "Jev reported cost in USD", "counter"),
        ("wow_agent_jev_cost_usd_24h", "Jev reported cost in the last 24 hours", "gauge"),
        ("wow_agent_jev_confidence", "Latest Jev decision confidence", "gauge"),
        ("wow_agent_jev_low_confidence_total", "Low confidence substitutions recorded", "counter"),
        ("wow_agent_jev_latency_ms", "Jev Decisions API latency histogram", "histogram"),
    ):
        _emit(lines, name, help_text, mtype)
    for m in by_agent.values():
        lines += _jev_sample_lines(m)

    _emit(lines, DECISIONS_BY_PROMPT_NAME, DECISIONS_BY_PROMPT_HELP, "counter")
    for m in by_agent.values():
        for pid, count in sorted(m.decisions_by_prompt.items()):
            lines.append(f"{DECISIONS_BY_PROMPT_NAME}{_labels(agent=m.agent, prompt_id=pid)} {count}")

    _emit(lines, "wow_agent_audit_bad_lines_total",
          "Audit lines skipped by the exporter: not UTF-8, not JSON, or JSON that is not an object", "counter")
    for m in by_agent.values():
        lines.append(f"wow_agent_audit_bad_lines_total{_labels(agent=m.agent)} {m.bad_lines}")
    return lines


def render_prometheus_text(by_agent: dict) -> str:
    """Render the derived metrics as Prometheus exposition-format text
    (what a textfile collector expects a `.prom` file to contain). Every label value
    goes through _labels/_label_value (#513)."""
    lines = _core_sample_lines(by_agent) + _tail_sample_lines(by_agent)
    return "\n".join(lines) + "\n"


# ── Swallowed errors (#306) ───────────────────────────────────────────────
# Live, in-process (unlike the audit-derived metrics above): one counter per
# call site label, shared by every thread, rendered by the agent's GET /metrics.
SWALLOWED: dict[str, int] = {}
_swallowed_lock = threading.Lock()
_swallowed_throttle = _ErrorThrottle(ERROR_LOG_INTERVAL_S)


def swallowed(where: str, logger: logging.Logger, detail: str = "", *, key=None,
              throttle=None, level: int = logging.ERROR) -> None:
    """Call from inside an `except Exception:` block that swallows the error.

    Counts it under `where` (wow_agent_swallowed_errors_total{where=...}) and logs
    it with traceback, at most once per (where, exception type) per interval, or per
    `key` when the caller has a better one (the opcode) and its own `throttle`.
    `detail` is context the caller already knows (opcode, action name); never put
    packet payloads, credentials or prompts in it. Does not re-raise."""
    exc = sys.exc_info()[1]
    with _swallowed_lock:
        SWALLOWED[where] = SWALLOWED.get(where, 0) + 1
    should, suppressed = (throttle or _swallowed_throttle).check(
        key if key is not None else (where, type(exc).__name__))
    if should:
        logger.log(level, "swallowed error in %s%s: %s: %s (%d more suppressed since last report)",
                   where, f" ({detail})" if detail else "", type(exc).__name__, exc, suppressed,
                   exc_info=True)


def render_swallowed(agent: str) -> str:
    """The swallowed-error counter as Prometheus exposition text."""
    with _swallowed_lock:
        items = sorted(SWALLOWED.items())
    lines = ["# HELP wow_agent_swallowed_errors_total Exceptions caught and swallowed, by call site",
             "# TYPE wow_agent_swallowed_errors_total counter"]
    lines += [f'wow_agent_swallowed_errors_total{{agent="{agent}",where="{w}"}} {n}' for w, n in items]
    return "\n".join(lines) + "\n"


# ── Packet router counters (#494) ─────────────────────────────────────────
# Live and in-process like SWALLOWED: (kind, opcode) -> count, bumped by the router's
# injected sink (handlers.build_router wires count_router_event). Opcode names are
# resolved only when rendering, so the per-packet cost is one locked dict increment.
_ROUTER_METRICS = {   # kind -> (name, help). Names are literals: the #280 test reads this file as text.
    "packets": ("wow_agent_router_packets_total", "Packets dispatched by the router, handled or not"),
    "unhandled": ("wow_agent_router_unhandled_total", "Packets with no registered handler"),
    "truncated": ("wow_agent_router_truncated_total", "Packets whose handler raised PacketTruncated"),
    "handler_errors": ("wow_agent_router_handler_errors_total", "Packets whose handler raised any other exception"),
}
ROUTER_KINDS = tuple(_ROUTER_METRICS)
ROUTER_COUNTS: dict[tuple[str, int], int] = {}
_router_lock = threading.Lock()


def count_router_event(kind: str, opcode: int) -> None:
    with _router_lock:
        key = (kind, opcode)
        ROUTER_COUNTS[key] = ROUTER_COUNTS.get(key, 0) + 1


def render_router(agent: str) -> str:
    """The four router counters as Prometheus exposition text, labelled by opcode name."""
    names = {v: k for k, v in vars(opcodes).items() if k.isupper() and isinstance(v, int)}
    with _router_lock:
        items = sorted(ROUTER_COUNTS.items())
    lines = []
    for kind in ROUTER_KINDS:
        name, help_text = _ROUTER_METRICS[kind]
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} counter"]
        lines += [f'{name}{{agent="{agent}",opcode="{names.get(op, "0x%03X" % op)}"}} {n}'
                  for (k, op), n in items if k == kind]
    return "\n".join(lines) + "\n"


# ── Reconnect attempts (#402) ─────────────────────────────────────────────
# Process-wide like the router counters: one count per reconnect wait the supervisor
# (agent.supervisor.supervise_connection) takes, by reason. All reasons render, at 0 until
# they happen, so rate() works from the first scrape.
RECONNECT_REASONS = ("connect_failed", "auth_rejected", "disconnected")
RECONNECTS: dict[str, int] = dict.fromkeys(RECONNECT_REASONS, 0)
_reconnect_lock = threading.Lock()


def count_reconnect(reason: str) -> None:
    with _reconnect_lock:
        RECONNECTS[reason] += 1   # KeyError on an unknown reason: label values stay bounded


def render_reconnects(agent: str) -> str:
    """wow_agent_reconnect_attempts_total as Prometheus exposition text."""
    with _reconnect_lock:
        counts = dict(RECONNECTS)
    lines = ["# HELP wow_agent_reconnect_attempts_total Reconnect waits by reason (connect_failed, auth_rejected, disconnected)",
             "# TYPE wow_agent_reconnect_attempts_total counter"]
    lines += [f'wow_agent_reconnect_attempts_total{{agent="{agent}",reason="{r}"}} {counts[r]}'
              for r in RECONNECT_REASONS]
    return "\n".join(lines) + "\n"
