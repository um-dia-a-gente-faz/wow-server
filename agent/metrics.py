#!/usr/bin/env python3
"""Derive Prometheus-style counters/histograms from the decision audit log
(agent.audit) — no dependency on prometheus_client (agent/ is stdlib-only,
per CONTRIBUTING.md), so this builds the exposition-format text itself.

Used by agent.tools.metrics_textfile (a textfile-collector script; see that
module's docstring for why textfile over an HTTP /metrics server) and
unit-tested directly (agent/tests/test_metrics.py) without touching the
filesystem beyond the fixtures it's given.
"""

import glob
import json
import os
import time
from dataclasses import dataclass, field

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

    def outcome_pairs(self):
        return sorted(self.actions_total.items())


def _bucket_for(latency_ms: float) -> int | None:
    for b in LATENCY_BUCKETS_MS:
        if latency_ms <= b:
            return b
    return None  # +Inf bucket, handled separately


def iter_records(path: str):
    """Yield parsed JSON records from one JSONL file, skipping malformed
    lines (see agent.tools.replay.load_records — same rationale)."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def find_audit_files(base_dir: str, agent: str | None = None) -> list[str]:
    """All `<base_dir>/<agent-or-*>/*.jsonl` files (rotated `.jsonl.N` files
    are included too, since a metrics pass should see everything retained,
    not just the live file)."""
    pattern_dir = agent if agent else "*"
    files = glob.glob(os.path.join(base_dir, pattern_dir, "*.jsonl"))
    files += glob.glob(os.path.join(base_dir, pattern_dir, "*.jsonl.*"))
    return sorted(files)


def derive_metrics(records) -> dict:
    """records: iterable of parsed audit-log dicts (any agent mixed in is
    fine — grouped by the `agent` field). Returns {agent_name: AgentMetrics}.

    Deaths/level-ups/XP are derived from `result.detail`/`snapshot` fields
    when present rather than a dedicated schema field, since UM-51's record
    shape doesn't reserve one — this reads best-effort, defaulting to 0/None
    when the data isn't there (e.g. synthetic fixtures)."""
    by_agent: dict[str, AgentMetrics] = {}

    for rec in records:
        agent = rec.get("agent", "unknown")
        m = by_agent.setdefault(agent, AgentMetrics(agent=agent))

        m.cycles_total += 1
        ts = rec.get("ts")
        if isinstance(ts, (int, float)):
            m.first_ts = ts if m.first_ts is None else min(m.first_ts, ts)
            m.last_ts = ts if m.last_ts is None else max(m.last_ts, ts)

        valid = bool(rec.get("valid", False))
        result = rec.get("result") or {}
        ok = bool(result.get("ok", False))
        if valid:
            m.valid_tool_calls_total += 1
        else:
            m.invalid_tool_calls_total += 1

        tool_call = rec.get("tool_call") or {}
        name = tool_call.get("name") or "none"
        outcome = "ok" if (valid and ok) else "invalid" if not valid else "failed"
        key = (name, outcome)
        m.actions_total[key] = m.actions_total.get(key, 0) + 1

        pt, ct = rec.get("prompt_tokens"), rec.get("completion_tokens")
        if isinstance(pt, (int, float)):
            m.prompt_tokens_total += int(pt)
        if isinstance(ct, (int, float)):
            m.completion_tokens_total += int(ct)

        latency = rec.get("latency_ms")
        if isinstance(latency, (int, float)):
            m.latency_count += 1
            m.latency_sum_ms += latency
            b = _bucket_for(latency)
            if b is not None:
                # Cumulative histogram: every bucket >= this one gets +1.
                for bucket in LATENCY_BUCKETS_MS:
                    if bucket >= b:
                        m.latency_bucket_counts[bucket] += 1

        detail = result.get("detail") if isinstance(result.get("detail"), dict) else {}
        if detail.get("died") or detail.get("death"):
            m.deaths_total += 1
        if detail.get("leveled_up") or detail.get("level_up"):
            m.levelups_total += 1

        xp = detail.get("xp")
        if xp is None:
            snap = rec.get("snapshot") or {}
            xp = snap.get("xp") if isinstance(snap, dict) else None
        if isinstance(xp, (int, float)):
            m.last_xp = int(xp)
            if m.first_xp is None:
                m.first_xp = int(xp)

    return by_agent


def xp_per_hour(m: AgentMetrics) -> float | None:
    if m.first_xp is None or m.last_xp is None or m.first_ts is None or m.last_ts is None:
        return None
    elapsed_hours = (m.last_ts - m.first_ts) / 3600.0
    if elapsed_hours <= 0:
        return None
    return (m.last_xp - m.first_xp) / elapsed_hours


def render_prometheus_text(by_agent: dict) -> str:
    """Render the derived metrics as Prometheus exposition-format text
    (what a textfile collector expects a `.prom` file to contain)."""
    lines = []

    def emit(name, help_text, mtype):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")

    emit("wow_agent_audit_cycles_total", "Think cycles recorded in the audit log", "counter")
    for m in by_agent.values():
        lines.append(f'wow_agent_audit_cycles_total{{agent="{m.agent}"}} {m.cycles_total}')

    emit("wow_agent_audit_tool_calls_total", "Tool calls by validity", "counter")
    for m in by_agent.values():
        lines.append(f'wow_agent_audit_tool_calls_total{{agent="{m.agent}",valid="true"}} {m.valid_tool_calls_total}')
        lines.append(f'wow_agent_audit_tool_calls_total{{agent="{m.agent}",valid="false"}} {m.invalid_tool_calls_total}')

    emit("wow_agent_action_total", "Actions taken, by action name and outcome (ok/invalid/failed)", "counter")
    for m in by_agent.values():
        for (name, outcome), count in m.outcome_pairs():
            lines.append(f'wow_agent_action_total{{agent="{m.agent}",action="{name}",outcome="{outcome}"}} {count}')

    emit("wow_agent_llm_prompt_tokens_total", "Prompt tokens consumed", "counter")
    for m in by_agent.values():
        lines.append(f'wow_agent_llm_prompt_tokens_total{{agent="{m.agent}"}} {m.prompt_tokens_total}')

    emit("wow_agent_llm_completion_tokens_total", "Completion tokens consumed", "counter")
    for m in by_agent.values():
        lines.append(f'wow_agent_llm_completion_tokens_total{{agent="{m.agent}"}} {m.completion_tokens_total}')

    emit("wow_agent_llm_latency_ms", "LLM call latency histogram", "histogram")
    for m in by_agent.values():
        for bucket in LATENCY_BUCKETS_MS:
            lines.append(f'wow_agent_llm_latency_ms_bucket{{agent="{m.agent}",le="{bucket}"}} {m.latency_bucket_counts[bucket]}')
        lines.append(f'wow_agent_llm_latency_ms_bucket{{agent="{m.agent}",le="+Inf"}} {m.latency_count}')
        lines.append(f'wow_agent_llm_latency_ms_sum{{agent="{m.agent}"}} {m.latency_sum_ms}')
        lines.append(f'wow_agent_llm_latency_ms_count{{agent="{m.agent}"}} {m.latency_count}')

    emit("wow_agent_deaths_total", "Deaths observed in the audit log", "counter")
    for m in by_agent.values():
        lines.append(f'wow_agent_deaths_total{{agent="{m.agent}"}} {m.deaths_total}')

    emit("wow_agent_levelups_total", "Level-ups observed in the audit log", "counter")
    for m in by_agent.values():
        lines.append(f'wow_agent_levelups_total{{agent="{m.agent}"}} {m.levelups_total}')

    emit("wow_agent_xp_per_hour", "XP gained per hour, over the audit log's time span", "gauge")
    for m in by_agent.values():
        rate = xp_per_hour(m)
        if rate is not None:
            lines.append(f'wow_agent_xp_per_hour{{agent="{m.agent}"}} {rate}')

    return "\n".join(lines) + "\n"
