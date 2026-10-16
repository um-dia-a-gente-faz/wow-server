"""Per-character game state, filled in by the packet handlers (agent/handlers)."""

import collections
import time

from . import changes
from . import perception as per

# Bounded so a chatty channel can't grow this without limit.
CHAT_INBOX_MAXLEN = 50

# Bounded so a long fight or a busy session can't grow this without limit —
# the LLM loop (UM-44) only ever needs the last N events per think cycle.
EVENTS_MAXLEN = 100

# ADR 0020 (#390): activity events waiting for the next decision line. Bounded, so a burst between two
# decisions drops the oldest instead of growing; the cost is stated in ADR 0020 (Consequences).
ACTIVITY_MAXLEN = 20


class GameState:
    """Everything the agent knows about its character and the world."""

    @property
    def is_alive(self) -> bool:
        """False from the health>0 -> 0 transition until health is back (see _was_alive)."""
        return self._was_alive

    def __init__(self):
        # Connection lifecycle (read by the recv loop and the reconnect supervisor)
        self._running = False
        self._in_world = False
        self.dropped_packets = 0  # packets whose handler raised
        self.client_cache_version: int | None = None  # SMSG_CLIENTCACHE_VERSION (#751); None until it arrives

        # Game state
        self.player_guid = 0
        self.player_name = ""
        self.player_position: tuple | None = None  # (map_id, x, y, z, orient)
        self.level = 0
        self.race = 0  # ChrRaces.dbc ID; set by callers (e.g. login.py) from enum_characters()
        self.class_ = 0  # ChrClasses.dbc ID (UM-69); set by callers from enum_characters(), same as race
        self.xp = None
        self.next_level_xp = None
        self.coinage = None
        self.world_state = per.WorldState()  # nearby objects, players, etc.
        self.chat_inbox = collections.deque(maxlen=CHAT_INBOX_MAXLEN)
        # UM-47: optional agent.chat_relay.ChatRelay, set by the caller
        # (agent/login.py) — mirrors heard chat to tools/chat-feed.
        self.chat_relay = None
        # (time.monotonic(), normalised text) of our own recent channel messages:
        # the channel_say action's rate limit and repeat guard.
        self.channel_say_history: list[tuple[float, str]] = []
        # The session's one reflex of each kind, filled by agent.reflexes on first use.
        self.follow_reflex = None
        self.rest_reflex = None
        # The session's one movement engine (agent.movement.Mover), made by get_mover() on first use.
        self.mover = None
        self.pending_invite: dict | None = None  # {"inviter_name": str} or None
        self.group = None  # agent.group.parse_group_list() dict, or None when ungrouped (GH-71)
        # #318: lower-cased character names of the other agents (config AGENT_ROSTER); empty = unknown.
        self.agent_names: frozenset[str] = frozenset()
        # #318: set when an invite is accepted: (inviter_name, monotonic deadline). The follow reflex
        # starts following that leader once the server confirms the party, then clears it.
        self.follow_on_accept: tuple[str, float] | None = None
        self.spellbook: set[int] = set()  # known spell IDs (UM-39)
        self.spell_cooldowns: dict[int, dict] = {}  # spell_id -> agent.spells.parse_initial_spells' cooldown entry shape
        self.events = collections.deque(maxlen=EVENTS_MAXLEN)  # combat/XP events, shaped like {"kind": str, ...}
        self.loot: dict | None = None  # current open loot session (agent.loot.parse_loot_response()'s dict), or None
        # ADR 0020 (#390): set by agent/login.py from AGENT_ACTIVITY_EVENTS. Off: emit_activity buffers nothing.
        self.activity_events = False
        self._activity: collections.deque[dict] = collections.deque(maxlen=ACTIVITY_MAXLEN)

        # Death/resurrection (UM-43)
        self.corpse_position: tuple | None = None  # (map_id, x, y, z) from MSG_CORPSE_QUERY's response, or None
        self.corpse_reclaim_ready_at: float | None = None  # time.monotonic() deadline from SMSG_CORPSE_RECLAIM_DELAY, or None
        self.graveyard_position = None    # (map_id, x, y, z) from SMSG_DEATH_RELEASE_LOC, informational only
        self._was_alive = True            # tracks health>0 -> 0 for the 'death' event (see handlers/world.py::sync_self_from_block)

        # Reconnect supervisor (UM-43, agent/supervisor.py)
        self._logout_requested = False    # set by logout() — distinguishes a clean stop from a forced one
        self.unexpected_disconnect = False  # set when the recv thread/socket dies without a requested logout

    def record_event(self, kind: str, **fields):
        """Append to session.events with a time.monotonic() timestamp, so
        callers (e.g. agent.actions' _wait_for-style confirmation) can find
        "events since I sent this packet" reliably even though `events` is
        a bounded deque (older entries can fall off the left, which would
        make plain index-based slicing wrong)."""
        self.events.append({"kind": kind, "t": time.monotonic(), **fields})
        changes.notify()

    def emit_activity(self, kind: str, **detail) -> None:
        """Buffer one activity event for the next decision line (ADR 0020). Called from the recv thread."""
        if not self.activity_events:
            return
        self._activity.append({"kind": kind, "t": time.time(), "detail": detail})

    def drain_activity(self) -> list[dict]:
        """Take every buffered activity event, oldest first. Called on the think thread.

        Pops until IndexError, never copies and then clears: an event appended between a copy and a
        clear would be lost. deque.popleft and append are atomic, so the two threads need no lock."""
        out = []
        while True:
            try:
                out.append(self._activity.popleft())
            except IndexError:
                return out

    def events_snapshot(self) -> list[dict]:
        """A copy of `events`, oldest first. Readers on any thread but the recv thread
        (the appender) must iterate this, never `events`: a live loop raises "deque
        mutated during iteration" when an append lands mid-scan. list(deque) copies in
        C without releasing the GIL, so it cannot be interrupted (ADR 0009)."""
        return list(self.events)

    def chat_inbox_snapshot(self) -> list[dict]:
        """A copy of `chat_inbox`, for the same reason as events_snapshot()."""
        return list(self.chat_inbox)
