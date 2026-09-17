#!/usr/bin/env python3
"""Item template cache (UM-42): dedupe/budget CMSG_ITEM_QUERY_SINGLE requests
and cache SMSG_ITEM_QUERY_SINGLE_RESPONSE results by item entry, the same
shape as agent.names.NameCache for creature/gameobject templates — see that
module's docstring for the rationale (item templates are static, so caching
across sessions is safe; player names are not cached there for the opposite
reason)."""

import collections
import json
import os
import time

DEFAULT_CACHE_PATH = os.path.expanduser("~/.cache/wow-agent/items.json")


class ItemCache:
    """entry -> agent.loot.parse_item_query_response() dict, or None if the
    server said "not found". Not thread-safe on its own; agent.perception.
    WorldState (which owns an instance) guards every call with its own lock,
    same as NameCache."""

    def __init__(self, budget_per_second: int = 10, clock=time.monotonic,
                 cache_path: str = DEFAULT_CACHE_PATH):
        self.items: dict[int, dict | None] = {}
        self._in_flight: set[int] = set()
        self._pending: collections.deque = collections.deque()  # entry
        self._sent_times: collections.deque = collections.deque()
        self._budget_per_second = budget_per_second
        self._clock = clock
        self.cache_path = cache_path
        self.duplicate_queries = 0

    def want_item(self, entry: int):
        if entry in self.items:
            return
        if entry in self._in_flight:
            self.duplicate_queries += 1
            return
        self._in_flight.add(entry)
        self._pending.append(entry)

    def drain(self, max_items: int | None = None) -> list[int]:
        """Pop up to the current send budget's worth of pending item
        entries. Callers (agent/session.py) send one CMSG_ITEM_QUERY_SINGLE
        per returned entry."""
        now = self._clock()
        while self._sent_times and now - self._sent_times[0] >= 1.0:
            self._sent_times.popleft()
        available = self._budget_per_second - len(self._sent_times)
        if max_items is not None:
            available = min(available, max_items)
        out = []
        while available > 0 and self._pending:
            entry = self._pending.popleft()
            out.append(entry)
            self._sent_times.append(now)
            available -= 1
        return out

    def on_item_query_response(self, data: dict):
        self._in_flight.discard(data["entry"])
        self.items[data["entry"]] = data if data["found"] else None
        if data["found"]:
            self.save()

    def load(self, path: str | None = None):
        try:
            with open(path or self.cache_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        self.items.update({int(k): v for k, v in data.get("items", {}).items()})

    def save(self, path: str | None = None):
        path = path or self.cache_path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + f".tmp{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump({"items": self.items}, f)
            os.replace(tmp, path)
        except OSError:
            pass
