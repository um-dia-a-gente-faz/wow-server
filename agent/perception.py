#!/usr/bin/env python3
"""World state: a live, thread-safe model of what the agent perceives.

Built from agent.update_object.UpdateBlock (block framing + movement, UM-32)
and agent.update_fields.decode_fields (VALUES field mapping, UM-33). Every
flag constant is cited against TrinityCore branch `3.3.5`.
"""

import math
import threading
import time
from dataclasses import dataclass, field

from . import update_fields as uf
from . import update_object as uo

# NPCFlags (UnitDefines.h)
UNIT_NPC_FLAG_GOSSIP = 0x00000001
UNIT_NPC_FLAG_QUESTGIVER = 0x00000002
UNIT_NPC_FLAG_TRAINER = 0x00000010
UNIT_NPC_FLAG_VENDOR = 0x00000080

# UnitDynFlags (src/server/shared/SharedDefines.h)
UNIT_DYNFLAG_LOOTABLE = 0x0001


class PerceptionParseError(Exception):
    """A server packet could not be parsed into world state.

    Raised for truncated or malformed update-object data. The session drops
    the offending packet and keeps the connection alive.
    """


@dataclass
class ObjectInfo:
    guid: int
    object_type: str = ""  # uo.OBJECT_TYPE_NAMES value: unit/player/gameobject/... ("" until first CREATE)
    type_id: int | None = None  # uo.TYPEID_* — kept alongside object_type so VALUES-only merges can still decode_fields correctly
    entry: int | None = None
    name: str = ""  # filled by name resolution (UM-35); always "" for now
    level: int | None = None
    health: int | None = None
    max_health: int | None = None
    power: list = field(default_factory=list)
    max_power: list = field(default_factory=list)
    faction: int | None = None
    unit_flags: int | None = None
    dynamic_flags: int | None = None
    npc_flags: int | None = None
    target_guid: int | None = None
    position: tuple | None = None  # (map, x, y, z, o) — map is filled in by WorldState (blocks don't carry it)
    move_flags: int | None = None
    last_update: float = 0.0  # time.monotonic() of the last block that touched this object
    raw_fields: dict = field(default_factory=dict)
    spline: dict | None = None  # active spline (UM-64): {start_pos, destination, start_time, duration}

    def __repr__(self):
        return f"<{self.object_type or 'object'} #{self.guid:x} {self.name or self.entry or '?'}>"

    def is_player(self) -> bool:
        return self.object_type == "player"

    def is_dead(self) -> bool:
        return self.health == 0

    def is_lootable(self) -> bool:
        return bool(self.dynamic_flags) and bool(self.dynamic_flags & UNIT_DYNFLAG_LOOTABLE)

    def is_quest_giver(self) -> bool:
        return bool(self.npc_flags) and bool(self.npc_flags & UNIT_NPC_FLAG_QUESTGIVER)

    def is_vendor(self) -> bool:
        return bool(self.npc_flags) and bool(self.npc_flags & UNIT_NPC_FLAG_VENDOR)

    def is_gossip(self) -> bool:
        return bool(self.npc_flags) and bool(self.npc_flags & UNIT_NPC_FLAG_GOSSIP)

    def is_trainer(self) -> bool:
        return bool(self.npc_flags) and bool(self.npc_flags & UNIT_NPC_FLAG_TRAINER)

    def set_spline(self, start_pos: tuple, destination: tuple, start_time: float, duration: float):
        """Start (or replace) this object's spline-interpolation state.
        `start_pos`/`destination` are (x, y, z); `start_time` is a
        time.monotonic() timestamp; `duration` is in seconds."""
        self.spline = {"start_pos": start_pos, "destination": destination,
                        "start_time": start_time, "duration": duration}

    def clear_spline(self):
        self.spline = None

    def current_position(self) -> tuple | None:
        """self.position, or — while a spline is in flight (UM-64) — a
        straight-line interpolation toward its destination. This is a
        "simple interpolation over the spline duration", not a real spline
        evaluation (no easing, no intermediate waypoints): good enough to
        keep a moving NPC's reported position from going stale for the ~1s
        between its own update-object/MONSTER_MOVE ticks. Once `duration`
        elapses the interpolation clamps at the destination, which also
        covers "arrived and stopped" without needing an explicit signal."""
        if self.spline is None or self.position is None:
            return self.position
        duration = self.spline["duration"]
        if duration <= 0:
            return self.position
        t = max(0.0, min(1.0, (time.monotonic() - self.spline["start_time"]) / duration))
        sx, sy, sz = self.spline["start_pos"]
        dx, dy, dz = self.spline["destination"]
        map_id, _, _, _, o = self.position
        return (map_id, sx + (dx - sx) * t, sy + (dy - sy) * t, sz + (dz - sz) * t, o)

    def distance_to(self, pos) -> float | None:
        """3D distance to (x, y, z) or (map, x, y, z, ...), from this
        object's current (possibly spline-interpolated) position. None if
        either position is unknown, or they're on different maps."""
        my_pos = self.current_position()
        if my_pos is None:
            return None
        if len(pos) >= 4:
            map_id, x, y, z = pos[0], pos[1], pos[2], pos[3]
            if map_id != my_pos[0]:
                return None
        else:
            x, y, z = pos
        _, sx, sy, sz, _ = my_pos
        return math.sqrt((sx - x) ** 2 + (sy - y) ** 2 + (sz - z) ** 2)

    def is_hostile_to(self, my_faction: int | None) -> bool | None:
        """v1: unknown unless we know both factions and they differ. Real
        hostility depends on faction_template.dbc (enemy/friend masks,
        reputation) which this agent doesn't have loaded — this is a coarse
        "different faction" guess, not authoritative. None means unknown."""
        if my_faction is None or self.faction is None:
            return None
        return self.faction != my_faction


class WorldState:
    """Thread-safe container for perceived game objects.

    Every dict is keyed by the 64-bit GUID as an int. All mutation happens
    through update_object()/remove_guids() from the recv thread; snapshot()
    and the getters are safe to call from the main loop concurrently.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.objects: dict[int, ObjectInfo] = {}
        self.my_guid = 0
        self.my_map = None  # set by session.py from SMSG_LOGIN_VERIFY_WORLD
        self.unknown_field_updates = 0  # debug counter: VALUES for a guid we haven't CREATEd yet

    def set_my_guid(self, guid: int):
        """Remember which GUID is our own character. Does not create an object;
        our player shows up through its update-object block like anything else."""
        with self._lock:
            self.my_guid = guid

    def set_my_map(self, map_id: int):
        """Remember our current map ID (SMSG_LOGIN_VERIFY_WORLD) — update-object
        blocks carry x/y/z/o but never a map ID of their own."""
        with self._lock:
            self.my_map = map_id

    def get_objects(self) -> dict:
        with self._lock:
            return dict(self.objects)

    def get_object(self, guid: int) -> ObjectInfo | None:
        with self._lock:
            return self.objects.get(guid)

    def get_my_object(self) -> ObjectInfo | None:
        with self._lock:
            return self.objects.get(self.my_guid) if self.my_guid else None

    def record_guid(self, guid: int, update_type: int):
        """Legacy/minimal path: track that a GUID exists, nothing else.
        Superseded by update_object() for real blocks; kept for callers
        (and tests) that only care about GUID presence."""
        with self._lock:
            if guid not in self.objects:
                self.objects[guid] = ObjectInfo(guid=guid)

    def update_object(self, block: "uo.UpdateBlock"):
        """Apply one parsed UpdateBlock (agent/update_object.py) to world state.

        CREATE_OBJECT[2]: create-or-replace (a fresh object_type + movement +
        fields snapshot — the server only sends CREATE for objects the client
        doesn't already know about, or a resend after OUT_OF_RANGE).
        VALUES: merge fields into an existing object; if the GUID is unknown,
        bump `unknown_field_updates` and ignore (we have nowhere to put a
        partial update — a CREATE must come first, per the protocol).
        MOVEMENT: update position/move_flags on an existing object; same
        ignore-and-count policy if unknown.
        """
        with self._lock:
            if block.update_type in (uo.UPDATETYPE_CREATE_OBJECT, uo.UPDATETYPE_CREATE_OBJECT2):
                obj = self.objects.get(block.guid) or ObjectInfo(guid=block.guid)
                obj.type_id = block.object_type
                obj.object_type = uo.OBJECT_TYPE_NAMES.get(block.object_type, "")
                self._apply_movement(obj, block.movement)
                self._apply_fields(obj, block.object_type, block.fields)
                obj.last_update = time.monotonic()
                self.objects[block.guid] = obj
                return

            obj = self.objects.get(block.guid)
            if obj is None:
                self.unknown_field_updates += 1
                return

            if block.update_type == uo.UPDATETYPE_VALUES:
                self._apply_fields(obj, obj.type_id, block.fields)
            elif block.update_type == uo.UPDATETYPE_MOVEMENT:
                self._apply_movement(obj, block.movement)
            obj.last_update = time.monotonic()

    def apply_monster_move(self, info: dict):
        """SMSG_MONSTER_MOVE (agent.update_object.parse_monster_move):
        (re)start an NPC's spline-interpolation state, or — for
        MONSTER_MOVE_STOP / a spline with no destination — snap straight to
        its reported position. Same unknown-guid policy as update_object():
        a MONSTER_MOVE for a GUID we haven't CREATEd yet is ignored and
        counted, we'd have nowhere to put it."""
        with self._lock:
            obj = self.objects.get(info["mover_guid"])
            if obj is None:
                self.unknown_field_updates += 1
                return
            px, py, pz = info["pos"]
            o = obj.position[4] if obj.position else 0.0
            obj.position = (self.my_map, px, py, pz, o)
            if info["move_type"] == uo.MONSTER_MOVE_STOP or "destination" not in info:
                obj.clear_spline()
            else:
                obj.set_spline(info["pos"], info["destination"], time.monotonic(),
                                info["move_time"] / 1000.0)
            obj.last_update = time.monotonic()

    def apply_movement_info(self, guid: int, move_info: dict):
        """MSG_MOVE_* broadcasts (agent.update_object.parse_movement_info):
        a plain position update for an object we already know about. Same
        unknown-guid policy as update_object()."""
        with self._lock:
            obj = self.objects.get(guid)
            if obj is None:
                self.unknown_field_updates += 1
                return
            self._apply_movement(obj, move_info)
            obj.last_update = time.monotonic()

    def remove_guids(self, guids):
        """OUT_OF_RANGE_OBJECTS: the server is telling us these are no
        longer visible. Drop them entirely rather than mark them stale —
        we'd just be guessing at position/health from here on."""
        with self._lock:
            for guid in guids:
                self.objects.pop(guid, None)

    def _apply_movement(self, obj: ObjectInfo, movement: dict | None):
        if not movement:
            return
        if "x" in movement:
            # Every object the server tells us about is on our current map
            # (or instance) — update-object blocks never carry a map ID of
            # their own, so we borrow the self object's.
            obj.position = (self.my_map, movement["x"], movement["y"], movement["z"], movement.get("o", 0.0))
        if "move_flags" in movement:
            obj.move_flags = movement["move_flags"]
        if "target_guid" in movement:
            obj.target_guid = movement["target_guid"]

        spline = movement.get("spline")
        if spline is not None:
            obj.set_spline(
                (movement["x"], movement["y"], movement["z"]),
                spline["destination"],
                time.monotonic() - spline["time_passed"] / 1000.0,
                spline["duration"] / 1000.0,
            )
        elif "move_flags" in movement:
            # A fresh LIVING block without spline data (direct control, or
            # the spline finished) supersedes any earlier spline. POSITION/
            # STATIONARY_POSITION blocks never carry move_flags at all, so
            # they leave existing spline state alone rather than guess.
            obj.clear_spline()

    def _apply_fields(self, obj: ObjectInfo, object_type: int | None, raw_fields: dict | None):
        if not raw_fields:
            return
        obj.raw_fields.update(raw_fields)
        decoded = uf.decode_fields(object_type if object_type is not None else -1, raw_fields)
        for attr in ("entry", "level", "health", "max_health", "faction",
                     "unit_flags", "dynamic_flags", "npc_flags", "target_guid"):
            if attr in decoded:
                setattr(obj, attr, decoded[attr])
        if "power" in decoded:
            obj.power = decoded["power"]
        if "max_power" in decoded:
            obj.max_power = decoded["max_power"]

    def snapshot(self, my_position=None, max_range: float = 50.0, limit: int = 40) -> dict:
        """A JSON-serialisable view shaped like docs/AI-AGENT-SPEC.md's
        `GET /agent/{id}/perception`: position, nearby_units, nearby_players,
        nearby_objects, sorted by distance and capped at `limit` each.

        `my_position` overrides the self object's own recorded position
        (useful right after login, before any update-object block has
        arrived for self) — defaults to the self object's position.
        """
        with self._lock:
            objects = list(self.objects.values())
            me = self.objects.get(self.my_guid)

        pos = my_position or (me.position if me else None)
        out = {
            "position": _position_dict(pos),
            "nearby_units": [],
            "nearby_players": [],
            "nearby_objects": [],
        }
        if pos is None:
            return out

        scored = []
        for obj in objects:
            if obj.guid == self.my_guid:
                continue
            dist = obj.distance_to(pos)
            if dist is None or dist > max_range:
                continue
            scored.append((dist, obj))
        scored.sort(key=lambda pair: pair[0])

        for dist, obj in scored:
            entry = _object_dict(obj, dist)
            if obj.object_type == "player":
                bucket = out["nearby_players"]
            elif obj.object_type == "unit":
                bucket = out["nearby_units"]
            else:
                bucket = out["nearby_objects"]
            if len(bucket) < limit:
                bucket.append(entry)

        return out


def _position_dict(pos):
    if pos is None:
        return None
    map_id, x, y, z, *_o = pos
    return {"map": map_id, "x": x, "y": y, "z": z}


def _object_dict(obj: ObjectInfo, distance: float) -> dict:
    d = {
        "guid": obj.guid,
        "entry": obj.entry,
        "name": obj.name or None,
        "type": obj.object_type or None,
        "distance": round(distance, 1),
    }
    current_position = obj.current_position()
    if current_position is not None:
        d["position"] = _position_dict(current_position)
    if obj.faction is not None:
        d["faction"] = obj.faction
    if obj.level is not None:
        d["level"] = obj.level
    if obj.health is not None and obj.max_health:
        d["health_pct"] = round(obj.health / obj.max_health, 2) if obj.max_health else None
    if obj.target_guid is not None:
        d["target_guid"] = obj.target_guid
    d["in_combat"] = bool(obj.unit_flags and (obj.unit_flags & 0x00080000))  # UNIT_FLAG_IN_COMBAT, UnitDefines.h
    return d
