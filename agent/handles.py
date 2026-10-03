"""Short string handles for 64-bit GUIDs in what the LLM sees (UM-89).

A raw GUID like 17379391218345059262 is above 2^53, so it doesn't survive a
JSON round-trip through a float64 (the gateway/model returned
17379391218345058000 in the 2026-09-19 live run and every GUID-taking action
failed with "not currently perceived"). The snapshot therefore shows a short
handle such as "u3" instead, the tool schemas take that handle as a string,
and think.py maps it back to the real GUID before the action runs.

Handles are handed out once per GUID and never reused or recycled for the
life of a HandleMap (one per WorldState, i.e. per session), so a handle the
model saw a few cycles ago still means the same object. The prefix comes
from the GUID's HighGuid (the top 16 bits, ObjectGuid.h `enum class
HighGuid` on the TrinityCore 3.3.5 branch), so the model can tell a
creature ("u") from a player ("p"), a gameobject ("o"), an item ("i") or a
corpse ("c"). The number is one counter shared by every prefix.
"""

import threading

# ObjectGuid.h (TrinityCore 3.3.5) HighGuid -> handle prefix.
_PREFIX_BY_HIGH = {
    0x0000: "p",  # Player
    0x4000: "i",  # Item / Container
    0xF110: "o",  # GameObject
    0xF120: "o",  # Transport
    0x1FC0: "o",  # Mo_Transport
    0xF100: "o",  # DynamicObject
    0xF130: "u",  # Unit
    0xF140: "u",  # Pet
    0xF150: "u",  # Vehicle
    0xF101: "c",  # Corpse
}


def _prefix(guid: int) -> str:
    return _PREFIX_BY_HIGH.get((guid >> 48) & 0xFFFF, "g")


def is_guid_key(key) -> bool:
    """True for a snapshot key / action param that holds a GUID: `guid`
    itself or anything ending in `_guid` (vendor_guid, target_guid, ...)."""
    return isinstance(key, str) and (key == "guid" or key.endswith("_guid"))


def is_guid_list_key(key) -> bool:
    """True for a key holding a list of GUIDs (e.g. loot's `item_guids`)."""
    return isinstance(key, str) and key.endswith("_guids")


class UnknownHandle(ValueError):
    pass


class HandleMap:
    """Bidirectional GUID <-> handle map. Thread-safe: snapshot() runs on
    the main loop while nothing else writes, but it costs nothing to be sure."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_guid: dict[int, str] = {}
        self._by_handle: dict[str, int] = {}
        self._next = 1

    def handle_for(self, guid: int) -> str | None:
        """The handle for `guid`, allocating one on first sight. GUID 0
        (the protocol's "nothing", e.g. no target) maps to None."""
        if not guid:
            return None
        with self._lock:
            handle = self._by_guid.get(guid)
            if handle is None:
                handle = f"{_prefix(guid)}{self._next}"
                self._next += 1
                self._by_guid[guid] = handle
                self._by_handle[handle] = guid
            return handle

    def resolve(self, handle: str) -> int:
        """Handle -> GUID. Raises UnknownHandle for anything not handed out."""
        key = handle.strip().lower() if isinstance(handle, str) else handle
        with self._lock:
            guid = self._by_handle.get(key)
        if guid is None:
            raise UnknownHandle(f"unknown handle {handle!r}: use a handle from the snapshot, like 'u3' or 'p1'")
        return guid

    def encode(self, value):
        """A deep copy of `value` (a snapshot or any JSON-ish structure) with
        every GUID-valued field replaced by its handle. Never mutates the
        input, which may be live WorldState dicts like ui_state/trade."""
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if is_guid_key(k) and isinstance(v, int) and not isinstance(v, bool):
                    out[k] = self.handle_for(v)
                elif is_guid_list_key(k) and isinstance(v, (list, tuple)):
                    out[k] = [self.handle_for(g) if isinstance(g, int) else g for g in v]
                else:
                    out[k] = self.encode(v)
            return out
        if isinstance(value, (list, tuple)):
            return [self.encode(v) for v in value]
        return value

    def resolve_params(self, params: dict) -> dict:
        """Tool-call params with every GUID param (`guid`, `*_guid`) given as
        a handle string mapped back to its GUID. Ints and None pass through
        unchanged, so Python callers that already hold a real GUID still
        work. Raises UnknownHandle for a string that isn't a known handle."""
        out = dict(params)
        for k, v in params.items():
            if is_guid_key(k) and isinstance(v, str):
                try:
                    out[k] = self.resolve(v)
                except UnknownHandle as e:
                    raise UnknownHandle(f"{k}: {e}") from None
        return out
