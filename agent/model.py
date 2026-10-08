"""The world-snapshot contract (#288): the one place that names the keys of the
dict that flows perception -> think -> candidates/brain -> audit.

These are `TypedDict`s, so the runtime values stay the plain dicts they always
were (the JSON sent to the LLM, Jev, the audit log and `/perception` is
unchanged) while mypy checks every literal key: renaming a field here turns
each stale `snapshot["..."]` / `unit.get("...")` in a checked module into a
type error. `agent/tests/test_model.py` pins these types to the golden snapshot.

Leaf module: imports nothing from `agent/`.
"""

from typing import Any, NotRequired, TypedDict

# A raw 64-bit GUID inside perception; a short handle ("u3", agent.handles) once
# WorldState.snapshot() has encoded it, which is what every consumer sees.
Guid = int | str


class Position(TypedDict):
    map: int | None
    x: float
    y: float
    z: float


class Unit(TypedDict):
    """One entry of `nearby_units` / `nearby_players` / `nearby_objects`."""
    guid: Guid
    entry: int | None
    name: str | None
    type: str | None            # "unit", "player", "gameobject", "item", ...
    distance: float
    in_combat: bool
    position: NotRequired[Position]
    faction: NotRequired[int]
    level: NotRequired[int]
    health_pct: NotRequired[float | None]
    target_guid: NotRequired[Guid]
    quest_giver_status: NotRequired[str | int]  # the status name when known, else the raw value
    lootable: NotRequired[bool]                 # only present when true
    npc_flags: NotRequired[int]                 # only present when non-zero
    in_group: NotRequired[bool]                 # nearby_players only


class Item(TypedDict):
    """One equipped or carried item. Only `guid` is known until the item's own
    object and its template have arrived."""
    guid: Guid
    entry: NotRequired[int | None]
    name: NotRequired[str | None]
    template: NotRequired[dict[str, Any]]  # projection of the item-template cache
    count: NotRequired[int]
    slot: NotRequired[int]                 # inventory only; equipment is keyed by slot


class QuestEntry(TypedDict):
    slot: int
    quest_id: int
    state: int
    state_name: str
    counters: list[int]
    time: int
    title: NotRequired[str | None]            # these three once the quest text is cached
    objectives_text: NotRequired[str | None]
    objectives: NotRequired[list[dict[str, Any]]]


class Me(TypedDict):
    """Own status, added by agent.think (`_self_status`). Health and each power
    are "current/max" strings (the bare current value while max is unknown)."""
    level: int | None
    class_id: int | None
    health: NotRequired[str]
    mana: NotRequired[str | int]  # the power keys are agent.update_fields.POWER_NAMES
    rage: NotRequired[str | int]
    focus: NotRequired[str | int]
    energy: NotRequired[str | int]
    happiness: NotRequired[str | int]
    rune: NotRequired[str | int]
    runic_power: NotRequired[str | int]
    xp: NotRequired[int]
    next_level_xp: NotRequired[int]


class KnownSpell(TypedDict):
    id: int
    name: str


class Snapshot(TypedDict):
    """What WorldState.snapshot() returns, plus the `me` / `spells` keys
    agent.think adds before the brain sees it."""
    position: Position | None
    is_dead: bool
    is_ghost: bool
    corpse_position: Position | None
    nearby_units: list[Unit]
    nearby_players: list[Unit]
    nearby_objects: list[Unit]
    window: dict[str, Any] | None        # open NPC window (agent.perception.windows)
    trade: dict[str, Any] | None
    mailbox: dict[str, Any] | None
    has_new_mail: bool
    equipment: dict[int, Item]           # equipment slot -> item
    inventory: list[Item]
    pending_invite: dict[str, Any] | None
    group: dict[str, Any] | None         # agent.group.snapshot_view
    chat_inbox: list[Any]
    channels: list[str]
    quest_log: list[QuestEntry]
    me: NotRequired[Me]
    spells: NotRequired[list[KnownSpell]]


class Candidate(TypedDict):
    """One option agent.candidates offers a choice-only brain."""
    id: str
    label: str
    action: str
    params: dict[str, Any]


class ActionRecord(TypedDict):
    """One past cycle as the brain sees it (agent.think.ThinkState.for_prompt)."""
    action: str
    args: dict[str, Any]
    ok: bool
    error: NotRequired[str | None]
    result: NotRequired[str]
    changed: NotRequired[bool]  # only present (False) when the action changed nothing
