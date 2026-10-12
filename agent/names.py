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
import contextlib
import logging
import os
import struct
import time

from . import persist
from .opcodes import (
    SMSG_CREATURE_QUERY_RESPONSE,
    SMSG_GAMEOBJECT_QUERY_RESPONSE,
    SMSG_NAME_QUERY_RESPONSE,
)
from .wire import Reader

log = logging.getLogger("agent.names")

DEFAULT_CACHE_PATH = os.path.expanduser("~/.cache/wow-agent/names.json")
CACHE_SCHEMA = 2   # 2: names decoded as UTF-8 (#666); a schema-1 file may hold U+FFFD and is not read

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

# #569: a query with no answer (dropped, or never served) must not pin its slot forever.
QUERY_TIMEOUT_S = 30.0  # a sent query older than this is forgotten and may be asked again
MAX_PENDING = 512       # queued (not yet sent) queries; over this the oldest is dropped


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
    r = Reader(payload, SMSG_NAME_QUERY_RESPONSE)
    guid = r.packed_guid()
    result = r.u8()
    info = {"guid": guid, "found": result == 0}
    if not info["found"]:
        return info
    info["name"] = r.cstring()
    info["realm"] = r.cstring()
    info["race"] = r.u8()
    info["sex"] = r.u8()
    info["class_"] = r.u8()
    info["has_declined_names"] = bool(r.u8())
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
    r = Reader(payload, SMSG_CREATURE_QUERY_RESPONSE)
    raw_entry = r.u32()
    found = not (raw_entry & 0x80000000)
    entry = raw_entry & 0x7FFFFFFF
    info = {"entry": entry, "found": found}
    if not found:
        return info

    info["name"] = r.cstring()
    r.skip(3)  # name2, name3, name4 — always empty uint8(0) each
    info["subname"] = r.cstring()
    info["cursor_name"] = r.cstring()
    info["flags"] = r.u32()
    creature_type = r.u32()
    info["creature_type"] = creature_type
    info["creature_type_name"] = CREATURE_TYPE_NAMES.get(creature_type)
    info["creature_family"] = r.u32()
    classification = r.u32()
    info["rank"] = CREATURE_RANK_NAMES.get(classification, f"rank_{classification}")
    r.skip(4 * MAX_KILL_CREDIT)
    r.skip(4 * MAX_CREATURE_MODELS)
    r.skip(4 * 2)  # hp_multi, energy_multi (float, float) — not needed by callers yet
    r.skip(1)  # leader (uint8/bool) — not needed by callers yet
    r.skip(4 * MAX_CREATURE_QUEST_ITEMS)
    r.skip(4)  # movement_info_id — not needed by callers yet

    r.done()
    return info


def parse_gameobject_query_response(payload: bytes) -> dict:
    """SMSG_GAMEOBJECT_QUERY_RESPONSE (0x05F): WorldPackets::Query::
    QueryGameObjectResponse::Write. uint32 (entry | (found ? 0 : 0x80000000)),
    then if found: uint32 type, uint32 displayId, cstring name, 3x uint8(0)
    (name2/3/4), cstring iconName, cstring castBarCaption, cstring unkString,
    uint32 data[24] (type-specific params — not decoded here), float size,
    uint32 questItems[6].
    """
    r = Reader(payload, SMSG_GAMEOBJECT_QUERY_RESPONSE)
    raw_entry = r.u32()
    found = not (raw_entry & 0x80000000)
    entry = raw_entry & 0x7FFFFFFF
    info = {"entry": entry, "found": found}
    if not found:
        return info

    info["type"] = r.u32()
    info["display_id"] = r.u32()
    info["name"] = r.cstring()
    r.skip(3)  # name2, name3, name4 — always empty
    info["icon_name"] = r.cstring()
    info["cast_bar_caption"] = r.cstring()
    r.cstring()  # unk_string — not needed by callers yet
    r.skip(4 * MAX_GAMEOBJECT_DATA)
    info["size"] = r.f32()
    r.skip(4 * MAX_GAMEOBJECT_QUEST_ITEMS)

    r.done()
    return info


# Keys every cached object must carry, with the exact type (type(), not isinstance: True is not an int).
_CREATURE_REQUIRED: dict[str, type] = {"name": str}
_GAMEOBJECT_REQUIRED: dict[str, type] = {"name": str, "type": int}


def _parse_cache_section(section, required: dict[str, type]) -> dict[int, dict | None]:
    """One kind's section of the cache file: {"<entry>": parsed dict or null}. A null entry is a
    cached "not found". An object entry must carry every `required` key with that type; the
    readers index those keys without checking. Raises ValueError on any other shape, so load()
    drops the whole file."""
    if not isinstance(section, dict):
        raise ValueError(f"cache section is {type(section).__name__}, expected an object")
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


class NameCache:
    """Per-kind name dicts, query dedupe/budgeting, and on-disk persistence
    for the static creature/gameobject templates (player names are session-
    only — a player's name never changes, but caching it across sessions
    buys nothing and the file would grow without bound).

    Not thread-safe on its own. Threading rule: every call, drain() included, is made
    under agent/perception/world.py::WorldState's lock (want_*/on_*_response, and drain()
    via ResolutionMixin.drain_name_queries, called by agent/handlers/world.py
    send_name_queries). The one exception is load(): agent/session.py calls it from
    login_character before CMSG_PLAYER_LOGIN is sent and before the recv thread starts.
    Other threads already run (chat-relay, HTTP observer) but none can reach this
    session's WorldState yet: the relay gets its resolver from a received chat packet. The
    recv thread and the chat-relay thread share this cache. The expiry sweep still
    iterates a snapshot of _in_flight and tolerates a token already removed, which keeps
    that sweep safe if the class is used alone.
    """

    def __init__(self, budget_per_second: int = 10, clock=time.monotonic,
                 cache_path: str = DEFAULT_CACHE_PATH):
        self.players: dict[int, str] = {}
        self.creatures: dict[int, dict | None] = {}     # entry -> parsed dict, or None if the server said "not found"
        self.gameobjects: dict[int, dict | None] = {}
        # token -> clock time it was sent by drain(), or None while it waits in _pending
        self._in_flight: dict[tuple[str, int], float | None] = {}
        self._pending: collections.deque = collections.deque()  # (kind, key, sample_guid)
        self._sent_times: collections.deque = collections.deque()
        self._budget_per_second = budget_per_second
        self._clock = clock
        self.cache_path = cache_path  # used by load()/save() when no path is given — tests override this
        self._no_save: set[str] = set()  # paths whose file is of a newer schema: save() leaves them alone
        self.duplicate_queries = 0  # debug counter: a want_*() for something already known or in-flight
        self.expired_queries = 0  # sent queries forgotten after QUERY_TIMEOUT_S with no answer
        self.dropped_queries = 0  # queued queries pushed out by the MAX_PENDING cap

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
        if len(self._pending) >= MAX_PENDING:
            old_kind, old_key, _ = self._pending.popleft()
            self._in_flight.pop((old_kind, old_key), None)
            self.dropped_queries += 1
        self._in_flight[token] = None
        self._pending.append((kind, key, sample_guid))

    def drain(self, max_items: int | None = None) -> list[tuple[str, int, int]]:
        """Pop up to the current send budget's worth of pending queries.
        Callers (agent/handlers/world.py send_name_queries, via WorldState.drain_name_queries)
        send one packet per returned item. Also
        forgets every sent query older than QUERY_TIMEOUT_S (one pass, no sort)
        so a later want_*() asks again."""
        now = self._clock()
        while self._sent_times and now - self._sent_times[0] >= 1.0:
            self._sent_times.popleft()
        # Under WorldState the lock serialises drain() with want_*() (drain_name_queries
        # holds it; the chat-relay thread calls want_*()). The sweep still iterates a
        # snapshot and tolerates a token already removed, so the sweep itself does not
        # break if the class is used on its own (the class as a whole is still not
        # thread-safe alone; see the docstring).
        expired = [token for token, sent in list(self._in_flight.items())
                   if sent is not None and now - sent >= QUERY_TIMEOUT_S]
        for token in expired:
            self._in_flight.pop(token, None)
        self.expired_queries += len(expired)
        available = self._budget_per_second - len(self._sent_times)
        if max_items is not None:
            available = min(available, max_items)
        out = []
        while available > 0 and self._pending:
            item = self._pending.popleft()
            out.append(item)
            token = (item[0], item[1])
            if token in self._in_flight:  # gone already if its answer arrived before the send
                self._in_flight[token] = now
            self._sent_times.append(now)
            available -= 1
        return out

    def on_name_query_response(self, data: dict):
        self._in_flight.pop(("player", data["guid"]), None)
        self.players[data["guid"]] = data["name"] if data["found"] else None

    def on_creature_query_response(self, data: dict):
        self._in_flight.pop(("creature", data["entry"]), None)
        self.creatures[data["entry"]] = data if data["found"] else None
        if data["found"]:
            self.save()

    def on_gameobject_query_response(self, data: dict):
        self._in_flight.pop(("gameobject", data["entry"]), None)
        self.gameobjects[data["entry"]] = data if data["found"] else None
        if data["found"]:
            self.save()

    def load(self, path: str | None = None):
        """Best-effort: a missing, unreadable or newer cache file just means starting
        cold, never a fatal error. `path` overrides self.cache_path for this call only
        (tests use this; production code always uses the path set at construction).
        ADR 0018: a file of a newer schema is left alone and saving to it is switched off;
        an unreadable one is moved to <path>.corrupt and the next save writes a fresh file.
        All or nothing: either every section converts or none is kept."""
        path = path or self.cache_path
        loaded = persist.read_versioned(path, CACHE_SCHEMA)
        if loaded.status == persist.MISSING:
            return
        if loaded.status == persist.NEWER:
            log.warning("name cache %s is schema %d, newer than %d: not read, not overwritten",
                        path, loaded.version, CACHE_SCHEMA)
            self._no_save.add(path)
            return
        if loaded.status == persist.OLDER:
            return
        if loaded.status == persist.OK:
            try:
                creatures = _parse_cache_section(loaded.data.get("creatures", {}), _CREATURE_REQUIRED)
                gameobjects = _parse_cache_section(loaded.data.get("gameobjects", {}), _GAMEOBJECT_REQUIRED)
            except ValueError as e:
                log.warning("ignoring unreadable name cache %s: %s", path, str(e)[:200])
                persist.set_aside(path)
                return
            self.creatures.update(creatures)
            self.gameobjects.update(gameobjects)
            return
        log.warning("ignoring unreadable name cache %s", path)
        persist.set_aside(path)

    def save(self, path: str | None = None):
        """A save failure (read-only fs, disk full) must not crash the
        agent — the cache is a speed-up, not a source of truth. `path`
        overrides self.cache_path for this call only, same as load(). Does nothing
        for a path whose file load() found to be of a newer schema."""
        path = path or self.cache_path
        if path in self._no_save:
            return
        with contextlib.suppress(OSError):
            persist.write_versioned(path, CACHE_SCHEMA,
                                    {"creatures": self.creatures, "gameobjects": self.gameobjects})
