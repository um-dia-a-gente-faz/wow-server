"""Think-loop memory (UM-90): recent-action history, the idle watchdog (#320) and
the loop guard. No session or world dependency; agent.think re-exports these."""

import json
import time
from collections import deque

from . import config
from .model import ActionRecord
from .watchdog import Watchdog  # #320: ThinkState owns one

HISTORY_LEN = 8          # UM-90: recent cycles shown to the model
LOOP_GUARD_REPEATS = 3   # UM-90: identical failed/no-op calls before the guard blocks the next one
LOOP_GUARD_COOLDOWN_S = 15.0  # #521: a block lapses once its newest matching entry is this old
_DETAIL_MAX_CHARS = 160  # keep each history line short; the prompt is token-budgeted

def _args_key(params: dict) -> str:
    return json.dumps(params or {}, sort_keys=True, default=str)


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

    def __init__(self, maxlen: int = HISTORY_LEN, clock=time.monotonic):
        self.history: deque = deque(maxlen=maxlen)
        self.clock = clock  # #521: injectable so the cooldown is testable without sleeping
        self.watchdog = Watchdog(window_s=config.get("AGENT_WATCHDOG_WINDOW_S"),  # #320
                                 idle_cycles=config.get("AGENT_WATCHDOG_IDLE_CYCLES"), clock=clock)

    def observe(self, fingerprint: str) -> None:
        """Called once per cycle with the fresh snapshot's fingerprint:
        settles whether the previous action changed anything."""
        if self.history and self.history[-1]["changed"] is None:
            last = self.history[-1]
            last["changed"] = fingerprint != last["fingerprint"]

    def record(self, action: str, args: dict, ok: bool, fingerprint: str,
               error: str | None = None, detail=None) -> None:
        self.history.append({"action": action, "args": dict(args or {}), "ok": ok,
                             "error": error, "detail": detail, "at": self.clock(),
                             "fingerprint": fingerprint, "changed": None})

    def repeat_blocked(self, action: str, args: dict, n: int = LOOP_GUARD_REPEATS) -> bool:
        """True when each of the last `n` recorded cycles was this exact
        action with these exact args and either failed or changed nothing,
        and the newest of them is not yet LOOP_GUARD_COOLDOWN_S old (#521: a
        time-gated action such as reclaim_corpse is retried once its gate may
        have opened; its refusal re-arms the block).

        `idle` is the candidate generator's safe fallback when it has no
        actionable option. Blocking it cannot make the brain choose something
        else and would turn an empty candidate set into a permanent deadlock.
        """
        if action == "idle":
            return False
        if len(self.history) < n:
            return False
        key = _args_key(args)
        for h in list(self.history)[-n:]:
            if h["action"] != action or _args_key(h["args"]) != key:
                return False
            if h["ok"] and h["changed"] is not False:
                return False
        return self.clock() - self.history[-1]["at"] <= LOOP_GUARD_COOLDOWN_S

    def for_prompt(self) -> list[ActionRecord]:
        """Compact oldest-first view for agent.llm.build_messages."""
        out: list[ActionRecord] = []
        for h in self.history:
            e: ActionRecord = {"action": h["action"], "args": h["args"], "ok": h["ok"]}
            if h["error"]:
                e["error"] = _short(h["error"])
            elif (d := _short(h["detail"])) is not None:
                e["result"] = d
            if h["changed"] is False:
                e["changed"] = False
            out.append(e)
        return out
