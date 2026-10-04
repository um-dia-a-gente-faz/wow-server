#!/usr/bin/env python3
"""Decision audit log (UM-51).

One JSONL record per think cycle, written to
`AGENT_AUDIT_DIR/<agent>/<YYYY-MM-DD>.jsonl` (default base dir
`/data/audit`). Doubles as `docs/AI-AGENT-SPEC.md`'s Safety-section audit
log and as prompt-iteration data (see `docs/ROADMAP.md` Phase 3, item 3),
and is what `agent.tools.replay` and the metrics exporter
(`agent.tools.metrics_textfile`) both read.

Record shape (one JSON object per line):
    ts               float, unix epoch seconds
    agent            str, agent name (AGENT_NAME)
    cycle            int, monotonically increasing think-cycle counter
    snapshot_hash    str, sha256 of the compact-JSON snapshot
    snapshot         the full snapshot dict on every FULL_SNAPSHOT_EVERY'th
                      cycle or when the cycle was invalid/failed; otherwise
                      omitted (hash + this cycle's own record are enough to
                      tell "did perception change" without repeating ~40
                      nearby objects every 3s)
    prompt_tokens    int or None (only known LLM providers report usage;
                      Jev's usage.input_tokens)
    completion_tokens int or None
    model            str or None
    latency_ms       float or None (LLM/Jev call latency)
    tool_call        {"name": str or None, "args": dict}
    valid            bool — the tool call resolved to a registered action
                      with all required params (mirrors ThinkResult.ok up
                      to, but not including, in-game execution failures)
    result           {"ok": bool, "error": str or None}
    reflex           free-form dict describing reflex state (e.g. follow
                      reflex enabled/leader), or {} if none is active
    goal             str or None — the agent's current persona/goal, if any
    brain            "jev", "llm" or None (UM-101): the brain that decided this
                      cycle, or the last one tried when none did
    confidence       float or None: Jev's confidence in its choice (Jev only)
    fallback         str or None: why Jev did not decide (call failed or
                      cooling down) when it is configured and the LLM was
                      used or the cycle was skipped
    candidates       int or None: how many candidates Jev was offered
    confidence_threshold  float or None: JEV_MIN_CONFIDENCE applied (Jev only)
    confidence_rule  "acted", "low_confidence_safe_fallback" (the safe
                      candidate replaced Jev's choice) or "confidence_unknown"
                      (Jev reported none; acted as chosen); None off Jev
    overridden       str or None: candidate id Jev chose before the safe
                      substitution

Never writes the LLM API key or account password — nothing in this module
ever touches `Config.llm_api_key`/`password`; only `Config.redacted()`-safe
fields (agent_name, persona) reach a record.
"""

import glob
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("agent.audit")

DEFAULT_BASE_DIR = "/data/audit"
DEFAULT_RETENTION_DAYS = 14
DEFAULT_FULL_SNAPSHOT_EVERY = 20
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB per file before rotation

_DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl(\.\d+)?$")

# Defence in depth: even though callers never pass secrets into a record,
# scrub anything that looks like one before it hits disk (config.redacted()
# does the same for the "***"-masked account password).
_SECRET_KEYS = {"api_key", "llm_api_key", "password", "wow_password", "authorization"}


def _redact(obj):
    """Recursively replace any dict value whose key looks like a secret
    with "***", mirroring agent.config.Config.redacted()."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEYS:
                out[k] = "***" if v else "(unset)"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _compact_json(obj) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"), sort_keys=True)


def env_base_dir() -> str:
    return os.environ.get("AGENT_AUDIT_DIR", DEFAULT_BASE_DIR).strip() or DEFAULT_BASE_DIR


def env_retention_days() -> int:
    raw = os.environ.get("AGENT_AUDIT_RETENTION_DAYS", "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS


@dataclass
class AuditRecord:
    ts: float
    agent: str
    cycle: int
    snapshot_hash: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str | None = None
    latency_ms: float | None = None
    tool_call: dict = field(default_factory=lambda: {"name": None, "args": {}})
    valid: bool = False
    result: dict = field(default_factory=lambda: {"ok": False, "error": None})
    reflex: dict = field(default_factory=dict)
    goal: str | None = None
    brain: str | None = None
    confidence: float | None = None
    fallback: str | None = None
    candidates: int | None = None
    confidence_threshold: float | None = None
    confidence_rule: str | None = None
    overridden: str | None = None
    snapshot: dict | None = None  # only set when this cycle carries the full snapshot

    def to_dict(self) -> dict:
        d = {
            "ts": self.ts,
            "agent": self.agent,
            "cycle": self.cycle,
            "snapshot_hash": self.snapshot_hash,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "tool_call": self.tool_call,
            "valid": self.valid,
            "result": self.result,
            "reflex": self.reflex,
            "goal": self.goal,
            "brain": self.brain,
            "confidence": self.confidence,
            "fallback": self.fallback,
            "candidates": self.candidates,
            "confidence_threshold": self.confidence_threshold,
            "confidence_rule": self.confidence_rule,
            "overridden": self.overridden,
        }
        if self.snapshot is not None:
            d["snapshot"] = self.snapshot
        return _redact(d)


class AuditLogger:
    """Appends one JSONL record per think cycle to
    `<base_dir>/<agent>/<YYYY-MM-DD>.jsonl`.

    Rotation: size-based — if the current day's file would exceed
    `max_bytes` after a write, it's rolled to `<date>.jsonl.1` (bumping any
    existing `.1`, `.2`, ... up by one) before the write, so a single day
    under heavy load doesn't grow one unbounded file.

    Retention: on every rotation and once per process (first record of
    each new day file), files older than `retention_days` are deleted.
    """

    def __init__(self, agent_name: str, base_dir: str | None = None,
                 retention_days: int | None = None,
                 full_snapshot_every: int = DEFAULT_FULL_SNAPSHOT_EVERY,
                 max_bytes: int = DEFAULT_MAX_BYTES):
        self.agent_name = agent_name
        self.base_dir = base_dir if base_dir is not None else env_base_dir()
        self.retention_days = retention_days if retention_days is not None else env_retention_days()
        self.full_snapshot_every = max(1, full_snapshot_every)
        self.max_bytes = max_bytes
        self.agent_dir = os.path.join(self.base_dir, agent_name)
        self._last_cleaned_date = None
        # UM-50: optional callback(record_dict) run for every record, before
        # the file write — agent.http_api.AgentObserver.record_decision.
        self.on_record = None

    # ── paths ────────────────────────────────────────────────────────
    def _date_str(self, ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    def _current_path(self, ts: float) -> str:
        return os.path.join(self.agent_dir, f"{self._date_str(ts)}.jsonl")

    # ── writing ──────────────────────────────────────────────────────
    def record(self, cycle: int, snapshot: dict, tool_call: dict, valid: bool,
               result: dict, reflex: dict | None = None, goal: str | None = None,
               prompt_tokens: int | None = None, completion_tokens: int | None = None,
               model: str | None = None, latency_ms: float | None = None,
               brain: str | None = None, confidence: float | None = None,
               fallback: str | None = None, candidates: int | None = None,
               confidence_threshold: float | None = None,
               confidence_rule: str | None = None, overridden: str | None = None,
               ts: float | None = None) -> AuditRecord:
        ts = ts if ts is not None else time.time()
        snapshot = snapshot or {}
        compact = _compact_json(snapshot)
        snapshot_hash = hashlib.sha256(compact.encode("utf-8")).hexdigest()

        include_full = (cycle % self.full_snapshot_every == 0) or not valid or not result.get("ok", False)

        rec = AuditRecord(
            ts=ts, agent=self.agent_name, cycle=cycle, snapshot_hash=snapshot_hash,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            model=model, latency_ms=latency_ms,
            tool_call=tool_call or {"name": None, "args": {}},
            valid=valid, result=result or {"ok": False, "error": None},
            reflex=reflex or {}, goal=goal,
            brain=brain, confidence=confidence, fallback=fallback, candidates=candidates,
            confidence_threshold=confidence_threshold, confidence_rule=confidence_rule,
            overridden=overridden,
            snapshot=snapshot if include_full else None,
        )

        if self.on_record is not None:
            try:
                self.on_record(rec.to_dict())
            except Exception:  # an observer must never break auditing
                log.exception("audit on_record hook failed")

        self._maybe_clean_retention(ts)
        os.makedirs(self.agent_dir, exist_ok=True)
        line = json.dumps(rec.to_dict(), default=str) + "\n"
        path = self._current_path(ts)
        self._rotate_if_needed(path, len(line.encode("utf-8")))
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return rec

    def _rotate_if_needed(self, path: str, incoming_bytes: int):
        if not os.path.exists(path):
            return  # nothing written yet this day — nothing to rotate
        size = os.path.getsize(path)
        if size + incoming_bytes <= self.max_bytes:
            return
        # Bump .N -> .N+1 from highest to lowest, then move the live file to .1.
        existing = sorted(
            (p for p in glob.glob(path + ".*") if p.rsplit(".", 1)[-1].isdigit()),
            key=lambda p: int(p.rsplit(".", 1)[-1]),
            reverse=True,
        )
        for p in existing:
            n = int(p.rsplit(".", 1)[-1])
            os.replace(p, f"{path}.{n + 1}")
        os.replace(path, f"{path}.1")
        log.info("audit log rotated: %s (size %d bytes)", path, size)

    # ── retention ────────────────────────────────────────────────────
    def _maybe_clean_retention(self, ts: float):
        today = self._date_str(ts)
        if self._last_cleaned_date == today:
            return
        self._last_cleaned_date = today
        try:
            self.clean_retention(now=ts)
        except OSError as e:
            log.warning("audit retention cleanup failed: %s", e)

    def clean_retention(self, now: float | None = None) -> list[str]:
        """Delete audit files (any `.jsonl` or rotated `.jsonl.N`) whose day
        is older than `retention_days`. Returns the list of deleted paths."""
        now = now if now is not None else time.time()
        cutoff = now - self.retention_days * 86400
        deleted = []
        if not os.path.isdir(self.agent_dir):
            return deleted
        for name in os.listdir(self.agent_dir):
            m = _DAY_FILE_RE.match(name)
            if not m:
                continue
            try:
                day_ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))
            except ValueError:
                continue
            if day_ts < cutoff:
                path = os.path.join(self.agent_dir, name)
                try:
                    os.remove(path)
                    deleted.append(path)
                except OSError:
                    pass
        if deleted:
            log.info("audit retention: deleted %d file(s) older than %d days",
                      len(deleted), self.retention_days)
        return deleted
