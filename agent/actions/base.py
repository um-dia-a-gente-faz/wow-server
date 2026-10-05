"""Action framework: Action, ActionResult, REGISTRY, register(), catalog(), the confirmation waits and the session facade (send).

Split out of the former agent/actions.py (issue #248).
"""

from dataclasses import dataclass, field
import time

from .. import update_fields as uf


def _record_event(session, kind: str, **fields):
    """Same shape as WoWSession._record_event — duplicated here (matching
    agent.reflexes.follow/rest's own copy of this) instead of calling
    session._record_event() directly, so an action that records its own
    event (send_mail, UM-60) also works against the plain `events: list`
    fake sessions this module's tests use, which have no _record_event."""
    events = getattr(session, "events", None)
    if events is None:
        return
    events.append({"kind": kind, "t": time.monotonic(), **fields})


# ── Action framework (UM-36) ─────────────────────────────────────────────
# The uniform shape every action follows, so UM-44's LLM loop can expose
# REGISTRY as a tool catalog without bespoke per-action glue.

DEFAULT_CONFIRM_TIMEOUT_S = 2.0
DEFAULT_CONFIRM_POLL_S = 0.1


@dataclass
class ActionResult:
    ok: bool
    error: str | None = None
    detail: dict = field(default_factory=dict)


def _wait_for(predicate, timeout: float = DEFAULT_CONFIRM_TIMEOUT_S,
              interval: float = DEFAULT_CONFIRM_POLL_S) -> bool:
    """Poll `predicate` (called with no args) until it's true or `timeout`
    seconds pass. Used by an action's execute() to confirm its effect landed
    in perception (e.g. the self target field, UNIT_FIELD_TARGET) instead of
    just trusting the packet was sent. Blocks the calling thread — callers
    run this from the think/act step, not the recv thread."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _wait_for_value(get_value, timeout: float = DEFAULT_CONFIRM_TIMEOUT_S,
                     interval: float = DEFAULT_CONFIRM_POLL_S):
    """Like _wait_for, but for confirmations that need to return *which*
    event matched (e.g. a cast succeeding vs. failing) — polls `get_value()`
    until it returns something other than None, or `timeout` elapses (then
    returns None)."""
    deadline = time.monotonic() + timeout
    while True:
        value = get_value()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


class Action:
    """Base for every agent action. Subclasses set `name`/`description`/
    `params` (JSON-schema `properties`, OpenAI/Anthropic-tool-compatible)
    and implement `check`/`execute`; `run` is what callers use."""

    name: str = ""
    description: str = ""
    params: dict = {}
    required: tuple = ()  # subset of params.keys() the schema marks required

    def check(self, session, world, **params) -> str | None:
        """Return an error string if this action shouldn't execute right
        now, else None. Called by run() before execute() — execute()
        implementations can assume check() already passed."""
        return None

    def execute(self, session, world, **params) -> ActionResult:
        raise NotImplementedError

    def run(self, session, world, **params) -> ActionResult:
        error = self.check(session, world, **params)
        if error is not None:
            return ActionResult(ok=False, error=error)
        return self.execute(session, world, **params)

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": self.params,
                "required": list(self.required),
            },
        }


REGISTRY: dict[str, Action] = {}


def register(action_cls: type) -> type:
    """Class decorator: instantiates `action_cls` and adds it to REGISTRY
    under its `.name`. Actions are stateless aside from per-instance
    confirm_timeout/confirm_interval overrides, so one shared instance is
    fine — tests that need a different timeout construct their own
    instance directly instead of going through REGISTRY."""
    instance = action_cls()
    REGISTRY[instance.name] = instance
    return action_cls


def catalog() -> list[dict]:
    """Every registered action's schema, in registration order — the tool
    list UM-44's LLM loop passes to the model."""
    return [a.schema() for a in REGISTRY.values()]


def _find_item_guid(world, bag: int, slot: int) -> int | None:
    """Resolve a (bag, slot) inventory position to the item GUID sitting
    there, from the self player's own INV_SLOT_HEAD/PACK_SLOT_1 fields.
    v1 only understands bag == INVENTORY_SLOT_BAG_0 (equipped items,
    equipped bag containers, and backpack contents) — an item inside a
    *non-backpack* bag isn't addressable yet (would need decoding that
    bag's own CONTAINER_FIELD_SLOT_1 array, not modeled in this card)."""
    if bag != uf.INVENTORY_SLOT_BAG_0:
        return None
    me = world.get_my_object()
    if me is None:
        return None
    return uf.decode_equipment_and_inventory_guids(me.raw_fields).get(slot)


# Hooks called before an explicit LLM movement action (move_to/move_towards/
# stop_movement): the follow reflex (agent.reflexes.follow, UM-58) registers
# its pause here on import, so this package never imports the reflex (which
# imports Action/register from here). An LLM-picked move outranks the reflex
# (docs/AI-AGENT-SPEC.md's reflex priority), so following pauses and reports
# why instead of fighting the move. No hooks registered = nothing is following.
MOVE_OVERRIDE_HOOKS: list = []


def _pause_follow_reflex(session, world):
    for hook in MOVE_OVERRIDE_HOOKS:
        hook(session, world)


def send(session, opcode: int, payload: bytes = b""):
    """Session facade: the one place actions send a packet through, so they
    don't each reach for the session's private _send_packet."""
    session._send_packet(opcode, payload)
