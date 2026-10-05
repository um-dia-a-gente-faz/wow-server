"""Per-character game state, filled in by the packet handlers (agent/handlers)."""

import collections
import time

from . import perception as per

# Bounded so a chatty channel can't grow this without limit.
CHAT_INBOX_MAXLEN = 50

# Bounded so a long fight or a busy session can't grow this without limit —
# the LLM loop (UM-44) only ever needs the last N events per think cycle.
EVENTS_MAXLEN = 100


class GameState:
    """Everything the agent knows about its character and the world."""

    def __init__(self):
        # Connection lifecycle (read by the recv loop and the reconnect supervisor)
        self._running = False
        self._in_world = False
        self.dropped_packets = 0  # packets whose handler raised

        # Game state
        self.player_guid = 0
        self.player_name = ""
        self.player_position = None  # (map_id, x, y, z, orient)
        self.level = 0
        self.race = 0  # ChrRaces.dbc ID; set by callers (e.g. __main__.py) from enum_characters()
        self.class_ = 0  # ChrClasses.dbc ID (UM-69); set by callers from enum_characters(), same as race
        self.xp = None
        self.next_level_xp = None
        self.coinage = None
        self.world_state = per.WorldState()  # nearby objects, players, etc.
        self.on_update_object = None  # callback(update_type, guid, fields)
        self.chat_inbox = collections.deque(maxlen=CHAT_INBOX_MAXLEN)
        # UM-47: optional agent.chat_relay.ChatRelay, set by the caller
        # (agent/__main__.py) — mirrors heard chat to tools/chat-feed.
        self.chat_relay = None
        self.pending_invite = None  # {"inviter_name": str} or None
        self.spellbook: set[int] = set()  # known spell IDs (UM-39)
        self.spell_cooldowns: dict[int, dict] = {}  # spell_id -> agent.spells.parse_initial_spells' cooldown entry shape
        self.events = collections.deque(maxlen=EVENTS_MAXLEN)  # combat/XP events, shaped like {"kind": str, ...}
        self.loot: dict | None = None  # current open loot session (agent.loot.parse_loot_response()'s dict), or None

        # Death/resurrection (UM-43)
        self.corpse_position = None       # (map_id, x, y, z) from MSG_CORPSE_QUERY's response, or None
        self.corpse_reclaim_ready_at = None  # time.monotonic() deadline from SMSG_CORPSE_RECLAIM_DELAY, or None
        self.graveyard_position = None    # (map_id, x, y, z) from SMSG_DEATH_RELEASE_LOC, informational only
        self._was_alive = True            # tracks health>0 -> 0 for the 'death' event (see handlers/world.py::sync_self_from_block)

        # Reconnect supervisor (UM-43, agent/__main__.py)
        self._logout_requested = False    # set by logout() — distinguishes a clean stop from a forced one
        self.unexpected_disconnect = False  # set when the recv thread/socket dies without a requested logout

    def record_event(self, kind: str, **fields):
        """Append to session.events with a time.monotonic() timestamp, so
        callers (e.g. agent.actions' _wait_for-style confirmation) can find
        "events since I sent this packet" reliably even though `events` is
        a bounded deque (older entries can fall off the left, which would
        make plain index-based slicing wrong)."""
        self.events.append({"kind": kind, "t": time.monotonic(), **fields})

    _record_event = record_event  # historical name, still used by actions and tests
