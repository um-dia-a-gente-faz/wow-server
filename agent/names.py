#!/usr/bin/env python3
"""Name resolution for perceived objects: CMSG_NAME_QUERY / CMSG_CREATURE_QUERY /
CMSG_GAMEOBJECT_QUERY request builders + response parsers (pure, no I/O — see
agent/session.py for the opcodes and dispatch), and NameCache (per-kind
dicts, in-flight dedupe, a send budget, on-disk persistence for the static
creature/gameobject templates).

Every wire layout below is copied from TrinityCore branch `3.3.5`
(`Server/Packets/QueryPackets.h`/`.cpp`, `Handlers/QueryHandler.cpp`,
`Entities/Creature/CreatureData.h`, `Entities/GameObject/GameObjectData.h`,
`shared/SharedDefines.h`) and cited by file — see docs/PROTOCOL-NOTES.md for
the full writeup.
"""

import collections
import json
import os
import struct
import time

from . import packets as pk

DEFAULT_CACHE_PATH = os.path.expanduser("~/.cache/wow-agent/names.json")

# CreatureEliteType (SharedDefines.h) — Classification in QueryCreatureResponse.
CREATURE_RANK_NAMES = {
    0: "normal", 1: "elite", 2: "rareelite", 3: "worldboss", 4: "rare", 5: "trivial",
}

# CreatureType (SharedDefines.h).
CREATURE_TYPE_NAMES = {
    1: "beast", 2: "dragonkin", 3: "demon", 4: "elemental", 5: "giant",
    6: "undead", 7: "humanoid", 8: "critter", 9: "mechanical",
    10: "not_specified", 11: "totem", 12: "non_combat_pet", 13: "gas_cloud",
}

MAX_KILL_CREDIT = 2
MAX_CREATURE_MODELS = 4
MAX_CREATURE_QUEST_ITEMS = 6
MAX_GAMEOBJECT_DATA = 24
MAX_GAMEOBJECT_QUEST_ITEMS = 6


def build_name_query(guid: int) -> bytes:
    """CMSG_NAME_QUERY (0x050): WorldPackets::Query::QueryPlayerName::Read
    (QueryPackets.cpp) — a single raw (not packed) uint64 guid."""
    return struct.pack('<Q', guid)


def build_creature_query(entry: int, guid: int) -> bytes:
    """CMSG_CREATURE_QUERY (0x060): WorldPackets::Query::QueryCreature::Read —
    uint32 entry, then a raw (not packed) uint64 guid. The guid is a sample
    instance of this entry (TrinityCore doesn't validate it — only entry
    matters, the query answers "what is creature template N", not "what is
    this specific creature") — any known guid with this entry works."""
    return struct.pack('<IQ', entry, guid)


def build_gameobject_query(entry: int, guid: int) -> bytes:
    """CMSG_GAMEOBJECT_QUERY (0x05E): same shape as build_creature_query."""
    return struct.pack('<IQ', entry, guid)


def parse_name_query_response(payload: bytes) -> dict:
    """SMSG_NAME_QUERY_RESPONSE (0x051): WorldPackets::Query::
    QueryPlayerNameResponse::Write. packedGuid player, uint8 result (0 = full
    data follows, non-zero = not found), then if found: cstring name,
    cstring realmName, uint8 race, uint8 sex, uint8 classId, uint8
    hasDeclinedNames, [5 cstrings if hasDeclinedNames — a Cyrillic-client-only
    feature basically never seen on this server; not parsed, and since this
    is a self-delimited packet with nothing after it, not fully consuming
    those bytes is harmless].
    """
    guid, off = pk.unpack_packed_guid(payload, 0)
    result = payload[off]; off += 1
    info = {"guid": guid, "found": result == 0}
    if not info["found"]:
        return info
    info["name"], off = pk.cstring(payload, off)
    info["realm"], off = pk.cstring(payload, off)
    info["race"] = payload[off]; off += 1
    info["sex"] = payload[off]; off += 1
    info["class_"] = payload[off]; off += 1
    info["has_declined_names"] = bool(payload[off]); off += 1
    return info


def parse_creature_query_response(payload: bytes) -> dict:
    """SMSG_CREATURE_QUERY_RESPONSE (0x061): WorldPackets::Query::
    QueryCreatureResponse::Write. uint32 (entry | (found ? 0 : 0x80000000)),
    then if found: cstring name, 3x uint8(0) (name2/3/4, always empty),
    cstring subname (Title), cstring cursorName, uint32 flags, uint32
    creatureType, uint32 creatureFamily, uint32 classification (rank),
    uint32 killCredit[2], uint32 displayId[4], float hpMulti, float
    energyMulti, uint8 leader, uint32 questItems[6], uint32 movementInfoId.
    """
    raw_entry = pk.u32(payload, 0); off = 4
    found = not (raw_entry & 0x80000000)
    entry = raw_entry & 0x7FFFFFFF
    info = {"entry": entry, "found": found}
    if not found:
        return info

    info["name"], off = pk.cstring(payload, off)
    off += 3  # name2, name3, name4 — always empty uint8(0) each
    info["subname"], off = pk.cstring(payload, off)
    info["cursor_name"], off = pk.cstring(payload, off)
    info["flags"] = pk.u32(payload, off); off += 4
    creature_type = pk.u32(payload, off); off += 4
    info["creature_type"] = creature_type
    info["creature_type_name"] = CREATURE_TYPE_NAMES.get(creature_type)
    info["creature_family"] = pk.u32(payload, off); off += 4
    classification = pk.u32(payload, off); off += 4
    info["rank"] = CREATURE_RANK_NAMES.get(classification, f"rank_{classification}")
    off += 4 * MAX_KILL_CREDIT
    off += 4 * MAX_CREATURE_MODELS
    off += 4 * 2  # hp_multi, energy_multi (float, float) — not needed by callers yet
    off += 1  # leader (uint8/bool) — not needed by callers yet
    off += 4 * MAX_CREATURE_QUEST_ITEMS
    off += 4  # movement_info_id — not needed by callers yet

    if off != len(payload):
        raise ValueError(f"parsed {off} of {len(payload)} bytes ({len(payload) - off} leftover)")
    return info


def parse_gameobject_query_response(payload: bytes) -> dict:
    """SMSG_GAMEOBJECT_QUERY_RESPONSE (0x05F): WorldPackets::Query::
    QueryGameObjectResponse::Write. uint32 (entry | (found ? 0 : 0x80000000)),
    then if found: uint32 type, uint32 displayId, cstring name, 3x uint8(0)
    (name2/3/4), cstring iconName, cstring castBarCaption, cstring unkString,
    uint32 data[24] (type-specific params — not decoded here), float size,
    uint32 questItems[6].
    """
    raw_entry = pk.u32(payload, 0); off = 4
    found = not (raw_entry & 0x80000000)
    entry = raw_entry & 0x7FFFFFFF
    info = {"entry": entry, "found": found}
    if not found:
        return info

    info["type"] = pk.u32(payload, off); off += 4
    info["display_id"] = pk.u32(payload, off); off += 4
    info["name"], off = pk.cstring(payload, off)
    off += 3  # name2, name3, name4 — always empty
    info["icon_name"], off = pk.cstring(payload, off)
    info["cast_bar_caption"], off = pk.cstring(payload, off)
    _, off = pk.cstring(payload, off)  # unk_string — not needed by callers yet
    off += 4 * MAX_GAMEOBJECT_DATA
    info["size"] = pk.f32(payload, off); off += 4
    off += 4 * MAX_GAMEOBJECT_QUEST_ITEMS

    if off != len(payload):
        raise ValueError(f"parsed {off} of {len(payload)} bytes ({len(payload) - off} leftover)")
    return info


class NameCache:
    """Per-kind name dicts, query dedupe/budgeting, and on-disk persistence
    for the static creature/gameobject templates (player names are session-
    only — a player's name never changes, but caching it across sessions
    buys nothing and the file would grow without bound).

    Not thread-safe on its own; agent/perception/world.py::WorldState (which owns
    an instance) guards every call with its own lock.
    """

    def __init__(self, budget_per_second: int = 10, clock=time.monotonic,
                 cache_path: str = DEFAULT_CACHE_PATH):
        self.players: dict[int, str] = {}
        self.creatures: dict[int, dict | None] = {}     # entry -> parsed dict, or None if the server said "not found"
        self.gameobjects: dict[int, dict | None] = {}
        self._in_flight: set[tuple[str, int]] = set()
        self._pending: collections.deque = collections.deque()  # (kind, key, sample_guid)
        self._sent_times: collections.deque = collections.deque()
        self._budget_per_second = budget_per_second
        self._clock = clock
        self.cache_path = cache_path  # used by load()/save() when no path is given — tests override this
        self.duplicate_queries = 0  # debug counter: a want_*() for something already known or in-flight

    def want_player(self, guid: int):
        self._want("player", guid, guid)

    def want_creature(self, entry: int, sample_guid: int):
        self._want("creature", entry, sample_guid)

    def want_gameobject(self, entry: int, sample_guid: int):
        self._want("gameobject", entry, sample_guid)

    def _want(self, kind: str, key: int, sample_guid: int):
        cache = getattr(self, kind + "s")
        if key in cache:
            return
        token = (kind, key)
        if token in self._in_flight:
            self.duplicate_queries += 1
            return
        self._in_flight.add(token)
        self._pending.append((kind, key, sample_guid))

    def drain(self, max_items: int | None = None) -> list[tuple[str, int, int]]:
        """Pop up to the current send budget's worth of pending queries.
        Callers (agent/session.py) send one packet per returned item."""
        now = self._clock()
        while self._sent_times and now - self._sent_times[0] >= 1.0:
            self._sent_times.popleft()
        available = self._budget_per_second - len(self._sent_times)
        if max_items is not None:
            available = min(available, max_items)
        out = []
        while available > 0 and self._pending:
            item = self._pending.popleft()
            out.append(item)
            self._sent_times.append(now)
            available -= 1
        return out

    def on_name_query_response(self, data: dict):
        self._in_flight.discard(("player", data["guid"]))
        self.players[data["guid"]] = data["name"] if data["found"] else None

    def on_creature_query_response(self, data: dict):
        self._in_flight.discard(("creature", data["entry"]))
        self.creatures[data["entry"]] = data if data["found"] else None
        if data["found"]:
            self.save()

    def on_gameobject_query_response(self, data: dict):
        self._in_flight.discard(("gameobject", data["entry"]))
        self.gameobjects[data["entry"]] = data if data["found"] else None
        if data["found"]:
            self.save()

    def load(self, path: str | None = None):
        """Best-effort: a missing or corrupt cache file just means starting
        cold, not a fatal error. `path` overrides self.cache_path for this
        call only (tests use this — production code always uses the path
        set at construction)."""
        try:
            with open(path or self.cache_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        self.creatures.update({int(k): v for k, v in data.get("creatures", {}).items()})
        self.gameobjects.update({int(k): v for k, v in data.get("gameobjects", {}).items()})

    def save(self, path: str | None = None):
        """A save failure (read-only fs, disk full) must not crash the
        agent — the cache is a speed-up, not a source of truth. `path`
        overrides self.cache_path for this call only, same as load()."""
        path = path or self.cache_path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + f".tmp{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump({"creatures": self.creatures, "gameobjects": self.gameobjects}, f)
            os.replace(tmp, path)
        except OSError:
            pass
