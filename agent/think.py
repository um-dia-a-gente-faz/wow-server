#!/usr/bin/env python3
"""Think step (UM-44): perception snapshot -> one brain decision (UM-101:
Jev over generated candidates, or the LLM over the action catalog; see
agent/brain.py) -> one validated action executed, per think cycle.

Wired into agent/__main__.py::_run_loop between the perceive and act
comments that Phase 3 (docs/ROADMAP.md) left as placeholders. Kept in its
own module (not inlined in __main__.py) so it can be unit-tested against a
fake LLM client without booting a real WoWSession.
"""

import hashlib
import json
import logging
import time
from collections import deque

from . import actions as ac
from . import spells as sp
from . import trade as tr
from . import update_fields as uf
from .brain import Brain, BrainError, Decision
from .handles import UnknownHandle
from .llm import LLMError

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
    session._send_packet(tr.CMSG_CANCEL_TRADE, tr.build_cancel_trade())
    # Bump last_activity_at so we don't re-send the cancel on every think
    # cycle while waiting for the server's TRADE_STATUS_TRADE_CANCELED to
    # arrive and clear world.trade (found in post-merge review, UM-59).
    trade["last_activity_at"] = time.monotonic()


HISTORY_LEN = 8          # UM-90: recent cycles shown to the model
LOOP_GUARD_REPEATS = 3   # UM-90: identical failed/no-op calls before the guard blocks the next one
_DETAIL_MAX_CHARS = 160  # keep each history line short; the prompt is token-budgeted

# Snapshot keys whose change means an action did something. Deliberately
# leaves out things that drift on their own (own health/mana regen, chat,
# other units' positions) so "changed" means progress, not noise.
_PROGRESS_KEYS = ("position", "is_dead", "is_ghost", "window", "trade", "mailbox",
                  "equipment", "inventory", "quest_log", "pending_invite")


def _args_key(params: dict) -> str:
    return json.dumps(params or {}, sort_keys=True, default=str)


def progress_fingerprint(snapshot: dict) -> str:
    """Digest of the parts of a snapshot an action can change (UM-90's
    "returned no state change" test): the _PROGRESS_KEYS (position rounded
    to 1 yd so float jitter doesn't count), own level/xp, and the health of
    units in combat (so a fight in progress counts as a change)."""
    view = {k: snapshot.get(k) for k in _PROGRESS_KEYS}
    pos = snapshot.get("position")
    if isinstance(pos, dict):
        view["position"] = {k: round(v) if isinstance(v, float) else v for k, v in pos.items()}
    me = snapshot.get("me") or {}
    view["me"] = {"level": me.get("level"), "xp": me.get("xp")}
    view["fights"] = sorted(
        (str(u.get("guid")), u.get("health_pct"))
        for u in snapshot.get("nearby_units") or [] if u.get("in_combat"))
    raw = json.dumps(view, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _short(value) -> str | None:
    if value is None or value == {} or value == "":
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str, separators=(",", ":"))
    return text if len(text) <= _DETAIL_MAX_CHARS else text[:_DETAIL_MAX_CHARS - 3] + "..."


class ThinkState:
    """Think-loop memory across cycles (UM-90), owned by the main loop and
    passed into every think_and_act() call. Holds the last HISTORY_LEN
    executed-or-rejected actions, args exactly as the model sent them.
    LLM-failed cycles (no tool call) are not recorded: they carry nothing
    the model could learn from. Kept out of WorldState on purpose: this is
    the agent's own record, not protocol state."""

    def __init__(self, maxlen: int = HISTORY_LEN):
        self.history: deque = deque(maxlen=maxlen)

    def observe(self, fingerprint: str) -> None:
        """Called once per cycle with the fresh snapshot's fingerprint:
        settles whether the previous action changed anything."""
        if self.history and self.history[-1]["changed"] is None:
            last = self.history[-1]
            last["changed"] = fingerprint != last["fingerprint"]

    def record(self, action: str, args: dict, ok: bool, fingerprint: str,
               error: str | None = None, detail=None) -> None:
        self.history.append({"action": action, "args": dict(args or {}), "ok": ok,
                             "error": error, "detail": detail,
                             "fingerprint": fingerprint, "changed": None})

    def repeat_blocked(self, action: str, args: dict, n: int = LOOP_GUARD_REPEATS) -> bool:
        """True when each of the last `n` recorded cycles was this exact
        action with these exact args and either failed or changed nothing."""
        if len(self.history) < n:
            return False
        key = _args_key(args)
        for h in list(self.history)[-n:]:
            if h["action"] != action or _args_key(h["args"]) != key:
                return False
            if h["ok"] and h["changed"] is not False:
                return False
        return True

    def for_prompt(self) -> list[dict]:
        """Compact oldest-first view for agent.llm.build_messages."""
        out = []
        for h in self.history:
            e = {"action": h["action"], "args": h["args"], "ok": h["ok"]}
            if h["error"]:
                e["error"] = _short(h["error"])
            elif (d := _short(h["detail"])) is not None:
                e["result"] = d
            if h["changed"] is False:
                e["changed"] = False
            out.append(e)
        return out


def _self_status(session, world) -> dict:
    """Own level/health/power/xp and castable spells, which the goal prompt
    needs ("mobs of your level", "rest when low", "cast_spell from your
    spellbook") and WorldState.snapshot() doesn't carry."""
    out = {}
    me = world.get_my_object()
    if me is not None:
        mine = {"level": me.level}
        if me.health is not None and me.max_health:
            mine["health"] = f"{me.health}/{me.max_health}"
        for name, cur in (me.power or {}).items():
            top = (me.max_power or {}).get(name)
            mine[name] = f"{cur}/{top}" if top else cur
        raw = me.raw_fields or {}
        if uf.PLAYER_XP in raw:
            mine["xp"] = raw[uf.PLAYER_XP]
        if uf.PLAYER_NEXT_LEVEL_XP in raw:
            mine["next_level_xp"] = raw[uf.PLAYER_NEXT_LEVEL_XP]
        out["me"] = mine
    known = getattr(session, "spellbook", None) or ()
    # Only spells agent.spells has metadata for: the raw spellbook is
    # mostly passives (languages, weapon skills) that would waste tokens.
    spell_list = [{"id": i.spell_id, "name": i.name}
                  for i in (sp.get_spell_info(s) for s in sorted(known)) if i is not None]
    if spell_list:
        out["spells"] = spell_list
    return out


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


def think_and_act(session, world, brain, persona: str = "",
                   my_position=None, registry: dict | None = None,
                   audit_logger=None, cycle: int = 0,
                   reflex_state: dict | None = None,
                   state: ThinkState | None = None) -> ThinkResult:
    """One full think cycle:

    1. Build a perception snapshot (world.snapshot()).
    2. Ask the brain (agent.brain.Brain, UM-101) for exactly one action:
       Jev picking from agent.candidates.generate(), or the LLM picking
       from the action catalog exposed as tools, with the LLM as Jev's
       per-cycle fallback. A bare LLM client (anything with LLMClient's
       choose_action) is accepted too and wrapped as an LLM-only Brain.
    3. Validate the model's choice against the action registry — unknown
       action name, or an action whose check() rejects the params, never
       reaches the game.
    4. Execute exactly one validated action and return its result.

    Any failure (LLM error, invalid tool call, failed validation) yields a
    non-ok ThinkResult instead of raising — callers (the main loop) log and
    move on to the next cycle rather than crash the agent.

    If `audit_logger` (agent.audit.AuditLogger, UM-51) is given, one record
    is appended for this cycle regardless of outcome — that's the whole
    point of an audit log: it must capture failed/invalid cycles too, not
    just successful ones.

    If `state` (ThinkState, UM-90) is given, its recent history goes into
    the prompt, this cycle's outcome is appended to it, and the loop guard
    applies: an action+args identical to each of the last
    LOOP_GUARD_REPEATS cycles, all of which failed or changed nothing, is
    not executed.
    """
    registry = registry if registry is not None else ac.REGISTRY
    if not isinstance(brain, Brain):
        brain = Brain(llm=brain)
    _maybe_cancel_idle_trade(session, world)
    # corpse_position (UM-43) is session-scoped (MSG_CORPSE_QUERY), not part
    # of WorldState — pass it through so the snapshot exposes it alongside
    # is_dead/is_ghost. getattr() with a default: harmless if session is a
    # test double without it.
    snapshot = world.snapshot(my_position=my_position, corpse_position=getattr(session, "corpse_position", None),
                               pending_invite=getattr(session, "pending_invite", None),
                               chat_inbox=getattr(session, "chat_inbox", None))
    snapshot.update(_self_status(session, world))
    fingerprint = progress_fingerprint(snapshot)
    if state is not None:
        state.observe(fingerprint)

    def _remember(action_name, params, ok, error=None, detail=None):
        if state is not None:
            state.record(action_name, params, ok, fingerprint, error=error, detail=detail)

    decision = Decision()

    def _audit(action_name=None, params=None, valid=False, ok=False, error=None):
        if audit_logger is None:
            return
        try:
            audit_logger.record(
                cycle=cycle,
                snapshot=snapshot,
                tool_call={"name": action_name, "args": params or {}},
                valid=valid,
                result={"ok": ok, "error": error},
                reflex=reflex_state or {},
                goal=persona or None,
                prompt_tokens=decision.prompt_tokens,
                completion_tokens=decision.completion_tokens,
                model=decision.model,
                latency_ms=decision.latency_ms,
                brain=decision.brain,
                confidence=decision.confidence,
                fallback=decision.fallback,
                candidates=decision.candidates,
            )
        except OSError as e:  # never let audit I/O crash a think cycle
            log.warning("audit log write failed: %s", e)

    try:
        decision = brain.decide(
            snapshot, persona=persona,
            history=state.for_prompt() if state is not None else None,
            my_guid=getattr(world, "my_guid", None) or None,
            reflex_state=reflex_state,
            blocked=state.repeat_blocked if state is not None else None)
    except BrainError as e:
        decision = e.decision
        log.warning("%s", e)
        _audit(error=str(e))
        return ThinkResult(ok=False, error=str(e))
    action_name, params = decision.action, decision.params

    action = registry.get(action_name)
    if action is None:
        log.warning("model chose unknown action %r (params=%r)", action_name, params)
        _audit(action_name=action_name, params=params, valid=False,
               error=f"unknown action: {action_name!r}")
        _remember(action_name, params, False, error=f"unknown action: {action_name!r}")
        return ThinkResult(ok=False, action_name=action_name, params=params,
                            error=f"unknown action: {action_name!r}")

    missing = [p for p in action.required if p not in params]
    if missing:
        log.warning("action %s missing required params %r (got %r)", action_name, missing, params)
        _audit(action_name=action_name, params=params, valid=False,
               error=f"missing required params: {missing}")
        _remember(action_name, params, False, error=f"missing required params: {missing}")
        return ThinkResult(ok=False, action_name=action_name, params=params,
                            error=f"missing required params: {missing}")

# UM-89: the model sees and sends short handles ("u3"), never raw
    # GUIDs; map them back here, the one place tool-call params enter the
    # game. `params` (handles) stays what the audit log/ThinkResult record.
    handles = getattr(world, "handles", None)
    run_params = params
    if handles is not None:
        try:
            run_params = handles.resolve_params(params)
        except UnknownHandle as e:
            log.warning("action %s got an unknown handle (params=%r): %s", action_name, params, e)
            _audit(action_name=action_name, params=params, valid=False, error=str(e))
            return ThinkResult(ok=False, action_name=action_name, params=params, error=str(e))

    if state is not None and state.repeat_blocked(action_name, params):
        error = (f"loop guard: {action_name} with these exact args already failed or changed "
                 f"nothing {LOOP_GUARD_REPEATS} times in a row; not executed. Try something different.")
        log.warning("loop guard blocked %s %r", action_name, params)
        _audit(action_name=action_name, params=params, valid=False, error=error)
        _remember(action_name, params, False, error=error)
        return ThinkResult(ok=False, action_name=action_name, params=params, error=error)


    try:
        result = action.run(session, world, **run_params)
    except TypeError as e:
        # Unexpected/extra kwargs the model hallucinated, or a param of the
        # wrong shape reaching execute()'s positional signature.
        log.warning("action %s rejected params %r: %s", action_name, params, e)
        _audit(action_name=action_name, params=params, valid=True,
               error=f"bad params: {e}")
        _remember(action_name, params, False, error=f"bad params: {e}")
        return ThinkResult(ok=False, action_name=action_name, params=params,
                            error=f"bad params: {e}")

    if not result.ok:
        log.info("action %s failed validation/execution: %s", action_name, result.error)
        _audit(action_name=action_name, params=params, valid=True, ok=False, error=result.error)
        _remember(action_name, params, False, error=result.error)
        return ThinkResult(ok=False, action_name=action_name, params=params,
                            error=result.error, detail=result.detail)

    log.info("action %s executed: %s", action_name, result.detail)
    _audit(action_name=action_name, params=params, valid=True, ok=True)
    _remember(action_name, params, True, detail=result.detail)
    return ThinkResult(ok=True, action_name=action_name, params=params, detail=result.detail)
