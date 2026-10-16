#!/usr/bin/env python3
"""Think step (UM-44): perception snapshot -> one brain decision (UM-101:
Jev over generated candidates, or the LLM over the action catalog; see
agent/brain.py) -> one validated action executed, per think cycle.

Wired into agent/runloop.py::run_loop between the perceive and act
comments that Phase 3 (docs/ROADMAP.md) left as placeholders. Kept in its
own module (not inlined in runloop.py) so it can be unit-tested against a
fake LLM client without booting a real WoWSession.
"""

import logging
import time

from . import actions as ac
from . import ports
from . import spells as sp
from . import trade as tr
from . import update_fields as uf
from .brain import Brain, BrainError, Decision
from .action_specs import specs_by_name
from .handles import UnknownHandle
from .actions.base import send
from .loopguard import (HISTORY_LEN, LOOP_GUARD_COOLDOWN_S, LOOP_GUARD_REPEATS, ThinkState,  # noqa: F401 (re-exported)
                        _DETAIL_MAX_CHARS, progress_fingerprint)
from .model import KnownSpell, Me, Snapshot
from .unmet import UnmetIntents

log = logging.getLogger("agent.think")

TRADE_IDLE_TIMEOUT_S = 60.0  # UM-59: cancel a stalled trade instead of blocking the agent forever


def _maybe_cancel_idle_trade(session, world):
    """UM-59 safety rail: an open/pending trade nobody has touched in a
    while (an offer change, accept, etc. all bump `last_activity_at`) gets
    cancelled automatically — code decides this, not the LLM, matching the
    issue's "timeout and cancel an idle trade after ~60s". A side effect
    checked once per think cycle rather than its own reflex thread: the
    ~60s target doesn't need sub-second responsiveness, unlike follow/rest."""
    trade = world.get_trade()
    if trade is None:
        return
    if time.monotonic() - trade["last_activity_at"] < TRADE_IDLE_TIMEOUT_S:
        return
    log.info("trade with %#x idle for over %.0fs — cancelling", trade["partner_guid"], TRADE_IDLE_TIMEOUT_S)
    send(session, tr.CMSG_CANCEL_TRADE, tr.build_cancel_trade())
    # Bump last_activity_at so we don't re-send the cancel on every think
    # cycle while waiting for the server's TRADE_STATUS_TRADE_CANCELED to
    # arrive and clear world.trade (found in post-merge review, UM-59).
    trade["last_activity_at"] = time.monotonic()


def _self_status(session: ports.PlayerView, world, snapshot: Snapshot) -> None:
    """Add own level/health/power/xp (`me`) and castable spells (`spells`) to
    `snapshot`: the goal prompt needs them ("mobs of your level", "rest when
    low", "cast_spell from your spellbook") and WorldState.snapshot() doesn't
    carry them."""
    me = world.get_my_object()
    if me is not None:
        mine: Me = {"level": me.level, "class_id": session.class_}
        if me.health is not None and me.max_health:
            mine["health"] = f"{me.health}/{me.max_health}"
        if me.unit_flags and (me.unit_flags & uf.UNIT_FLAG_IN_COMBAT):
            mine["in_combat"] = True  # only ever true: absent means out of combat (gh-544)
        for name, cur in (me.power or {}).items():
            top = (me.max_power or {}).get(name)
            # `name` is one of agent.update_fields.POWER_NAMES, all declared on Me.
            mine[name] = f"{cur}/{top}" if top else cur  # type: ignore[literal-required]
        raw = me.raw_fields or {}
        if uf.PLAYER_XP in raw:
            mine["xp"] = raw[uf.PLAYER_XP]
        if uf.PLAYER_NEXT_LEVEL_XP in raw:
            mine["next_level_xp"] = raw[uf.PLAYER_NEXT_LEVEL_XP]
        snapshot["me"] = mine
    known = session.spellbook or ()
    # Only spells agent.spells has metadata for: the raw spellbook is
    # mostly passives (languages, weapon skills) that would waste tokens.
    spell_list: list[KnownSpell] = [
        {"id": i.spell_id, "name": i.name}
        for i in (sp.get_spell_info(s) for s in sorted(known)) if i is not None]
    if spell_list:
        snapshot["spells"] = spell_list


class ThinkResult:
    """Outcome of one think_and_act() call, for logging/tests. Never raises
    for expected failure modes (LLM error, unknown action, bad params,
    action rejected by check()/execute()) — those are recorded here instead,
    so one bad cycle never crashes the agent loop."""

    def __init__(self, ok: bool, action_name: str | None = None,
                 params: dict | None = None, error: str | None = None,
                 detail: dict | None = None):
        self.ok = ok
        self.action_name = action_name
        self.params = params or {}
        self.error = error
        self.detail = detail or {}

    def __repr__(self):
        if self.ok:
            return f"<ThinkResult ok action={self.action_name} params={self.params}>"
        return f"<ThinkResult error={self.error!r} action={self.action_name}>"


class Cycle:
    """State one think cycle carries through the stages. A stage that ends the
    cycle early calls fail(); record() then writes the audit line and history."""

    def __init__(self, session, world, brain, persona, my_position, registry,
                 audit_logger, cycle, reflex_state, state, unmet=None):
        self.session, self.world, self.brain, self.persona = session, world, brain, persona
        self.my_position, self.registry, self.audit_logger = my_position, registry, audit_logger
        self.cycle, self.reflex_state, self.state = cycle, reflex_state, state
        self.unmet = unmet      # #319: UnmetIntents, or None (no unmet-intent records)
        self.snapshot: Snapshot = {}  # type: ignore[typeddict-item]  # filled by perceive
        self.fingerprint = ""
        self.decision = Decision()
        self.action = None
        self.action_name: str | None = None
        self.params: dict | None = None
        self.run_params: dict = {}
        self.valid = False
        self.ok = False
        self.error: str | None = None
        self.detail: dict | None = None
        self.remember = True    # BrainError cycles carry nothing the model could learn from
        self.done = False

    def fail(self, error, *, valid=False, detail=None):
        self.error, self.valid, self.detail, self.done = error, valid, detail, True


# Pre-decision hooks, run in order before the snapshot is taken. A safety rail
# is one function (session, world) added here; think_and_act does not change.
GUARDS = [_maybe_cancel_idle_trade]


def guards(c: Cycle) -> None:
    for guard in GUARDS:
        guard(c.session, c.world)


def perceive(c: Cycle) -> None:
    # corpse_position (UM-43) is session-scoped (MSG_CORPSE_QUERY), not part
    # of WorldState; getattr() with a default keeps test doubles working.
    c.snapshot = c.world.snapshot(my_position=c.my_position,
                                  corpse_position=getattr(c.session, "corpse_position", None),
                                  pending_invite=getattr(c.session, "pending_invite", None),
                                  group=getattr(c.session, "group", None),
                                  chat_inbox=getattr(c.session, "chat_inbox", None))
    _self_status(c.session, c.world, c.snapshot)
    c.fingerprint = progress_fingerprint(c.snapshot)
    if c.state is not None:
        c.state.observe(c.fingerprint)


def decide(c: Cycle) -> None:
    state = c.state
    try:
        c.decision = c.brain.decide(
            c.snapshot, persona=c.persona,
            history=state.for_prompt() if state is not None else None,
            my_guid=getattr(c.world, "my_guid", None) or None,
            reflex_state=c.reflex_state,
            blocked=state.repeat_blocked if state is not None else None,
            handles=getattr(c.world, "handles", None))
    except BrainError as e:
        c.decision, c.remember = e.decision, False
        log.warning("%s", e)
        return c.fail(str(e))
    c.action_name, c.params = c.decision.action, c.decision.params


def validate(c: Cycle) -> None:
    name, params = c.action_name, c.params
    c.action = c.registry.get(name) if name is not None else None
    if c.action is None or name is None:
        log.warning("model chose unknown action %r (params=%r)", name, params)
        if c.unmet is not None:
            c.unmet.record("unknown_action", name, params, c.snapshot)
        return c.fail(f"unknown action: {name!r}")
    spec = specs_by_name.get(name)  # the spec is the source of `required`; a test-double action has none
    required = spec.required if spec is not None else c.action.required
    missing = [p for p in required if p not in params]  # type: ignore[operator]  # None params raise TypeError, as on main
    if missing:
        log.warning("action %s missing required params %r (got %r)", name, missing, params)
        return c.fail(f"missing required params: {missing}")
    # UM-89: the model sees and sends short handles ("u3"), never raw GUIDs;
    # map them back here. `params` (handles) stays what the audit log records.
    handles = getattr(c.world, "handles", None)
    c.run_params = params  # type: ignore[assignment]
    if handles is not None:
        try:
            c.run_params = handles.resolve_params(params)
        except UnknownHandle as e:
            log.warning("action %s got an unknown handle (params=%r): %s", name, params, e)
            return c.fail(str(e))
    if c.state is not None and c.state.repeat_blocked(name, params):
        log.warning("loop guard blocked %s %r", name, params)
        c.remember = False  # #521: the guard's refusal is not a new failure of the action; the audit line keeps it
        return c.fail(f"loop guard: {name} with these exact args already failed or changed "
                      f"nothing {LOOP_GUARD_REPEATS} times in a row; not executed. Try something different.")


def execute(c: Cycle) -> None:
    assert c.action is not None  # validate() ended the cycle otherwise
    try:
        result = c.action.run(c.session, c.world, **c.run_params)
    except TypeError as e:
        # Extra kwargs the model hallucinated, or a param of the wrong shape.
        log.warning("action %s rejected params %r: %s", c.action_name, c.params, e)
        return c.fail(f"bad params: {e}", valid=True)
    c.valid, c.detail = True, result.detail
    if not result.ok:
        log.info("action %s failed validation/execution: %s", c.action_name, result.error)
        return c.fail(result.error, valid=True, detail=result.detail)
    log.info("action %s executed: %s", c.action_name, result.detail)
    c.ok = True


def record(c: Cycle) -> ThinkResult:
    d = c.decision
    if c.unmet is not None and d.candidates == 1:   # the generator offered idle only (#319)
        c.unmet.record("no_candidates", None, None, c.snapshot)
    if c.audit_logger is not None:
        try:
            drain = getattr(c.session, "drain_activity", None)  # ADR 0020; a test double may lack it
            events = drain() if drain else []  # the activity events since the last decision
            c.audit_logger.record(
                cycle=c.cycle, snapshot=c.snapshot,
                tool_call={"name": c.action_name, "args": c.params or {}},
                valid=c.valid, result={"ok": c.ok, "error": c.error},
                reflex=c.reflex_state or {}, goal=c.persona or None,
                prompt_tokens=d.prompt_tokens, completion_tokens=d.completion_tokens,
                model=d.model, latency_ms=d.latency_ms, brain=d.brain,
                confidence=d.confidence, fallback=d.fallback, substituted=d.substituted,
                candidates=d.candidates, confidence_threshold=d.confidence_threshold,
                confidence_rule=d.confidence_rule, overridden=d.overridden, usage=d.usage,
                jev_status=d.jev_status, history_notes=d.history_notes, prompt_id=d.prompt_id,
                events=events)
        except OSError as e:  # never let audit I/O crash a think cycle
            log.warning("audit log write failed: %s", e)
    if c.state is not None and c.remember:
        c.state.record(c.action_name, c.params, c.ok, c.fingerprint, error=c.error,
                       detail=c.detail if c.ok else None)
    return ThinkResult(ok=c.ok, action_name=c.action_name, params=c.params, error=c.error, detail=c.detail)


STAGES = (guards, perceive, decide, validate, execute)


def think_and_act(session, world, brain, persona: str = "",
                   my_position=None, registry: dict | None = None,
                   audit_logger=None, cycle: int = 0,
                   reflex_state: dict | None = None,
                   state: ThinkState | None = None,
                   unmet: UnmetIntents | None = None) -> ThinkResult:
    """One think cycle: guards -> perceive -> decide -> validate -> execute -> record.

    Decide asks the brain (agent.brain.Brain, UM-101; a bare LLM client is
    wrapped as an LLM-only Brain) for exactly one action. Validate checks it
    against the registry, resolves handles and applies the loop guard
    (`state`, UM-90). Execute runs it. Record appends the audit line (if
    `audit_logger`, UM-51) and the history entry, whatever the outcome.
    `unmet` (#319) gets an unmet-intent record for an unknown action or an idle-only candidate list.
    Expected failures yield a non-ok ThinkResult, never an exception.
    """
    if not isinstance(brain, Brain):
        brain = Brain(llm=brain)
    c = Cycle(session, world, brain, persona, my_position,
              registry if registry is not None else ac.REGISTRY,
              audit_logger, cycle, reflex_state, state, unmet)
    for stage in STAGES:
        stage(c)
        if c.done:
            break
    return record(c)
