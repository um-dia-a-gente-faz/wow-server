#!/usr/bin/env python3
"""Idle watchdog (#320): a general no-progress monitor run once per think cycle.

Progress is `progress_fingerprint` changing: the same test the loop guard's "changed"
flag uses (agent.loopguard re-exports it). Two triggers, each fired once per idle stretch
and re-armed when the fingerprint changes:
  - "stuck_follow": following, and no progress for `window_s` seconds.
  - "idle": no progress for `idle_cycles` consecutive cycles, following or not.
A trigger set to 0 is off. Stdlib only; no I/O. This module must not import agent.loopguard
(loopguard builds a Watchdog for ThinkState), so the fingerprint lives here.
"""

import hashlib
import json
import time

from .model import Snapshot

# Snapshot keys whose change means an action did something. Deliberately
# leaves out things that drift on their own (own health/mana regen, chat,
# other units' positions) so "changed" means progress, not noise.
_PROGRESS_KEYS = ("position", "is_dead", "is_ghost", "window", "trade", "mailbox",
                  "equipment", "inventory", "quest_log", "pending_invite", "group")


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


class Watchdog:
    def __init__(self, window_s: float, idle_cycles: int, clock=time.monotonic):
        self.window_s = window_s
        self.idle_cycles = idle_cycles
        self._clock = clock
        self._fingerprint: str | None = None
        self._changed_at = 0.0
        self._unchanged = 0
        self._fired: set[str] = set()

    def observe(self, snapshot: Snapshot, following: bool) -> list[str]:
        """Called once per think cycle. Returns the triggers that fire this cycle."""
        now = self._clock()
        fingerprint = progress_fingerprint(snapshot)
        if fingerprint != self._fingerprint:
            self._fingerprint, self._changed_at, self._unchanged = fingerprint, now, 0
            self._fired.clear()
            return []
        self._unchanged += 1
        fires: list[str] = []
        if following and self.window_s > 0 and now - self._changed_at >= self.window_s:
            fires.append("stuck_follow")
        if self.idle_cycles > 0 and self._unchanged >= self.idle_cycles:
            fires.append("idle")
        fresh = [t for t in fires if t not in self._fired]
        self._fired.update(fresh)
        return fresh
