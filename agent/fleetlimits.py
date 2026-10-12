#!/usr/bin/env python3
"""The fleet limits file (ADR 0017, #625): time-bounded blocks that say "do not
call brain X until T, because R", written by one process outside the agents.

    {"version": 1, "written_ts": 1700000000.0,
     "blocks": {"jev": {"until_ts": 1700000030.0, "reason": "fleet Jev budget spent"}}}

`FleetLimits.blocked(brain)` is what `Brain.decide` asks before a Jev or an LLM
call. The only expiry is `until_ts`; `written_ts` is informational and is never
read here. Agents fail open: a missing, unreadable, oversized, malformed or
other-version file blocks nothing, and each distinct problem is logged once
(not once per think cycle) and again only after the file has been good in
between.

Not thread-safe: `Brain.decide` runs on the think thread only, and the cache
below is that thread's alone. The file is never held open; it is read whole
and closed, and a rename over it is seen because the cache key includes the
inode.
"""

import json
import logging
import math
import os
import stat
import time
from collections.abc import Callable
from datetime import UTC, datetime

from .metrics import swallowed

log = logging.getLogger("agent.fleetlimits")

SCHEMA_VERSION = 1
MAX_BYTES = 65536
REASON_MAX_CHARS = 200
BRAINS = ("jev", "llm")   # keys of `blocks` that mean something; others are ignored


class FleetLimits:
    def __init__(self, path: str, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        self._key: tuple[int, int, int] | None = None   # (mtime_ns, size, inode) of what _blocks was parsed from
        self._blocks: dict[str, tuple[float, str]] = {}   # brain -> (until_ts, reason), valid entries only
        self._logged: set[str] = set()   # problems warned about and still present

    def blocked(self, brain: str) -> str | None:
        """The reason `brain` is blocked right now, or None. Never raises."""
        try:
            self._refresh()
            entry = self._blocks.get(brain)
            if entry is None:
                return None
            until, reason = entry
            if not until > self._clock():
                return None
            return f"{brain} blocked by fleet limits until {_iso(until)}: {reason}"
        except Exception:   # noqa: BLE001 - fail open, whatever went wrong
            self._blocks, self._key = {}, None
            swallowed("fleetlimits.blocked", log)   # counted and logged, throttled
            return None

    # ── internals ──────────────────────────────────────────────────
    def _refresh(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError as e:
            return self._fail("stat", f"cannot stat ({type(e).__name__}), no block applies")
        if not stat.S_ISREG(st.st_mode):
            return self._fail("not-a-file", "not a regular file")
        if st.st_size > MAX_BYTES:
            return self._fail("too-big", f"larger than {MAX_BYTES} bytes")
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        if key == self._key:
            return
        try:
            with open(self.path, "rb") as f:
                raw = f.read(MAX_BYTES + 1)
        except OSError as e:
            return self._fail("read", f"cannot read ({type(e).__name__})")
        if len(raw) > MAX_BYTES:   # grew between stat and read
            return self._fail("too-big", f"larger than {MAX_BYTES} bytes")
        try:
            doc = json.loads(raw)
        except (ValueError, RecursionError) as e:   # JSONDecodeError and UnicodeDecodeError are ValueErrors
            return self._fail("json", f"not valid JSON ({type(e).__name__})")
        blocks, problems = _validate(doc)
        self._blocks, self._key = blocks, key
        self._report(problems)

    def _fail(self, name: str, message: str) -> None:
        self._blocks, self._key = {}, None
        self._report({name: message})

    def _report(self, problems: dict[str, str]) -> None:
        """Warn about each problem that was not already reported; forget the ones that cleared."""
        for name, message in problems.items():
            if name not in self._logged:
                log.warning("fleet limits file %r: %s", self.path[:200], message)
        self._logged = set(problems)


def _validate(doc: object) -> tuple[dict[str, tuple[float, str]], dict[str, str]]:
    """The valid blocks of a parsed file and a bounded description of each problem."""
    if not isinstance(doc, dict):
        return {}, {"shape": "top level is not an object"}
    version = doc.get("version")
    if type(version) is not int or version != SCHEMA_VERSION:
        return {}, {"version": f"unsupported version (want {SCHEMA_VERSION})"}
    raw_blocks = doc.get("blocks")
    if not isinstance(raw_blocks, dict):
        return {}, {"blocks": "blocks is not an object"}
    blocks: dict[str, tuple[float, str]] = {}
    problems: dict[str, str] = {}
    for brain in BRAINS:
        if brain not in raw_blocks:
            continue
        entry = raw_blocks[brain]
        raw_until = entry.get("until_ts") if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or isinstance(raw_until, bool) or not isinstance(raw_until, int | float):
            problems[f"entry-{brain}"] = f"block for {brain} is not an object with a numeric until_ts"
            continue
        try:
            until = float(raw_until)
        except OverflowError:
            until = math.nan
        if not math.isfinite(until):
            problems[f"entry-{brain}"] = f"until_ts of {brain} is not a finite number"
            continue
        reason = entry.get("reason")
        text = " ".join(reason.split()) if isinstance(reason, str) else ""
        blocks[brain] = (until, text[:REASON_MAX_CHARS])
    return blocks, problems


def _iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return f"{ts:.0f}"
