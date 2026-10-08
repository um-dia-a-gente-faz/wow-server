"""WorldState: a live, thread-safe model of what the agent perceives.

Built from agent.update_object.UpdateBlock (block framing + movement, UM-32)
and agent.update_fields.decode_fields (VALUES field mapping, UM-33). The
snapshot sections are built by the sibling modules; the name, window and
trade state live in mixins.
"""

import threading
import time

from .. import handles as hd
from .. import items as it
from .. import names as nm
from .. import npc as npc_mod
from .. import quests as qu
from .. import update_object as uo
from .fields import FieldsMixin
from ..model import Item, QuestEntry, Snapshot
from .inventory import build_equipment_and_inventory
from .objects import ObjectInfo
from .quest_log import build_quest_log
from .resolution import ResolutionMixin
from .snapshot import compose_snapshot
from .trade_state import TradeMixin
from .windows import WindowsMixin


class WorldState(FieldsMixin, ResolutionMixin, WindowsMixin, TradeMixin):
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
        # UM-59: the currently pending/open player trade, or None — see the
        # "── Trade ──" section below. Exposed to the LLM via snapshot()'s
        # 'trade' key, separately from 'window' (an NPC gossip/vendor/
        # trainer window and a trade can't both be relevant at once in
        # practice, but they're conceptually different things: a trade has
        # two-sided state a single "window" dict doesn't shape well).
        self.trade: dict | None = None

        # UM-60: the mailbox window (None until open_mailbox()), and a
        # standing "you have mail" flag (SMSG_RECEIVED_MAIL) — both exposed
        # to the LLM via snapshot()'s 'mailbox'/'has_new_mail' keys.
        self.mailbox: dict | None = None
        self.has_new_mail = False

        # UM-89: short, stable string handles ("u3", "p1") shown to the LLM
        # instead of raw 64-bit GUIDs, which don't survive a float64 JSON
        # round-trip. snapshot() encodes, think.py resolves tool-call params.
        self.handles = hd.HandleMap()
        # UM-93: chat channels we're in, full server name ("General -
        # Eversong Woods") -> {"channel_id", "flags"}. Filled from
        # SMSG_CHANNEL_NOTIFY (agent.channels); exposed to the LLM as
        # snapshot()'s sorted 'channels' list (channel_say is unregistered
        # while chat is deferred, UM-98).
        self.channels: dict[str, dict] = {}

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
        window (UM-40), for the agent/actions/ package to check against without racing the
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


    def build_quest_log(self) -> list[QuestEntry]:
        """UM-41: the self player's quest log, enriched from the quest-text
        cache (see perception/quest_log.py). Queues a CMSG_QUEST_QUERY for
        any quest id not cached yet."""
        with self._lock:
            return build_quest_log(self.objects.get(self.my_guid), self.quest_texts, self.names)

    def build_equipment_and_inventory(self) -> tuple[dict[int, Item], list[Item]]:
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
            return build_equipment_and_inventory(self.objects.get(self.my_guid), self.objects, self.items)

    def snapshot(self, my_position=None, max_range: float = 50.0, limit: int = 40,
                 corpse_position=None, pending_invite=None, chat_inbox=None,
                 group=None) -> Snapshot:
        """A JSON-serialisable view shaped like docs/AI-AGENT-SPEC.md's
        `GET /agent/{id}/perception`: position, nearby_units, nearby_players,
        nearby_objects, sorted by distance and capped at `limit` each.

        GUIDs never appear raw (UM-89): every `guid`/`*_guid`/`*_guids`
        field holds a short handle like "u3" from self.handles instead,
        stable for as long as this WorldState lives.

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
        react to with the accept_group/say/whisper actions (agent/actions/chat.py).

        `group` (GH-71, session.group from agent.group.parse_group_list) is
        exposed as `group` (leader, loot method, members; None when
        ungrouped), and marks each nearby player `in_group` when they are
        in *our* group.
        """
        with self._lock:
            view = {
                "objects": list(self.objects.values()),
                "me": self.objects.get(self.my_guid),
                "my_guid": self.my_guid,
                "window": self.ui_state,
                "trade": self.trade,
                "mailbox": self.mailbox,
                "has_new_mail": self.has_new_mail,
                "channels": sorted(self.channels),
            }
        view["equipment"], view["inventory"] = self.build_equipment_and_inventory()
        view["quest_log"] = self.build_quest_log()
        out = compose_snapshot(view, my_position, max_range, limit, corpse_position,
                               pending_invite, chat_inbox, group)
        # UM-89: every GUID-valued field (guid, *_guid, *_guids) anywhere in
        # the snapshot becomes a handle; see agent/handles.py.
        return self.handles.encode(out)
