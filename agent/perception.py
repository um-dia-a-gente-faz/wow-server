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

from . import items as it
from . import names as nm
from . import npc as npc_mod
from . import quests as qu
from . import update_fields as uf
from . import update_object as uo

# NPCFlags (UnitDefines.h)
UNIT_NPC_FLAG_GOSSIP = 0x00000001
UNIT_NPC_FLAG_QUESTGIVER = 0x00000002
UNIT_NPC_FLAG_TRAINER = 0x00000010
UNIT_NPC_FLAG_VENDOR = 0x00000080

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
        self.names = nm.NameCache()  # UM-35
        self.items = it.ItemCache()  # UM-42: item template cache (names/stats by entry)
        # UM-38: the last position the SERVER reported for us via a real
        # update-object block — distinct from ObjectInfo.position, which
        # update_my_position_from_simulation() also overwrites with our own
        # simulated guess between real updates. Movement's stuck detection
        # needs this untouched-by-simulation value to know whether the
        # server actually agrees with where we think we are.
        self.my_server_position: tuple | None = None
        self.npc_texts = npc_mod.NpcTextCache()  # UM-40: gossip body text, cached like names
        self.quest_texts = qu.QuestCache()  # UM-41: static per-quest-id text/reward cache
        # UM-41: guids we've queued a CMSG_QUESTGIVER_STATUS_QUERY for but
        # haven't heard back on yet — deliberately simple (no persistent
        # cache like names/quest_texts: quest-giver status is per-visit
        # state, e.g. it flips the moment a quest is turned in) since a
        # nearby questgiver's status is cheap to re-query every time it's
        # first perceived.
        self._quest_status_pending: list = []
        self._quest_status_in_flight: set = set()
        # UM-40: "which NPC window is open", read by the LLM via snapshot()'s
        # 'window' key — None (no window), or {"kind": "gossip"|"vendor"|
        # "trainer", **parsed response}.
        self.ui_state: dict | None = None

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

    def get_ui_state(self) -> dict | None:
        """Thread-safe read of the currently open gossip/vendor/trainer
        window (UM-40), for actions.py to check against without racing the
        recv thread's apply_gossip_message/apply_list_inventory/etc."""
        with self._lock:
            return self.ui_state

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
                self._maybe_resolve_name(obj)
                if block.guid == self.my_guid and block.movement and "x" in block.movement:
                    self.my_server_position = obj.position
                return

            obj = self.objects.get(block.guid)
            if obj is None:
                self.unknown_field_updates += 1
                return

            if block.update_type == uo.UPDATETYPE_VALUES:
                self._apply_fields(obj, obj.type_id, block.fields)
                self._maybe_resolve_name(obj)  # entry (OBJECT_FIELD_ENTRY) may have just arrived
            elif block.update_type == uo.UPDATETYPE_MOVEMENT:
                self._apply_movement(obj, block.movement)
            obj.last_update = time.monotonic()
            if block.guid == self.my_guid and block.movement and "x" in block.movement:
                self.my_server_position = obj.position

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

    def _maybe_resolve_name(self, obj: ObjectInfo):
        """UM-35: fill obj.name (etc.) from the cache if already known,
        otherwise enqueue a query. Called with self._lock already held.
        Skips our own object — we already know our name; nothing queries it."""
        if obj.guid == self.my_guid:
            return
        if obj.object_type == "player":
            if obj.guid in self.names.players:
                name = self.names.players[obj.guid]
                if name is not None:
                    obj.name = name
            else:
                self.names.want_player(obj.guid)
        elif obj.object_type == "unit" and obj.entry:
            if obj.entry in self.names.creatures:
                data = self.names.creatures[obj.entry]
                if data is not None:
                    self._apply_creature_name(obj, data)
            else:
                self.names.want_creature(obj.entry, obj.guid)
        elif obj.object_type == "gameobject" and obj.entry:
            if obj.entry in self.names.gameobjects:
                data = self.names.gameobjects[obj.entry]
                if data is not None:
                    obj.name = data["name"]
            else:
                self.names.want_gameobject(obj.entry, obj.guid)
        elif obj.object_type in ("item", "container") and obj.entry:
            if obj.entry in self.items.items:
                data = self.items.items[obj.entry]
                if data is not None:
                    obj.name = data["name"]
            else:
                self.items.want_item(obj.entry)

        # UM-41: any unit/player with the questgiver npc flag and no status
        # yet gets a CMSG_QUESTGIVER_STATUS_QUERY queued, same trigger point
        # as the name/npc-text queries above (called every time a CREATE or
        # a fresh VALUES update touches this object).
        if obj.is_quest_giver() and obj.quest_giver_status is None \
                and obj.guid not in self._quest_status_in_flight:
            self._quest_status_in_flight.add(obj.guid)
            self._quest_status_pending.append(obj.guid)

    def drain_quest_giver_status_queries(self, max_items: int | None = None) -> list:
        """Pop queued questgiver guids to CMSG_QUESTGIVER_STATUS_QUERY —
        drained by session.py once per recv-loop tick, like
        names/npc_texts/quest_texts. No send-rate budget: this queue only
        grows from newly-perceived questgivers, which in practice arrive at
        a trickle, not a flood."""
        with self._lock:
            if max_items is None:
                out, self._quest_status_pending = self._quest_status_pending, []
                return out
            out = self._quest_status_pending[:max_items]
            self._quest_status_pending = self._quest_status_pending[max_items:]
            return out

    @staticmethod
    def _apply_creature_name(obj: ObjectInfo, data: dict):
        obj.name = data["name"]
        obj.subname = data.get("subname", "")
        obj.rank = data.get("rank", "") if data.get("rank") != "normal" else ""
        obj.creature_type = data.get("creature_type_name")

    def apply_name_query_response(self, data: dict):
        """SMSG_NAME_QUERY_RESPONSE (agent.names.parse_name_query_response):
        backfill .name on the matching player object, if it's still around."""
        with self._lock:
            self.names.on_name_query_response(data)
            if data["found"]:
                obj = self.objects.get(data["guid"])
                if obj is not None:
                    obj.name = data["name"]

    def apply_creature_query_response(self, data: dict):
        """SMSG_CREATURE_QUERY_RESPONSE: backfill every currently-known unit
        with this entry (there can be several, e.g. six identical "Shaker"
        NPCs — see agent/tests/fixtures/update_object/README.md)."""
        with self._lock:
            self.names.on_creature_query_response(data)
            if data["found"]:
                for obj in self.objects.values():
                    if obj.object_type == "unit" and obj.entry == data["entry"]:
                        self._apply_creature_name(obj, data)

    def apply_gameobject_query_response(self, data: dict):
        """SMSG_GAMEOBJECT_QUERY_RESPONSE: backfill every currently-known
        gameobject with this entry."""
        with self._lock:
            self.names.on_gameobject_query_response(data)
            if data["found"]:
                for obj in self.objects.values():
                    if obj.object_type == "gameobject" and obj.entry == data["entry"]:
                        obj.name = data["name"]

    # ── Quests (UM-41) ─────────────────────────────────────────────────────

    def apply_questgiver_status(self, data: dict):
        """SMSG_QUESTGIVER_STATUS (agent.quests.parse_questgiver_status):
        backfill the matching NPC's quest_giver_status, exposed to the LLM
        via snapshot()'s nearby_units/nearby_players entries."""
        with self._lock:
            obj = self.objects.get(data["guid"])
            if obj is not None:
                obj.quest_giver_status = data["status"]
                obj.quest_giver_status_name = data["status_name"]
            self._quest_status_in_flight.discard(data["guid"])

    def apply_questgiver_quest_list(self, data: dict):
        """SMSG_QUESTGIVER_QUEST_LIST: opens the questgiver's quest-list
        window (available/offered quests at this NPC)."""
        with self._lock:
            self.ui_state = {"kind": "quest_list", **data}

    def apply_questgiver_quest_details(self, data: dict):
        """SMSG_QUESTGIVER_QUEST_DETAILS: opens a single quest's detail
        view (full text, before accepting)."""
        with self._lock:
            self.ui_state = {"kind": "quest_details", **data}

    def apply_questgiver_request_items(self, data: dict):
        """SMSG_QUESTGIVER_REQUEST_ITEMS: the "turn this quest in" window —
        shown in response to CMSG_QUESTGIVER_COMPLETE_QUEST when the
        server wants confirmation of the required items/money before
        offering the reward."""
        with self._lock:
            self.ui_state = {"kind": "quest_request_items", **data}

    def apply_questgiver_offer_reward(self, data: dict):
        """SMSG_QUESTGIVER_OFFER_REWARD: the reward-choice window —
        CMSG_QUESTGIVER_CHOOSE_REWARD (turn_in_quest) reads its
        reward_choice_items from here."""
        with self._lock:
            self.ui_state = {"kind": "quest_offer_reward", **data}

    def apply_quest_query_response(self, data: dict):
        """SMSG_QUEST_QUERY_RESPONSE (agent.quests.parse_quest_query_response):
        cache this quest's static text/reward data — consumed by
        build_quest_log() below to enrich quest_log entries with title/
        objective text and used by any window that only carries a bare
        quest id."""
        with self._lock:
            self.quest_texts.on_response(data)

    def build_quest_log(self) -> list:
        """UM-41: the self player's quest log (agent.update_fields.
        decode_quest_log) enriched with cached quest text/rewards (title,
        objectives) and a best-effort human-readable progress string per
        objective, e.g. "Mana Wyrm slain: 3/8" — built from the quest log's
        raw counters against the cached quest's required_credit/
        required_items counts (agent.quests.QuestCache), when known.
        Queues a CMSG_QUEST_QUERY for any quest id seen that isn't cached
        yet (drained by session.py like the name/npc-text caches)."""
        with self._lock:
            me = self.objects.get(self.my_guid)
            if me is None:
                return []
            slots = uf.decode_quest_log(me.raw_fields)
            out = []
            for slot in slots:
                quest_id = slot["quest_id"]
                cached = self.quest_texts.quests.get(quest_id)
                if cached is None:
                    self.quest_texts.want(quest_id)
                entry = {
                    "slot": slot["slot"], "quest_id": quest_id, "state": slot["state"],
                    "counters": slot["counters"], "time": slot["time"],
                }
                if cached is not None:
                    entry["title"] = cached.get("title")
                    entry["objectives_text"] = cached.get("objectives")
                    entry["objectives"] = _quest_objectives_progress(slot["counters"], cached)
                out.append(entry)
            return out

    # ── NPC interaction / ui_state (UM-40) ────────────────────────────────

    def apply_gossip_message(self, data: dict):
        """SMSG_GOSSIP_MESSAGE (agent.npc.parse_gossip_message): opens (or
        replaces) the gossip window. If the npc text for this menu's
        text_id is already cached, attach its first option's text as
        `body_text`; otherwise queue a CMSG_NPC_TEXT_QUERY (drained by
        session.py like the name cache)."""
        with self._lock:
            window = {"kind": "gossip", **data}
            cached = self.npc_texts.texts.get(data["text_id"])
            if cached:
                window["body_text"] = _first_npc_text(cached)
            else:
                self.npc_texts.want(data["text_id"], data["npc_guid"])
            self.ui_state = window

    def apply_gossip_complete(self):
        """SMSG_GOSSIP_COMPLETE: the server closed the gossip window (e.g.
        after gossip_select on a plain "go away" option)."""
        with self._lock:
            self.ui_state = None

    def apply_list_inventory(self, data: dict):
        """SMSG_LIST_INVENTORY (agent.npc.parse_list_inventory): opens the
        vendor window."""
        with self._lock:
            self.ui_state = {"kind": "vendor", **data}

    def apply_trainer_list(self, data: dict):
        """SMSG_TRAINER_LIST (agent.npc.parse_trainer_list): opens the
        trainer window."""
        with self._lock:
            self.ui_state = {"kind": "trainer", **data}

    def apply_npc_text_update(self, data: dict):
        """SMSG_NPC_TEXT_UPDATE (agent.npc.parse_npc_text_update): backfill
        the currently-open gossip window's body_text if it's still waiting
        on this text_id."""
        with self._lock:
            self.npc_texts.on_response(data)
            if data["found"] and self.ui_state is not None \
                    and self.ui_state.get("kind") == "gossip" \
                    and self.ui_state.get("text_id") == data["text_id"]:
                self.ui_state["body_text"] = _first_npc_text(data)

    def close_window(self):
        """Local-only close (the client doesn't need server confirmation to
        stop showing a window) — used by actions.CloseWindowAction."""
        with self._lock:
            self.ui_state = None

    def apply_item_query_response(self, data: dict):
        """SMSG_ITEM_QUERY_SINGLE_RESPONSE (UM-42): backfill every
        currently-known item/container with this entry, same policy as
        apply_creature_query_response."""
        with self._lock:
            self.items.on_item_query_response(data)
            if data["found"]:
                for obj in self.objects.values():
                    if obj.object_type in ("item", "container") and obj.entry == data["entry"]:
                        obj.name = data["name"]

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
        if "speeds" in movement:
            obj.speeds = movement["speeds"]
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

    def update_my_position_from_simulation(self, position: tuple):
        """UM-38: agent.movement.Mover simulates our own position between
        real server updates (client-authoritative movement) — mirror it
        onto our own ObjectInfo so every perception consumer (distance_to,
        snapshot) sees it, not just session.player_position. A no-op before
        our own object exists (there's nowhere to put it yet)."""
        with self._lock:
            obj = self.objects.get(self.my_guid)
            if obj is not None:
                obj.position = position

    def _apply_fields(self, obj: ObjectInfo, object_type: int | None, raw_fields: dict | None):
        if not raw_fields:
            return
        obj.raw_fields.update(raw_fields)
        decoded = uf.decode_fields(object_type if object_type is not None else -1, raw_fields)
        for attr in ("entry", "level", "health", "max_health", "faction",
                     "unit_flags", "dynamic_flags", "npc_flags", "target_guid",
                     "player_flags", "power_type"):
            if attr in decoded:
                setattr(obj, attr, decoded[attr])
        if "power" in decoded:
            obj.power.update(decoded["power"])
        if "max_power" in decoded:
            obj.max_power.update(decoded["max_power"])

    def build_equipment_and_inventory(self) -> tuple[dict, list]:
        """UM-42: equipment (slot -> item dict, slots 0-18) and inventory
        (list of item dicts, slots 19-38: 4 equipped-bag-container slots +
        16 backpack slots) built from the self player's own INV_SLOT_HEAD/
        PACK_SLOT_1 guid fields (agent.update_fields.
        decode_equipment_and_inventory_guids) cross-referenced against the
        item objects those guids point to (agent.update_fields.
        decode_item_fields) and this WorldState's item-template cache for
        names. A slot whose item object hasn't arrived yet (or whose
        template name hasn't resolved) is still included with whatever is
        known (bare guid, or guid+entry without a name)."""
        with self._lock:
            me = self.objects.get(self.my_guid)
            if me is None:
                return {}, []
            slot_guids = uf.decode_equipment_and_inventory_guids(me.raw_fields)
            equipment: dict[int, dict] = {}
            inventory: list = []
            for slot, guid in slot_guids.items():
                item_obj = self.objects.get(guid)
                d = {"guid": guid}
                if item_obj is not None:
                    d["entry"] = item_obj.entry
                    d["name"] = item_obj.name or None
                    count = item_obj.raw_fields and uf.decode_item_fields(item_obj.raw_fields).get("count")
                    if count is not None:
                        d["count"] = count
                if slot < uf.EQUIPMENT_SLOT_COUNT:
                    equipment[slot] = d
                else:
                    d["slot"] = slot
                    inventory.append(d)
            return equipment, inventory

    def snapshot(self, my_position=None, max_range: float = 50.0, limit: int = 40,
                 corpse_position=None, pending_invite=None, chat_inbox=None) -> dict:
        """A JSON-serialisable view shaped like docs/AI-AGENT-SPEC.md's
        `GET /agent/{id}/perception`: position, nearby_units, nearby_players,
        nearby_objects, sorted by distance and capped at `limit` each.

        `my_position` overrides the self object's own recorded position
        (useful right after login, before any update-object block has
        arrived for self) — defaults to the self object's position.

        `corpse_position` (UM-43) is session-scoped protocol state (from
        MSG_CORPSE_QUERY) this class doesn't otherwise have access to —
        callers (agent/think.py) pass session.corpse_position through so the
        LLM sees it, is_dead, and is_ghost alongside everything else in one
        snapshot.

        `pending_invite` (session.pending_invite) and `chat_inbox`
        (session.chat_inbox, UM-68) are likewise session-scoped state this
        class doesn't own — passed through so the LLM has something to
        react to with the accept_group/say/whisper actions (agent/actions.py).
        """
        with self._lock:
            objects = list(self.objects.values())
            me = self.objects.get(self.my_guid)
            window = self.ui_state

        pos = my_position or (me.position if me else None)
        equipment, inventory = self.build_equipment_and_inventory()
        out = {
            "position": _position_dict(pos),
            "is_dead": bool(me is not None and me.is_dead()),
            "is_ghost": bool(me is not None and me.is_ghost()),
            "corpse_position": _position_dict(corpse_position) if corpse_position is not None else None,
            "nearby_units": [],
            "nearby_players": [],
            "nearby_objects": [],
            "window": window,
            "equipment": equipment,
            "inventory": inventory,
            "pending_invite": pending_invite,
            "chat_inbox": list(chat_inbox) if chat_inbox is not None else [],
            "quest_log": self.build_quest_log(),
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
    if obj.quest_giver_status is not None:
        d["quest_giver_status"] = obj.quest_giver_status_name or obj.quest_giver_status
    d["in_combat"] = bool(obj.unit_flags and (obj.unit_flags & 0x00080000))  # UNIT_FLAG_IN_COMBAT, UnitDefines.h
    return d


def _quest_objectives_progress(counters: list, cached: dict) -> list:
    """Best-effort per-objective progress strings, e.g. "Mana Wyrm slain:
    3/8" for a kill-credit objective or "Linen Cloth: 2/5" for an item
    objective — pairs the quest log's up-to-4 raw counters (agent.
    update_fields.decode_quest_log) against the cached quest's
    required_credit (creature/GO kill credit) then required_items
    (agent.quests.parse_quest_query_response), in that order, since that's
    the order TrinityCore's own quest-log UI lists them in. A counter with
    no corresponding cached requirement (index beyond what the quest
    actually needs) is skipped rather than guessed at."""
    reqs = list(cached.get("required_credit") or []) + list(cached.get("required_items") or [])
    out = []
    for i, req in enumerate(reqs):
        if i >= len(counters) or not req.get("count"):
            continue
        out.append({
            "entry": req["entry"], "count": counters[i], "needed": req["count"],
            "text": f"{req['entry']}: {counters[i]}/{req['count']}",
        })
    return out


def _first_npc_text(data: dict) -> str:
    """Pick the first non-empty option's text0 out of a parsed
    SMSG_NPC_TEXT_UPDATE (agent.npc.parse_npc_text_update) — real servers
    fill option 0 for a plain gossip greeting; the remaining 7 slots
    (MAX_GOSSIP_TEXT_OPTIONS) are usually empty placeholders for randomized
    flavor text, which this v1 doesn't attempt to pick between."""
    for option in data.get("options", []):
        if option["text0"]:
            return option["text0"]
    return ""
