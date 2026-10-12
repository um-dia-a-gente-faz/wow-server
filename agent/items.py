#!/usr/bin/env python3
"""Item template cache (UM-42): dedupe/budget CMSG_ITEM_QUERY_SINGLE requests
and cache SMSG_ITEM_QUERY_SINGLE_RESPONSE results by item entry, the same
shape as agent.names.NameCache for creature/gameobject templates — see that
module's docstring for the rationale (item templates are static, so caching
across sessions is safe; player names are not cached there for the opposite
reason)."""

import collections
import contextlib
import logging
import os
import time

from . import persist

log = logging.getLogger("agent.items")

DEFAULT_CACHE_PATH = os.path.expanduser("~/.cache/wow-agent/items.json")
CACHE_SCHEMA = 1   # ADR 0018: the `schema` key of items.json


# Keys every cached item must carry, with the exact type (type(), not isinstance). The
# parser always sets `name` on a found item (a cstring), so everything ItemCache writes passes.
_ITEM_REQUIRED: dict[str, type] = {"name": str}


def _parse_items_section(section, required: dict[str, type]) -> dict[int, dict | None]:
    """The `items` section of the cache file: {"<entry>": parsed dict or null}. A null entry is a
    cached "not found". An object entry must carry every `required` key with that type; the
    readers index those keys without checking. Raises ValueError on any other shape, so load()
    drops the whole file."""
    if not isinstance(section, dict):
        raise ValueError(f"items section is {type(section).__name__}, expected an object")
    out: dict[int, dict | None] = {}
    for key, value in section.items():
        try:
            entry = int(key)
        except ValueError:
            raise ValueError(f"non-numeric entry key {key!r}") from None
        if value is None:
            out[entry] = None
            continue
        if not isinstance(value, dict):
            raise ValueError(f"entry {entry} is {type(value).__name__}, expected an object or null")
        for name, kind in required.items():
            if type(value.get(name)) is not kind:
                raise ValueError(f"entry {entry} has no usable {name!r}")
        out[entry] = value
    return out


class ItemCache:
    """entry -> agent.loot.parse_item_query_response() dict, or None if the
    server said "not found". Not thread-safe on its own; agent.perception.
    WorldState (which owns an instance) guards every call with its own lock,
    same as NameCache, and save() is its second exception (#705): the persisted dicts (items) are mutated only by
    on_*_query_response on the recv thread and by load() before that thread starts, and
    save() is called on that same thread after the lock is released, so a save never
    overlaps a mutation and saves never overlap each other. A new mutator, or a save() on
    another thread, needs a lock.
    """

    def __init__(self, budget_per_second: int = 10, clock=time.monotonic,
                 cache_path: str = DEFAULT_CACHE_PATH):
        self.items: dict[int, dict | None] = {}
        self._in_flight: set[int] = set()
        self._pending: collections.deque = collections.deque()  # entry
        self._sent_times: collections.deque = collections.deque()
        self._budget_per_second = budget_per_second
        self._clock = clock
        self.cache_path = cache_path
        self._no_save: set[str] = set()  # paths whose file is of a newer schema: save() leaves them alone
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

    def on_item_query_response(self, data: dict, save: bool = True):
        self._in_flight.discard(data["entry"])
        self.items[data["entry"]] = data if data["found"] else None
        if save and data["found"]:
            self.save()

    def load(self, path: str | None = None):
        """Best-effort, same rules as NameCache.load (ADR 0018): missing or older is a
        silent cold start; newer is left alone with saving to that path off; unreadable is
        moved to <path>.corrupt. All or nothing."""
        path = path or self.cache_path
        loaded = persist.read_versioned(path, CACHE_SCHEMA)
        if loaded.status in (persist.MISSING, persist.OLDER):
            return
        if loaded.status == persist.NEWER:
            log.warning("item cache %s is schema %d, newer than %d: not read, not overwritten",
                        path, loaded.version, CACHE_SCHEMA)
            self._no_save.add(path)
            return
        if loaded.status == persist.OK:
            try:
                items = _parse_items_section(loaded.data.get("items", {}), _ITEM_REQUIRED)
            except ValueError as e:
                log.warning("ignoring unreadable item cache %s: %s", path, str(e)[:200])
                persist.set_aside(path)
                return
            self.items.update(items)
            return
        log.warning("ignoring unreadable item cache %s", path)
        persist.set_aside(path)

    def save(self, path: str | None = None):
        path = path or self.cache_path
        if path in self._no_save:
            return
        with contextlib.suppress(OSError):
            persist.write_versioned(path, CACHE_SCHEMA, {"items": self.items})
