"""One perceived object (ObjectInfo) and the flag constants it is read with.
Every flag constant is cited against TrinityCore branch `3.3.5`."""

import math
import time
from dataclasses import dataclass, field


UNIT_NPC_FLAG_GOSSIP = 0x00000001
UNIT_NPC_FLAG_QUESTGIVER = 0x00000002
UNIT_NPC_FLAG_TRAINER = 0x00000010
UNIT_NPC_FLAG_VENDOR = 0x00000080
UNIT_NPC_FLAG_MAILBOX = 0x04000000  # a rare custom mailbox NPC — see agent/mail.py's docstring; the usual case is a gameobject
GAMEOBJECT_TYPE_MAILBOX = 19  # SharedDefines.h GameobjectTypes — the usual mailbox case (a gameobject, not an NPC)

# UnitDynFlags (src/server/shared/SharedDefines.h)
UNIT_DYNFLAG_LOOTABLE = 0x0001

# NPCFlags (UnitDefines.h) — spirit healer/guide, used by UM-43's spirit-healer fallback
UNIT_NPC_FLAG_SPIRITHEALER = 0x00004000
UNIT_NPC_FLAG_SPIRITGUIDE = 0x00008000

# PlayerFlags (src/server/game/Entities/Player/Player.h) — UM-43's death/ghost detection
PLAYER_FLAGS_GHOST = 0x00000010

# UnitDefines.h POWER_MANA (enum Powers) — used by UM-43's rest reflex to tell
# "no mana bar" classes (warrior/rogue/...) from "low on mana" ones.
POWER_MANA = 0


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
    name: str = ""  # filled by name resolution (UM-35) once a query response arrives
    subname: str = ""  # creature "title" (e.g. "Guard Captain") — creatures/players only
    rank: str = ""  # CreatureEliteType name ("elite", "rareelite", "worldboss", "rare") — creatures only, "" = normal
    creature_type: str | None = None  # CreatureType name ("beast", "humanoid", ...) — creatures only
    gameobject_type: int | None = None  # SMSG_GAMEOBJECT_QUERY_RESPONSE's `type` (GameObjectTypes, SharedDefines.h) — gameobjects only, None until queried
    level: int | None = None
    health: int | None = None
    max_health: int | None = None
    power: dict = field(default_factory=dict)  # {Powers name: value} — agent.update_fields.POWER_NAMES, UM-70
    max_power: dict = field(default_factory=dict)
    faction: int | None = None
    unit_flags: int | None = None
    dynamic_flags: int | None = None
    npc_flags: int | None = None
    player_flags: int | None = None  # PLAYER_FLAGS — players only; used by is_ghost() (UM-43)
    quest_giver_status: int | None = None  # SMSG_QUESTGIVER_STATUS (UM-41); None = not yet queried
    quest_giver_status_name: str | None = None
    power_type: int | None = None  # Powers enum (0=mana, 1=rage, 2=focus, 3=energy, ...) — UNIT_FIELD_BYTES_0
    target_guid: int | None = None
    position: tuple | None = None  # (map, x, y, z, o) — map is filled in by WorldState (blocks don't carry it)
    move_flags: int | None = None
    speeds: tuple | None = None  # 9 floats, UnitMoveType order (UnitDefines.h): walk, run, run_back, swim, swim_back, turn_rate, flight, flight_back, pitch_rate — only present on a LIVING movement block
    last_update: float = 0.0  # time.monotonic() of the last block that touched this object
    raw_fields: dict = field(default_factory=dict)
    spline: dict | None = None  # active spline (UM-64): {start_pos, destination, start_time, duration}

    def __repr__(self):
        return f"<{self.object_type or 'object'} #{self.guid:x} {self.name or self.entry or '?'}>"

    def is_player(self) -> bool:
        return self.object_type == "player"

    def is_dead(self) -> bool:
        return self.health == 0

    def is_ghost(self) -> bool:
        """PLAYER_FLAGS_GHOST (UM-43) — true from release_spirit() until the
        agent resurrects (corpse reclaim or spirit healer). Distinct from
        is_dead(): BuildPlayerRepop (Player.cpp) sets health back to 1 the
        moment the ghost state begins, so is_dead() alone would miss the
        whole corpse-run window."""
        return bool(self.player_flags and (self.player_flags & PLAYER_FLAGS_GHOST))

    def is_spirit_healer(self) -> bool:
        return bool(self.npc_flags and (self.npc_flags & UNIT_NPC_FLAG_SPIRITHEALER))

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

    def is_mailbox(self) -> bool:
        """UM-60: a gameobject whose queried template type is
        GAMEOBJECT_TYPE_MAILBOX (the usual case — unknown/None until its
        SMSG_GAMEOBJECT_QUERY_RESPONSE arrives, same lazy-resolution as
        name), or a unit/player with the rare UNIT_NPC_FLAG_MAILBOX set."""
        if self.gameobject_type == GAMEOBJECT_TYPE_MAILBOX:
            return True
        return bool(self.npc_flags) and bool(self.npc_flags & UNIT_NPC_FLAG_MAILBOX)

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
