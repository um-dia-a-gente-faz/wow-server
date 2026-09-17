#!/usr/bin/env python3
"""Rest reflex (UM-43): recover HP/mana between fights by eating/drinking
(if inventory has food) or sitting, without an LLM decision each tick — the
same "reflex between LLM steps" shape as agent.reflexes.follow (UM-58).

CMSG_STANDSTATECHANGE verified against TrinityCore branch `3.3.5`:
  src/server/game/Server/Protocol/Opcodes.h (CMSG_STANDSTATECHANGE = 0x101)
  src/server/game/Handlers/MiscHandler.cpp
    (HandleStandStateChangeOpcode: uint32 animstate)
  src/server/game/Entities/Unit/UnitDefines.h
    (enum UnitStandStateType: UNIT_STAND_STATE_STAND = 0, ..._SIT = 1)

UM-42 (inventory/loot) has merged: _try_eat_and_drink() reads the real
inventory model (agent.perception.WorldState.build_equipment_and_inventory)
and uses the registered `use_item` action. It still degrades gracefully —
no inventory, no matching food/drink item, or no use_item action registered
all mean rest() just sits, per this card.
"""

import logging
import struct
import time

from .. import actions
from .. import update_fields as uf
from ..perception import POWER_MANA

log = logging.getLogger("agent.reflexes.rest")

CMSG_STANDSTATECHANGE = 0x101
UNIT_STAND_STATE_STAND = 0
UNIT_STAND_STATE_SIT = 1

UNIT_FLAG_IN_COMBAT = 0x00080000  # UnitDefines.h — same mask perception.py/follow.py use

REST_HP_PCT_THRESHOLD = 0.5
REST_MANA_PCT_THRESHOLD = 0.3
REST_STOP_HP_PCT = 0.9

# Food/drink item names this agent knows to look for — a coarse heuristic
# (no item DB lookup), good enough until there's a real "is this consumable
# food/drink" flag to check instead.
_FOOD_DRINK_NAME_HINTS = ("bread", "water", "ration", "food", "drink", "juice", "bandage")


def _record_event(session, kind: str, **fields):
    """Same shape as WoWSession._record_event — duplicated here instead of
    calling that method directly so this module also works against the
    plain `events: list` fake sessions used in unit tests (matches
    agent.reflexes.follow._record_event)."""
    events = getattr(session, "events", None)
    if events is None:
        return
    events.append({"kind": kind, "t": time.monotonic(), **fields})


def _in_combat(me) -> bool:
    return bool(me is not None and me.unit_flags and (me.unit_flags & UNIT_FLAG_IN_COMBAT))


def needs_rest(me) -> bool:
    """True if `me` (agent.perception.ObjectInfo, our own object) is hurt or
    low on mana and out of combat. `me` may be None (own object not yet
    perceived), in which case this is False rather than raising."""
    if me is None or _in_combat(me):
        return False
    if me.health is not None and me.max_health:
        if me.health / me.max_health < REST_HP_PCT_THRESHOLD:
            return True
    if (me.power_type == POWER_MANA and me.power and me.max_power
            and len(me.power) > 0 and len(me.max_power) > 0 and me.max_power[0]):
        if me.power[0] / me.max_power[0] < REST_MANA_PCT_THRESHOLD:
            return True
    return False


def _try_eat_and_drink(session, world) -> bool:
    """Best-effort use of a food/drink item from the world's inventory
    model (agent.perception.WorldState.build_equipment_and_inventory, UM-42),
    if one exists. Returns True if something was consumed.

    Inventory items only carry a `slot` key (19-38 range) — they're all
    implicitly in bag=INVENTORY_SLOT_BAG_0 (255, agent/update_fields.py),
    the backpack/equipped-bags pseudo-bag; there is no per-item `bag` key to
    read."""
    _, inventory = world.build_equipment_and_inventory()
    if not inventory:
        return False
    use_item = actions.REGISTRY.get("use_item")
    if use_item is None:
        return False
    for item in inventory:
        name = str(item.get("name") or "").lower()
        if any(hint in name for hint in _FOOD_DRINK_NAME_HINTS):
            result = use_item.run(session, world, bag=uf.INVENTORY_SLOT_BAG_0, slot=item.get("slot"))
            if result.ok:
                return True
    return False


class RestReflex:
    """Per-session rest state — one instance per WoWSession, see
    get_rest_reflex(). Mirrors agent.reflexes.follow.FollowReflex's shape: a
    plain tick(session, world) driven at a fixed cadence from its own
    thread (agent/__main__.py), independent of the LLM think loop."""

    def __init__(self):
        self.active = False
        self.method: str | None = None  # "food" | "sit", set while active

    def start(self, session, world) -> bool:
        """Begin resting now, regardless of needs_rest() — used by both the
        reflex's own tick() and the `rest` action (so the LLM can rest
        proactively). No-op (returns False) if already resting."""
        if self.active:
            return False
        ate = _try_eat_and_drink(session, world)
        if not ate:
            session._send_packet(CMSG_STANDSTATECHANGE, struct.pack('<I', UNIT_STAND_STATE_SIT))
        self.active = True
        self.method = "food" if ate else "sit"
        _record_event(session, "resting_started", method=self.method)
        return True

    def stop(self, session, reason: str = "stopped") -> bool:
        if not self.active:
            return False
        if self.method == "sit":
            session._send_packet(CMSG_STANDSTATECHANGE, struct.pack('<I', UNIT_STAND_STATE_STAND))
        self.active = False
        self.method = None
        _record_event(session, "resting_stopped", reason=reason)
        return True

    def tick(self, session, world):
        """One reflex step. Safe to call repeatedly — production:
        agent/__main__.py's reflex loop; tests: call directly with a fake
        session/world. No-op besides the checks below."""
        me = world.get_my_object()
        if self.active:
            if _in_combat(me):
                self.stop(session, reason="aggro")
                return
            if me is not None and me.health is not None and me.max_health:
                if me.health / me.max_health >= REST_STOP_HP_PCT:
                    self.stop(session, reason="recovered")
            return
        if needs_rest(me):
            self.start(session, world)


def get_rest_reflex(session) -> RestReflex:
    """One RestReflex per session, created lazily and cached on it —
    mirrors agent.reflexes.follow.get_follow_reflex()."""
    reflex = getattr(session, "_rest_reflex", None)
    if reflex is None:
        reflex = RestReflex()
        session._rest_reflex = reflex
    return reflex


@actions.register
class RestAction(actions.Action):
    name = "rest"
    description = ("Rest to recover HP/mana: eats/drinks if inventory has food, otherwise "
                    "sits. Stops automatically once healed or attacked. This also runs as an "
                    "automatic reflex whenever HP/mana drop low out of combat, so this action "
                    "is mainly for resting proactively before a fight you can see coming.")
    params = {}
    required = ()

    def check(self, session, world, **_):
        if _in_combat(world.get_my_object()):
            return "cannot rest while in combat"
        return None

    def execute(self, session, world, **_) -> actions.ActionResult:
        reflex = get_rest_reflex(session)
        started = reflex.start(session, world)
        return actions.ActionResult(ok=True, detail={"resting": True, "method": reflex.method,
                                                       "already_resting": not started})
