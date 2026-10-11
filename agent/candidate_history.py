"""Candidate history post-processing (GH-166, split out of candidates.py for #550).

`history_effects` reads the think-cycle history and says which candidate ids to drop
(a retry cannot work) or demote (a no-op repeat); `finish` de-duplicates the provider
output, applies those maps, caps the list and ends it with idle. `candidates.generate`
is the only caller. Nothing here imports candidates: the `candidate` builder is passed
into `history_effects` by the caller.
"""

import logging
from collections.abc import Callable

from .metrics import swallowed
from .model import ActionRecord, Candidate

HISTORY_DROP_WINDOW = 4     # only the most recent entries can drop an option
HISTORY_DEMOTE_REPEATS = 2  # consecutive no-effect successes before demotion
# Error text (agent/think.py, agent/handles.py) that retrying the same args cannot fix.
_DETERMINISTIC_ERRORS = ("missing required params", "bad params", "unknown handle", "unknown action")


def history_effects(history: list[ActionRecord] | None,
                    candidate: Callable[[str, dict, str], Candidate]) -> tuple[dict, dict]:
    """({id: reason} to drop, {id: reason} to demote) from ThinkState history.
    Tolerates malformed entries (skipped); never raises."""
    entries = []
    for h in history or []:
        if not isinstance(h, dict) or not isinstance(h.get("action"), str):
            continue
        args = h.get("args")
        args = args if isinstance(args, dict) else {}
        try:
            cid = candidate(h["action"], args, "")["id"]
        except Exception:
            swallowed("candidates.history", logging.getLogger("agent.candidates"), h["action"],
                      level=logging.WARNING)
            continue
        entries.append((cid, h))
    drop, demote = {}, {}

    recent = entries[-HISTORY_DROP_WINDOW:]
    by_id: dict[str, list[ActionRecord]] = {}
    for cid, h in recent:
        by_id.setdefault(cid, []).append(h)
    for cid, hs in by_id.items():
        last = hs[-1]
        if last.get("ok") is not False:
            continue
        err = str(last.get("error") or "")
        if any(m in err.lower() for m in _DETERMINISTIC_ERRORS):
            drop[cid] = f"rejected: {err}"
        elif len(hs) >= 2 and hs[-2].get("ok") is False and hs[-2].get("error") == last.get("error"):
            drop[cid] = f"failed twice in a row: {err}"

    if entries:
        tail_id = entries[-1][0]
        run = 0
        for cid, h in reversed(entries):
            if cid != tail_id or h.get("ok") is not True or h.get("changed") is not False:
                break
            run += 1
        if run >= HISTORY_DEMOTE_REPEATS and tail_id not in drop:
            demote[tail_id] = f"repeated {run}x with no effect"
    return drop, demote


def finish(out: list[Candidate], idle: Candidate, limit: int, drop: dict | None = None,
           demote: dict | None = None, protected: set | frozenset = frozenset(),
           notes: list | None = None) -> list[Candidate]:
    """De-duplicate by id (first, highest-priority wins), apply the history
    `drop`/`demote` maps ({id: reason}; `protected` ids are exempt), cap, end
    with idle."""
    drop, demote = drop or {}, demote or {}
    seen, unique, demoted = set(), [], []
    for c in out:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        if c["id"] not in protected:
            if c["id"] in drop:
                if notes is not None:
                    notes.append({"id": c["id"], "effect": "dropped", "reason": drop[c["id"]]})
                continue
            if c["id"] in demote:
                if notes is not None:
                    notes.append({"id": c["id"], "effect": "demoted", "reason": demote[c["id"]]})
                demoted.append(c)
                continue
        unique.append(c)
    return (unique + demoted)[:limit - 1] + [idle]
