"""Versioned on-disk shape of one decision-audit record (#277).

Every line `agent.audit` writes is `AuditRecord.to_json()`. Every reader
(`agent.tools.replay`, `agent.metrics`) loads lines with `AuditRecord.from_json()`,
which upgrades older lines: a line with no `schema` key is version 0 and takes the
field defaults below. Keys this version does not know are kept in `extra` and
written back, so a newer writer's fields survive a round trip through this code.

When a field's meaning or shape changes, bump SCHEMA_VERSION and add a golden line
under agent/tests/fixtures/audit/. A new field needs a field here, or the writer
contract test in agent/tests/test_audit_schema.py fails.
"""

from dataclasses import dataclass, field, fields

from .model import Snapshot

SCHEMA_VERSION = 1


@dataclass
class AuditRecord:
    ts: float | None = None                  # unix epoch seconds
    agent: str | None = None                 # AGENT_NAME
    cycle: int | None = None                 # think-cycle counter, monotonically increasing
    snapshot_hash: str | None = None         # sha256 of the compact-JSON snapshot
    prompt_tokens: int | None = None         # only providers that report usage (Jev: usage.input_tokens)
    completion_tokens: int | None = None     # Jev: usage.output_tokens
    model: str | None = None                 # LLM model id, or None when no call was made
    latency_ms: float | None = None          # LLM/Jev call latency
    tool_call: dict = field(default_factory=lambda: {"name": None, "args": {}})  # {"name": str|None, "args": dict}
    valid: bool = False                      # tool call resolved to a registered action with all required params
                                             # (mirrors ThinkResult.ok, before in-game execution)
    result: dict = field(default_factory=lambda: {"ok": False, "error": None})   # {"ok": bool, "error": str|None}
    reflex: dict = field(default_factory=dict)  # reflex state (e.g. follow enabled/leader), {} if none active
    goal: str | None = None                  # the agent's current persona/goal, if any
    brain: str | None = None                 # "jev", "llm" or None (UM-101): the brain that decided this cycle,
                                             # or the last one tried when none did
    confidence: float | None = None          # Jev's confidence in its choice (Jev only)
    fallback: str | None = None              # why Jev did not decide (call failed, cooling down, or budget spent),
                                             # whether the cycle was then skipped or handed to the LLM
    substituted: bool = False                # true only when the LLM decided in Jev's place (AGENT_BRAIN_FALLBACK=llm)
    candidates: int | None = None            # how many candidates Jev was offered
    confidence_threshold: float | None = None  # JEV_MIN_CONFIDENCE applied (Jev only)
    confidence_rule: str | None = None       # "acted", "low_confidence_safe_fallback" (the safe candidate replaced
                                             # Jev's choice) or "confidence_unknown" (Jev gave none); None off Jev
    overridden: str | None = None            # candidate id Jev chose before the safe substitution
    usage: dict = field(default_factory=dict)  # bounded Jev token/cost fields only; no response payload
    jev_status: str | None = None            # Jev's status for the cycle, e.g. "budget_exhausted"
    history_notes: list | None = None        # candidates dropped/demoted by recent action history
                                             # ({"id", "effect", "reason"})
    # Full snapshot: on every FULL_SNAPSHOT_EVERY'th cycle or when the cycle was invalid/failed; otherwise
    # omitted (hash + this cycle's record are enough to tell "did perception change").
    snapshot: Snapshot | dict | None = None
    # Keys from a newer writer that this version does not know; written back by to_json().
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        d: dict = {
            "schema": SCHEMA_VERSION,
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
            "substituted": self.substituted,
            "candidates": self.candidates,
            "confidence_threshold": self.confidence_threshold,
            "confidence_rule": self.confidence_rule,
            "overridden": self.overridden,
            "usage": self.usage,
            "jev_status": self.jev_status,
            "history_notes": self.history_notes,
        }
        if self.snapshot is not None:
            d["snapshot"] = self.snapshot
        d.update(self.extra)
        return d

    def to_dict(self) -> dict:
        """Same as to_json(); kept for callers written before the schema existed."""
        return self.to_json()

    @classmethod
    def from_json(cls, d: dict) -> "AuditRecord":
        """Load one line of any schema version. Missing keys take the field defaults
        (that is the upgrade); unknown keys go to `extra`. The stored `schema` value is
        not checked: to_json() always writes SCHEMA_VERSION."""
        names = {f.name for f in fields(cls)} - {"extra"}
        known = {k: v for k, v in d.items() if k in names}
        extra = {k: v for k, v in d.items() if k not in names and k != "schema"}
        return cls(**known, extra=extra)
