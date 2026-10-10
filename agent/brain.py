#!/usr/bin/env python3
"""Brain seam (UM-101, ADR 0001): one place that decides which model picks
this think cycle's action.

Two peer brains sit behind `Brain.decide()`; AGENT_BRAIN=llm|jev (default
llm) picks one per agent (#161):

- **Jev** (agent/jev.py, UM-99): the candidate generator (agent/candidates.py,
  UM-97) turns the snapshot into concrete `(action, params)` options and Jev
  picks one. Used when AGENT_BRAIN=jev.
- **LLM** (agent/llm.py, UM-44): the action catalog goes out as tools and the
  model fills in the arguments. Used when AGENT_BRAIN=llm; Jev is then never
  constructed.

Failure policy (Jev brain): no silent substitution.

- Any JevError (network error, any HTTP status, malformed answer, a choice we
  did not offer) is a failed cycle, audited as brain=jev with the reason and
  no LLM call. Only with AGENT_BRAIN_FALLBACK=llm does the LLM decide in
  Jev's place, and then the Decision says `substituted=True`.
- 401/402/403 (bad key, out of credits) also start a JEV_AUTH_COOLDOWN_S
  cooldown, and 429 (rate limited) a JEV_RATE_LIMIT_COOLDOWN_S one. During a
  cooldown Jev is not called at all: the cycle is skipped (or substituted,
  when the fallback is on). Retrying a dead key or an empty account every
  3-5 s only burns requests. Other failures have no cooldown.

`decide()` returns a Decision that carries everything the audit log needs
(UM-51): which brain decided, Jev's confidence, model, token usage, latency,
and, when Jev failed, why (and `substituted` if the LLM stood in).
"""

import logging
import os
from collections import deque
import time
from dataclasses import dataclass, field

from . import actions as ac
from . import candidates as cand
from .jev import JevClient, JevError
from .llm import LLMClient, LLMError
from .model import ActionRecord, Candidate, Snapshot

log = logging.getLogger("agent.brain")

BRAIN_JEV = "jev"
BRAIN_LLM = "llm"

# Confidence policy (GH-165): the one threshold and the one rule. Applied in
# `Brain.decide()` only, to Jev's answer, and never calls the LLM. Default 0.0
# = the rule never fires until a threshold is chosen from measured data
# (JEV_MIN_CONFIDENCE, .env.example).
DEFAULT_JEV_MIN_CONFIDENCE = 0.0
SAFE_ACTION = "idle"
RULE_ACTED = "acted"
RULE_LOW_CONFIDENCE = "low_confidence_safe_fallback"
RULE_UNKNOWN = "confidence_unknown"

JEV_AUTH_COOLDOWN_S = 300.0       # 401/402/403: key or credits, will not fix itself in seconds
JEV_RATE_LIMIT_COOLDOWN_S = 30.0  # 429
_COOLDOWNS = {401: JEV_AUTH_COOLDOWN_S, 402: JEV_AUTH_COOLDOWN_S,
              403: JEV_AUTH_COOLDOWN_S, 429: JEV_RATE_LIMIT_COOLDOWN_S}

# #213: Jev token budget. Rolling window, per agent, input+output tokens.
JEV_BUDGET_WINDOW_S = 3600.0
JEV_STATUS_BUDGET = "budget_exhausted"

# Actions the LLM never sees as tools. `idle` (UM-97) exists so a choice-only
# brain always has a valid option; offered to a free tool-calling model it is
# an escape hatch that turns "could not decide" into a successful no-op,
# which hides exactly the failures UM-44's >=90% valid-call bar measures.
# Leaving it out keeps the LLM path what it was before UM-97.
LLM_EXCLUDED_ACTIONS = frozenset({"idle"})


class BrainError(Exception):
    """No brain produced an action this cycle. The message is ready for the
    audit log / ThinkResult; `decision` carries the brain metadata gathered
    before the failure (brain name, fallback reason, usage)."""

    def __init__(self, message: str, decision: "Decision"):
        super().__init__(message)
        self.decision = decision


@dataclass
class Decision:
    action: str | None = None
    params: dict = field(default_factory=dict)
    brain: str | None = None            # BRAIN_JEV / BRAIN_LLM: who decided (or was last tried)
    model: str | None = None
    confidence: float | None = None     # Jev only
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: float | None = None
    fallback: str | None = None         # why Jev did not decide, when it was the brain
    substituted: bool = False           # the LLM decided in place of a failed Jev (explicit opt-in)
    candidates: int | None = None       # how many candidates Jev was offered
    confidence_threshold: float | None = None  # threshold applied (Jev only)
    confidence_rule: str | None = None  # RULE_ACTED / RULE_LOW_CONFIDENCE / RULE_UNKNOWN
    overridden: str | None = None       # candidate id Jev chose before the safe substitution
    usage: dict = field(default_factory=dict)
    jev_status: str | None = None
    history_notes: list | None = None   # candidates dropped/demoted because of history, with why


def llm_catalog() -> list[dict]:
    """The action catalog as the LLM sees it: every registered action except
    LLM_EXCLUDED_ACTIONS."""
    return [s for s in ac.catalog() if s["name"] not in LLM_EXCLUDED_ACTIONS]


def _jev_error_status(status) -> str:
    """Bounded audit class for a failed Jev call."""
    if isinstance(status, int):
        if 400 <= status < 500:
            return "http_4xx"
        if 500 <= status < 600:
            return "http_5xx"
    return "error"


class Brain:
    """Routes one decision to Jev or the LLM. With only `llm` it is the llm
    brain; with `jev` it is the jev brain, and an `llm` alongside it is the
    explicit fallback (AGENT_BRAIN_FALLBACK=llm). `from_config` is what
    decides which clients exist, so a Brain never substitutes by accident."""

    def __init__(self, jev=None, llm=None, clock=time.monotonic,
                 min_confidence: float = DEFAULT_JEV_MIN_CONFIDENCE,
                 max_tokens_per_hour: int = 0):
        self.jev = jev
        self.llm = llm
        self.min_confidence = min_confidence
        self._clock = clock
        self._jev_cooldown_until = 0.0
        self._jev_cooldown_reason: str | None = None
        self.max_tokens_per_hour = max(0, int(max_tokens_per_hour or 0))
        self._jev_spend: deque = deque()  # (clock time, tokens) per billed Jev call

    @classmethod
    def from_config(cls, cfg) -> "Brain | None":
        jev = llm = None
        if cfg.agent_brain == BRAIN_JEV:
            if not cfg.jev_enabled:
                log.warning("AGENT_BRAIN=jev but no JEV_BASE_URL/JEV_API_KEY set")
                return None
            jev = JevClient(cfg.jev_base_url, model=cfg.jev_model, api_key=cfg.jev_api_key,
                             path=cfg.jev_path)
            want_llm = cfg.agent_brain_fallback == BRAIN_LLM
        elif cfg.agent_brain == BRAIN_LLM:
            want_llm = True
        else:
            log.warning("unknown AGENT_BRAIN %r (want llm or jev)", cfg.agent_brain)
            return None
        if want_llm and cfg.llm_base_url and cfg.llm_model:
            llm = LLMClient(cfg.llm_base_url, cfg.llm_model, api_key=cfg.llm_api_key)
        if jev is None and llm is None:
            return None
        return cls(jev=jev, llm=llm, min_confidence=cfg.jev_min_confidence,
                   max_tokens_per_hour=getattr(cfg, "max_tokens_per_hour", 0))

    def describe(self) -> str:
        parts = []
        if self.jev is not None:
            parts.append(f"jev {self.jev.model} @ {self.jev.base_url}")
        if self.llm is not None:
            parts.append(f"llm {self.llm.model} @ {self.llm.base_url}"
                         + (" (explicit fallback)" if self.jev is not None else ""))
        return ", ".join(parts) or "(none)"

    @property
    def model(self) -> str | None:
        """The primary brain's model, for the observability API's header."""
        client = self.jev or self.llm
        return getattr(client, "model", None)

    def decide(self, snapshot: Snapshot, *, persona: str = "",
               history: list[ActionRecord] | None = None,
               my_guid: int | None = None, reflex_state: dict | None = None,
               blocked=None, handles=None) -> Decision:
        """Pick one `(action, params)` for this cycle. Raises BrainError when
        no configured brain produced one. `blocked(action, params) -> bool`
        (ThinkState.repeat_blocked) drops candidates the loop guard would
        refuse anyway, so Jev is not offered them (`idle` is always kept).
        `handles` (world.handles) lets the candidate generator resolve the
        snapshot's handle-string GUIDs (UM-89) for its unit comparisons."""
        fallback = None
        failed_jev = None
        budget_skipped = False
        if self.jev is not None:
            d = Decision(brain=BRAIN_JEV, model=getattr(self.jev, "model", None))
            fallback = self._jev_skip_reason()
            if fallback is None:
                fallback = self._jev_budget_reason()
                if fallback is not None:
                    d.jev_status = JEV_STATUS_BUDGET
                    budget_skipped = True
                    log.warning("%s", fallback)
            if fallback is None:
                try:
                    notes: list[dict] = []
                    options = cand.generate(snapshot, my_guid=my_guid, reflex_state=reflex_state,
                                            handles=handles, history=history, notes=notes)
                    d.history_notes = notes or None
                    if blocked is not None:
                        options = [c for c in options
                                   if c["action"] == "idle" or not blocked(c["action"], c["params"])]
                    d.candidates = len(options)
                    d.action, d.params = self.jev.choose_action(snapshot, options, persona=persona,
                                                                history=history)
                    self._fill_jev(d)
                    self._record_jev_spend(d)
                    d.jev_status = ("skipped_single_candidate"
                                    if getattr(self.jev, "skipped_single_candidate", False)
                                    else "success")
                    self._apply_confidence_policy(d, options)
                    return d
                except JevError as e:
                    self._fill_jev(d)
                    self._record_jev_spend(d)
                    d.jev_status = _jev_error_status(getattr(e, "status", None))
                    fallback = f"jev call failed: {e}"
                    self._maybe_cool_down(e)
                    log.warning("%s%s", fallback, " — substituting the llm" if self.llm else "")
                    failed_jev = d
            if self.llm is None:
                d.fallback = fallback
                raise BrainError(fallback, d)

        d = Decision(brain=BRAIN_LLM, model=getattr(self.llm, "model", None), fallback=fallback,
                     substituted=self.jev is not None)
        if failed_jev is not None:
            # The failed Jev call still happened: keep its status and billed usage.
            d.jev_status, d.usage = failed_jev.jev_status, failed_jev.usage
        elif budget_skipped:
            d.jev_status = JEV_STATUS_BUDGET
        try:
            if history is not None:
                d.action, d.params = self.llm.choose_action(snapshot, llm_catalog(), persona=persona,
                                                            history=history)
            else:
                d.action, d.params = self.llm.choose_action(snapshot, llm_catalog(), persona=persona)
        except LLMError as e:
            self._fill_llm(d)
            message = f"llm call failed: {e}"
            if fallback:
                message = f"{fallback}; {message}"
            raise BrainError(message, d) from e
        self._fill_llm(d)
        return d

    # ── internals ──────────────────────────────────────────────────
    def _jev_skip_reason(self) -> str | None:
        remaining = self._jev_cooldown_until - self._clock()
        if remaining <= 0:
            return None
        return f"jev cooling down {remaining:.0f}s more ({self._jev_cooldown_reason})"

    def _jev_budget_reason(self) -> str | None:
        """None while the rolling-hour token budget has room (or is off 0);
        otherwise why Jev is not called. Skipping fails the cycle (or hands it
        to the explicit llm fallback) and is audited as budget_exhausted, so
        it is distinguishable from an idle choice and from a Jev error."""
        if not self.max_tokens_per_hour:
            return None
        cutoff = self._clock() - JEV_BUDGET_WINDOW_S
        while self._jev_spend and self._jev_spend[0][0] <= cutoff:
            self._jev_spend.popleft()
        used = sum(t for _, t in self._jev_spend)
        if used < self.max_tokens_per_hour:
            return None
        return (f"jev token budget exhausted: {used} of {self.max_tokens_per_hour} "
                f"tokens used in the last hour (AGENT_MAX_TOKENS_PER_HOUR)")

    def restore_jev_spend(self, audit_dir: str, agent: str, now: float | None = None):
        """Reload the last hour's Jev spend from this agent's audit log, so a
        restart does not reset the budget. The audit `usage` field holds Jev
        usage only (LLM tokens go to prompt/completion_tokens)."""
        if not self.max_tokens_per_hour:
            return
        from .metrics import find_audit_files, iter_records
        now = time.time() if now is None else now
        cutoff, mono, spend = now - JEV_BUDGET_WINDOW_S, self._clock(), []
        for path in find_audit_files(audit_dir, agent):
            if os.path.getmtime(path) <= cutoff:
                continue  # last written before the window: nothing in it counts
            for rec in iter_records(path):
                usage, ts = rec.get("usage") or {}, rec.get("ts") or 0
                tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens"))
                if ts > cutoff and tokens > 0:
                    spend.append((mono - (now - ts), tokens))
        self._jev_spend.extendleft(sorted(spend, reverse=True))

    def _record_jev_spend(self, d: Decision):
        tokens = sum(int(d.usage.get(k, 0)) for k in ("input_tokens", "output_tokens"))
        if tokens > 0:
            self._jev_spend.append((self._clock(), tokens))

    def _maybe_cool_down(self, e: JevError):
        seconds = _COOLDOWNS.get(getattr(e, "status", None) or 0)
        if seconds:
            self._jev_cooldown_until = self._clock() + seconds
            self._jev_cooldown_reason = f"HTTP {e.status}"
            log.warning("jev returned HTTP %d — not calling it for %.0fs", e.status, seconds)

    def _apply_confidence_policy(self, d: Decision, options: list[Candidate]):
        """The confidence rule. `confidence >= threshold` acts as chosen;
        below it the generator's safe candidate (`idle`, always offered)
        replaces the choice; no reported confidence is its own audited
        state and acts as chosen (not zero, not high). Never calls a model."""
        d.confidence_threshold = self.min_confidence
        if d.confidence is None:
            d.confidence_rule = RULE_UNKNOWN
            return
        if d.confidence >= self.min_confidence or d.action == SAFE_ACTION:
            d.confidence_rule = RULE_ACTED
            return
        safe = next((c for c in options if c["action"] == SAFE_ACTION), None)
        if safe is None:  # generator contract broken; do not invent one
            d.confidence_rule = RULE_ACTED
            return
        d.overridden = next((c["id"] for c in options
                             if c["action"] == d.action and c["params"] == d.params), d.action)
        d.action, d.params = safe["action"], dict(safe["params"])
        d.confidence_rule = RULE_LOW_CONFIDENCE

    def _fill_jev(self, d: Decision):
        usage = getattr(self.jev, "last_usage", None) or {}
        d.usage = {k: usage[k] for k in ("input_tokens", "output_tokens", "cost")
                   if isinstance(usage.get(k), (int, float))}
        d.prompt_tokens = usage.get("input_tokens")
        d.completion_tokens = usage.get("output_tokens")
        d.latency_ms = getattr(self.jev, "last_latency_ms", None)
        d.confidence = getattr(self.jev, "last_confidence", None)

    def _fill_llm(self, d: Decision):
        usage = getattr(self.llm, "last_usage", None) or {}
        d.prompt_tokens = usage.get("prompt_tokens")
        d.completion_tokens = usage.get("completion_tokens")
        d.latency_ms = getattr(self.llm, "last_latency_ms", None)
        # UM-94: the model that actually answered (None on a failed call);
        # clients without `last_model` keep the configured `model`.
        if hasattr(self.llm, "last_model"):
            d.model = self.llm.last_model
