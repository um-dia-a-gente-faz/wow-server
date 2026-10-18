#!/usr/bin/env python3
"""Unmet-intent records (#319): one record when the brain names an action that does not
exist (`unknown_action`), when a cycle's candidate list held only `idle` (`no_candidates`), or
when a ghost's corpse run reached the corpse spot with no corpse there (`corpse_unreachable`, #763,
once per death).

Kept in memory (GET /unmet-intents, the newest MAX_RECENT) and written to
`<agent>/unmet/<day>.jsonl` through AuditLogger.record_unmet_intent, never to the decision
day files, which hold one line per think cycle. Stdlib only, no network: nothing here
creates an issue or calls out.
"""

import json
import logging
import threading
import time
from collections import deque

log = logging.getLogger("agent.unmet")

UNMET_SCHEMA = 1
KINDS = ("unknown_action", "no_candidates", "corpse_unreachable", "no_progress")
MAX_RECENT = 50
DEDUP_S = 60.0
STATE_MAX_CHARS = 512      # the state holds scalars and counts only, so it is bounded by construction
PARAMS_MAX_CHARS = 256
PARAMS_PREVIEW_CHARS = 250
ACTION_MAX_CHARS = 64      # an LLM can name anything; the record keeps a bounded prefix


def _compact(obj) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"), sort_keys=True)


def _round(v):
    return None if v is None else round(v, 1)


def _state(snapshot) -> dict:
    """The bounded view of a snapshot: own level, rounded position, health, flags and counts."""
    snap = snapshot or {}
    me = snap.get("me") or {}
    pos = snap.get("position")
    return {
        "level": me.get("level"),
        "position": None if not pos else {"map": pos.get("map"), "x": _round(pos.get("x")),
                                          "y": _round(pos.get("y")), "z": _round(pos.get("z"))},
        "health": me.get("health"),
        "dead": bool(snap.get("is_dead")),
        "in_combat": bool(me.get("in_combat")),
        "nearby_units": len(snap.get("nearby_units") or ()),
        "quests": len(snap.get("quest_log") or ()),
    }


def _params(params) -> dict:
    params = dict(params or {})
    text = _compact(params)
    if len(text) <= PARAMS_MAX_CHARS:
        return params
    return {"truncated": text[:PARAMS_PREVIEW_CHARS]}


class UnmetIntents:
    """Records unmet intents for one agent. `record` runs on the think thread; `recent` may be
    read by the HTTP thread, so the two share a lock."""

    def __init__(self, agent: str, mission: str, audit_logger=None, clock=time.time):
        self.agent = agent
        self.mission = mission
        self.audit_logger = audit_logger
        self._clock = clock
        self._lock = threading.Lock()
        self._recent: deque = deque(maxlen=MAX_RECENT)
        self._last: dict[tuple, float] = {}   # (kind, action) -> when last recorded; pruned past DEDUP_S

    def record(self, kind: str, action: str | None, params: dict | None, snapshot) -> dict | None:
        """The record written, or None when the same (kind, action) was recorded within DEDUP_S."""
        now = self._clock()
        wanted = None if action is None else str(action)[:ACTION_MAX_CHARS]
        with self._lock:
            self._last = {k: t for k, t in self._last.items() if now - t < DEDUP_S}
            if (kind, wanted) in self._last:
                return None
            self._last[(kind, wanted)] = now
        rec = {"schema": UNMET_SCHEMA, "ts": now, "agent": self.agent, "mission": self.mission,
               "kind": kind, "wanted": {"action": wanted, "params": _params(params)},
               "state": _state(snapshot)}
        with self._lock:
            self._recent.append(rec)
        if self.audit_logger is not None:
            try:
                self.audit_logger.record_unmet_intent(rec)
            except OSError as e:  # never let audit I/O crash a think cycle
                log.warning("unmet-intent audit write failed: %s", e)
        return rec

    def recent(self) -> list[dict]:
        """The newest MAX_RECENT records, oldest first."""
        with self._lock:
            return list(self._recent)
