"""Ports: what actions and reflexes may use of a session (#304).

`agent.session.WoWSession` satisfies every port here, and so does the shared test
fake (`agent.tests.builders.FakeSession`). An action or reflex uses only these
members, so the interface is written down once instead of living in each test's
fake. Read-only members are properties: code behind a port reads them and never
rebinds them.

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
    """Read-only facts about the agent's own character."""

    @property
    def player_guid(self) -> int: ...

    @property
    def player_position(self) -> tuple | None:
        """(map_id, x, y, z, orientation), or None before the first position."""

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
    def pending_invite(self) -> dict | None: ...

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
