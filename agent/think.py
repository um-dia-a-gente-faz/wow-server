#!/usr/bin/env python3
"""Think step (UM-44): perception snapshot + action catalog -> one LLM call
-> one validated action executed, per think cycle.

Wired into agent/__main__.py::_run_loop between the perceive and act
comments that Phase 3 (docs/ROADMAP.md) left as placeholders. Kept in its
own module (not inlined in __main__.py) so it can be unit-tested against a
fake LLM client without booting a real WoWSession.
"""

import logging

from . import actions as ac
from .llm import LLMError

log = logging.getLogger("agent.think")


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


def think_and_act(session, world, llm_client, persona: str = "",
                   my_position=None, registry: dict | None = None,
                   audit_logger=None, cycle: int = 0,
                   reflex_state: dict | None = None) -> ThinkResult:
    """One full think cycle:

    1. Build a perception snapshot (world.snapshot()).
    2. Ask the LLM to pick exactly one action, from the action catalog
       exposed as tools (agent.actions.catalog()).
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
    """
    registry = registry if registry is not None else ac.REGISTRY
    snapshot = world.snapshot(my_position=my_position)

    def _audit(action_name=None, params=None, valid=False, ok=False, error=None):
        if audit_logger is None:
            return
        usage = getattr(llm_client, "last_usage", None) or {}
        try:
            audit_logger.record(
                cycle=cycle,
                snapshot=snapshot,
                tool_call={"name": action_name, "args": params or {}},
                valid=valid,
                result={"ok": ok, "error": error},
                reflex=reflex_state or {},
                goal=persona or None,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                model=getattr(llm_client, "model", None),
                latency_ms=getattr(llm_client, "last_latency_ms", None),
            )
        except OSError as e:  # never let audit I/O crash a think cycle
            log.warning("audit log write failed: %s", e)

    try:
        action_name, params = llm_client.choose_action(snapshot, ac.catalog(), persona=persona)
    except LLMError as e:
        log.warning("llm call failed: %s", e)
        _audit(error=f"llm call failed: {e}")
        return ThinkResult(ok=False, error=f"llm call failed: {e}")

    action = registry.get(action_name)
    if action is None:
        log.warning("model chose unknown action %r (params=%r)", action_name, params)
        _audit(action_name=action_name, params=params, valid=False,
               error=f"unknown action: {action_name!r}")
        return ThinkResult(ok=False, action_name=action_name, params=params,
                            error=f"unknown action: {action_name!r}")

    missing = [p for p in action.required if p not in params]
    if missing:
        log.warning("action %s missing required params %r (got %r)", action_name, missing, params)
        _audit(action_name=action_name, params=params, valid=False,
               error=f"missing required params: {missing}")
        return ThinkResult(ok=False, action_name=action_name, params=params,
                            error=f"missing required params: {missing}")

    try:
        result = action.run(session, world, **params)
    except TypeError as e:
        # Unexpected/extra kwargs the model hallucinated, or a param of the
        # wrong shape reaching execute()'s positional signature.
        log.warning("action %s rejected params %r: %s", action_name, params, e)
        _audit(action_name=action_name, params=params, valid=True,
               error=f"bad params: {e}")
        return ThinkResult(ok=False, action_name=action_name, params=params,
                            error=f"bad params: {e}")

    if not result.ok:
        log.info("action %s failed validation/execution: %s", action_name, result.error)
        _audit(action_name=action_name, params=params, valid=True, ok=False, error=result.error)
        return ThinkResult(ok=False, action_name=action_name, params=params,
                            error=result.error, detail=result.detail)

    log.info("action %s executed: %s", action_name, result.detail)
    _audit(action_name=action_name, params=params, valid=True, ok=True)
    return ThinkResult(ok=True, action_name=action_name, params=params, detail=result.detail)
