"""Ports: what actions and reflexes may use of a session (#304).

`agent.session.WoWSession` satisfies every port here, and so does the shared test
fake (`agent.tests.builders.FakeSession`). An action or reflex uses only these
members, so the interface is written down once instead of living in each test's
fake. Read-only members are properties: code behind a port reads them and never
rebinds them. Three `PlayerView` members are plain writable attributes because
actions do set them: `player_position` (`FaceAction` and the walk in
`agent.movement` mirror what they sent, since the server does not echo our own
movement), `pending_invite` (`accept_group`/`decline_group` clear it) and
`follow_on_accept` (`accept_group` sets it, the follow reflex clears it).

This module imports nothing from `agent/`.
"""
from collections.abc import Collection, Iterable, Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PacketSink(Protocol):
    """Sends one client packet to the world server."""

    def send_packet(self, opcode: int, payload: bytes = b"") -> None: ...


@runtime_checkable
class EventLog(Protocol):
    """The bounded log of things that happened, as `{"kind", "t", **fields}`."""

    @property
    def events(self) -> Iterable[dict[str, Any]]: ...

    def events_snapshot(self) -> list[dict[str, Any]]: ...

    def record_event(self, kind: str, **fields: Any) -> None: ...

    # ADR 0020 (#390): activity events for the next decision line; think drains them.
    def emit_activity(self, kind: str, **detail: Any) -> None: ...

    def drain_activity(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class PlayerView(Protocol):
    """Facts about the agent's own character. Read-only, except the two plain
    attributes, which actions set (see the module docstring)."""

    # (map_id, x, y, z, orientation), or None before the first position.
    player_position: tuple | None
    # {"inviter_name": str} while an invite is open, else None.
    pending_invite: dict | None
    # (inviter_name, monotonic deadline) after accepting an invite; the follow reflex consumes it (#318).
    follow_on_accept: tuple[str, float] | None

    @property
    def player_guid(self) -> int: ...

    @property
    def player_name(self) -> str: ...

    @property
    def agent_names(self) -> frozenset[str]: ...

    @property
    def is_alive(self) -> bool: ...

    @property
    def race(self) -> int: ...

    @property
    def class_(self) -> int: ...

    @property
    def coinage(self) -> int | None: ...

    @property
    def spellbook(self) -> Collection[int]: ...

    @property
    def spell_cooldowns(self) -> Mapping[int, dict]: ...

    @property
    def group(self) -> dict | None: ...

    # #411: read by agent.death, agent.control and agent.think. Plain attributes, set by the session.
    # (map_id, x, y, z) from MSG_CORPSE_QUERY's response, or None.
    corpse_position: tuple | None
    # time.monotonic() deadline from SMSG_CORPSE_RECLAIM_DELAY, or None.
    corpse_reclaim_ready_at: float | None
    # (map_id, x, y, z) from SMSG_DEATH_RELEASE_LOC, or None: the graveyard, the way back when the corpse is unreachable (#763).
    graveyard_position: tuple | None
    # #763: an unmet intent was already recorded for this death.
    corpse_unreachable_recorded: bool
    # True once the socket died without a requested logout.
    unexpected_disconnect: bool
    # The world model, agent.perception.WorldState; typed Any because this module imports
    # nothing from agent/.
    world_state: Any


@runtime_checkable
class Inbox(Protocol):
    """Chat the agent heard, and what it said on channels (for the repeat guard)."""

    @property
    def chat_inbox(self) -> Iterable[dict[str, Any]]: ...

    def chat_inbox_snapshot(self) -> list[dict[str, Any]]: ...

    @property
    def channel_say_history(self) -> list[tuple[float, str]]: ...


@runtime_checkable
class ReflexSlots(Protocol):
    """Where a session keeps its one reflex of each kind, and its movement engine. A slot is
    None until `agent.reflexes` (or `agent.movement.get_mover`, for `mover`) fills it; typed
    `Any` because this module imports no reflex or mover."""

    follow_reflex: Any
    rest_reflex: Any
    mover: Any
    # #312: agent.pathing.PathClient or None; the mover reads it, agent.root sets it.
    path_client: Any


@runtime_checkable
class ActionSession(PacketSink, EventLog, PlayerView, Inbox, ReflexSlots, Protocol):
    """Everything an `Action.check`/`execute` may use of the session."""
