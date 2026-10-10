"""Think-loop memory (UM-90): recent-action history, progress fingerprint and
the loop guard. No session or world dependency; agent.think re-exports these."""

import hashlib
import json
from collections import deque

from .model import ActionRecord, Snapshot

HISTORY_LEN = 8          # UM-90: recent cycles shown to the model
LOOP_GUARD_REPEATS = 3   # UM-90: identical failed/no-op calls before the guard blocks the next one
_DETAIL_MAX_CHARS = 160  # keep each history line short; the prompt is token-budgeted

# Snapshot keys whose change means an action did something. Deliberately
# leaves out things that drift on their own (own health/mana regen, chat,
# other units' positions) so "changed" means progress, not noise.
_PROGRESS_KEYS = ("position", "is_dead", "is_ghost", "window", "trade", "mailbox",
                  "equipment", "inventory", "quest_log", "pending_invite", "group")


def _args_key(params: dict) -> str:
    return json.dumps(params or {}, sort_keys=True, default=str)


def progress_fingerprint(snapshot: Snapshot) -> str:
    """Digest of the parts of a snapshot an action can change (UM-90's
    "returned no state change" test): the _PROGRESS_KEYS (position rounded
    to 1 yd so float jitter doesn't count), own level/xp, and the health of
    units in combat (so a fight in progress counts as a change)."""
    view = {k: snapshot.get(k) for k in _PROGRESS_KEYS}
    pos = snapshot.get("position")
    if isinstance(pos, dict):
        view["position"] = {k: round(v) if isinstance(v, float) else v for k, v in pos.items()}
    me = snapshot.get("me")
    view["me"] = {"level": me.get("level") if me else None, "xp": me.get("xp") if me else None}
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
        action with these exact args and either failed or changed nothing.

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
        return True

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
