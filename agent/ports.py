"""Ports: what actions and reflexes may use of a session (#304).

`agent.session.WoWSession` satisfies every port here, and so does the shared test
fake (`agent.tests.builders.FakeSession`). An action or reflex uses only these
members, so the interface is written down once instead of living in each test's
fake. Read-only members are properties: code behind a port reads them and never
rebinds them. Two `PlayerView` members are plain writable attributes because
actions do set them: `player_position` (`FaceAction` and the walk in
`agent.movement` mirror what they sent, since the server does not echo our own
movement) and `pending_invite` (`accept_group`/`decline_group` clear it).

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

    def record_event(self, kind: str, **fields: Any) -> None: ...


@runtime_checkable
class PlayerView(Protocol):
    """Facts about the agent's own character. Read-only, except the two plain
    attributes, which actions set (see the module docstring)."""

    # (map_id, x, y, z, orientation), or None before the first position.
    player_position: tuple | None
    # {"inviter_name": str} while an invite is open, else None.
    pending_invite: dict | None

    @property
    def player_guid(self) -> int: ...

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


@runtime_checkable
class Inbox(Protocol):
    """Chat the agent heard, and what it said on channels (for the repeat guard)."""

    @property
    def chat_inbox(self) -> Iterable[dict[str, Any]]: ...

    @property
    def channel_say_history(self) -> list[tuple[float, str]]: ...


@runtime_checkable
class ReflexSlots(Protocol):
    """Where a session keeps its one reflex of each kind. A slot is None until
    `agent.reflexes` fills it; typed `Any` because this module imports no reflex."""

    follow_reflex: Any
    rest_reflex: Any


@runtime_checkable
class ActionSession(PacketSink, EventLog, PlayerView, Inbox, ReflexSlots, Protocol):
    """Everything an `Action.check`/`execute` may use of the session."""
