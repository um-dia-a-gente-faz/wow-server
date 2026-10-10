"""Action framework: Action, ActionResult, REGISTRY, register(), catalog(), the confirmation waits and the session facade (send).

Split out of the former agent/actions.py (issue #248).
"""

from dataclasses import dataclass, field
import time

from .. import changes
from .. import update_fields as uf
from ..ports import ActionSession, PacketSink


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


def _wait_for_value(get_value, timeout: float = DEFAULT_CONFIRM_TIMEOUT_S,
                    interval: float = DEFAULT_CONFIRM_POLL_S):
    """Return the first non-None `get_value()`, or None after `timeout`.
    Used by an action's execute() to confirm its effect landed (a response
    event, a field change) instead of trusting the packet was sent, and to
    learn *which* outcome matched. Blocks the calling thread (think/act
    step, never the recv thread). Sleeps on agent.changes, which the recv
    thread signals on every recorded event and applied update-object block,
    so a confirmation is seen at once; `interval` only caps the wait for
    state no handler signals (plain GameState fields), so those are still
    polled."""
    deadline = time.monotonic() + timeout
    while True:
        value = get_value()
        if value is not None:
            return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        changes.wait(min(interval, remaining))


def _wait_for(predicate, timeout: float = DEFAULT_CONFIRM_TIMEOUT_S,
              interval: float = DEFAULT_CONFIRM_POLL_S) -> bool:
    """_wait_for_value for a boolean predicate."""
    return _wait_for_value(lambda: True if predicate() else None, timeout, interval) is True


class Action:
    """Base for every agent action. Subclasses set `name`/`description`/
    `params` (JSON-schema `properties`, OpenAI/Anthropic-tool-compatible)
    and implement `check`/`execute`; `run` is what callers use."""

    name: str = ""
    description: str = ""
    params: dict = {}
    required: tuple = ()  # subset of params.keys() the schema marks required

    def check(self, session: ActionSession, world, **params) -> str | None:
        """Return an error string if this action shouldn't execute right
        now, else None. Called by run() before execute() — execute()
        implementations can assume check() already passed."""
        return None

    def execute(self, session: ActionSession, world, **params) -> ActionResult:
        raise NotImplementedError

    def run(self, session: ActionSession, world, **params) -> ActionResult:
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


def send(session: PacketSink, opcode: int, payload: bytes = b""):
    """The one place actions send a packet through (agent.ports.PacketSink)."""
    session.send_packet(opcode, payload)
