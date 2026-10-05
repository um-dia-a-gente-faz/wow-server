"""Loot and inventory actions: loot, use_item, destroy_item, compare_items, equip_item (UM-42, UM-69).

Split out of the former agent/actions.py (issue #248).
"""

import time

from .. import item_compare
from .. import loot as lootmod
from .base import (
    Action,
    ActionResult,
    DEFAULT_CONFIRM_POLL_S,
    DEFAULT_CONFIRM_TIMEOUT_S,
    _find_item_guid,
    _wait_for_value,
    register,
    send,
)


# ── Loot / inventory (UM-42) ──────────────────────────────────────────────
#
# Wire layouts verified against TrinityCore branch `3.3.5`:
#   src/server/game/Handlers/LootHandler.cpp (CMSG_LOOT/_MONEY/_RELEASE,
#     CMSG_AUTOSTORE_LOOT_ITEM)
#   src/server/game/Server/Packets/LootPackets.h/.cpp (SMSG_LOOT_RESPONSE and
#     friends)
#   src/server/game/Entities/Player/Player.cpp (SendLoot/SendLootRelease/
#     SendLootError/SendNewItem — build several of these packets by hand)
#   src/server/game/Handlers/SpellHandler.cpp (HandleUseItemOpcode)
#   src/server/game/Server/Packets/ItemPackets.cpp (CMSG_DESTROYITEM)
# See agent/loot.py for the byte-level detail on each of these.

LOOT_RANGE_YD = 5.0
UNIT_FLAG_IN_COMBAT = 0x00080000  # UnitDefines.h — same bit perception.py's _object_dict uses


@register
class LootAction(Action):
    name = "loot"
    description = ("Loot a nearby lootable corpse/creature (UNIT_DYNFLAG_LOOTABLE): opens the "
                    "loot window, takes all money and every item (v1: take-everything, no "
                    "selective looting), then releases it. Must be within 5 yards.")
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the lootable corpse/creature."},
    }
    required = ("guid",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, guid: int, **_) -> str | None:
        target = world.get_object(guid)
        if target is None:
            return f"guid {guid:#x} is not currently perceived"
        if not target.is_lootable():
            return f"guid {guid:#x} is not lootable"
        if session.player_position is None:
            return "own position unknown"
        distance = target.distance_to(session.player_position)
        if distance is not None and distance > LOOT_RANGE_YD:
            return (f"guid {guid:#x} is {distance:.1f} yd away, out of loot range "
                    f"({LOOT_RANGE_YD} yd) — try move_towards first")
        return None

    def execute(self, session, world, guid: int, **_) -> ActionResult:
        sent_at = time.monotonic()
        send(session, lootmod.CMSG_LOOT, lootmod.build_loot(guid))

        def find_loot_response():
            for e in session.events:
                if e.get("t", 0) >= sent_at and e.get("kind") == "loot_response" and e.get("guid") == guid:
                    return e
            return None

        response = _wait_for_value(find_loot_response, timeout=self.confirm_timeout,
                                    interval=self.confirm_interval)
        if response is None:
            return ActionResult(ok=False, error="no loot response seen (timed out)", detail={"guid": guid})
        if not response.get("success"):
            return ActionResult(ok=False, error=response.get("failure_reason_name", "loot failed"),
                                 detail=response)

        coins = response.get("coins", 0)
        if coins:
            send(session, lootmod.CMSG_LOOT_MONEY, lootmod.build_loot_money())

        looted_items = list(response.get("items", []))
        for item in looted_items:
            send(session, lootmod.CMSG_AUTOSTORE_LOOT_ITEM,
                                  lootmod.build_autostore_loot_item(item["slot"]))

        def find_inventory_failure():
            for e in session.events:
                if e.get("t", 0) >= sent_at and e.get("kind") == "inventory_change_failure":
                    return e
            return None

        # A short, non-configurable wait: long enough for the server's
        # autostore replies to land, short enough not to meaningfully slow
        # every loot down when nothing goes wrong.
        failure = _wait_for_value(find_inventory_failure, timeout=0.3, interval=self.confirm_interval)

        send(session, lootmod.CMSG_LOOT_RELEASE, lootmod.build_loot_release(guid))

        detail = {"guid": guid, "coins": coins, "items": looted_items}
        if failure is not None:
            detail["inventory_error"] = failure
            return ActionResult(ok=False, error="inventory_change_failure while storing loot", detail=detail)
        return ActionResult(ok=True, detail=detail)


@register
class UseItemAction(Action):
    name = "use_item"
    description = ("Use/consume an item from inventory — e.g. eat food or drink water. Not usable "
                    "while in combat (server-enforced for most consumables; checked here too).")
    params = {
        "bag": {"type": "integer", "description": "Bag byte (255 = equipped items/backpack; "
                                                    "see CMSG_USE_ITEM's bag field)."},
        "slot": {"type": "integer", "description": "Slot within that bag (0-18 equipment, "
                                                     "19-22 equipped bags, 23-38 backpack)."},
        "target_guid": {"type": "string", "description": "Optional target snapshot handle (a string like \"p1\"), e.g. a bandage "
                                                            "used on an ally. Omit to target self."},
    }
    required = ("bag", "slot")

    def check(self, session, world, bag: int, slot: int, target_guid: int | None = None, **_) -> str | None:
        me = world.get_my_object()
        if me is not None and me.unit_flags and (me.unit_flags & UNIT_FLAG_IN_COMBAT):
            return "cannot use items while in combat"
        if _find_item_guid(world, bag, slot) is None:
            return f"no known item at bag={bag} slot={slot}"
        return None

    def execute(self, session, world, bag: int, slot: int, target_guid: int | None = None, **_) -> ActionResult:
        item_guid = _find_item_guid(world, bag, slot)
        spell_id = 0
        item_obj = world.get_object(item_guid) if item_guid else None
        if item_obj is not None and item_obj.entry is not None:
            item_info = world.items.items.get(item_obj.entry)
            if item_info:
                for s in item_info.get("spells", []):
                    if s["trigger"] == lootmod.ITEM_SPELLTRIGGER_ON_USE:
                        spell_id = s["spell_id"]
                        break
        send(session, 
            lootmod.CMSG_USE_ITEM,
            lootmod.build_use_item(bag, slot, item_guid, spell_id=spell_id, target_guid=target_guid))
        return ActionResult(ok=True, detail={"bag": bag, "slot": slot, "item_guid": item_guid,
                                              "spell_id": spell_id})


@register
class DestroyItemAction(Action):
    name = "destroy_item"
    description = ("Permanently destroy `count` of the item at bag/slot. Irreversible — requires "
                    "confirm=true as an explicit safety guard against accidental calls.")
    params = {
        "bag": {"type": "integer", "description": "Bag byte (255 = equipped items/backpack)."},
        "slot": {"type": "integer", "description": "Slot within that bag."},
        "count": {"type": "integer", "description": "How many to destroy from the stack."},
        "confirm": {"type": "boolean", "description": "Must be true — a safety guard, not read by "
                                                        "the server."},
    }
    required = ("bag", "slot", "count", "confirm")

    def check(self, session, world, bag: int, slot: int, count: int, confirm: bool = False,
              **_) -> str | None:
        if not confirm:
            return "destroy_item requires confirm=true"
        if count <= 0:
            return "count must be positive"
        return None

    def execute(self, session, world, bag: int, slot: int, count: int, confirm: bool = False,
                **_) -> ActionResult:
        send(session, lootmod.CMSG_DESTROYITEM, lootmod.build_destroy_item(bag, slot, count))
        return ActionResult(ok=True, detail={"bag": bag, "slot": slot, "count": count})


# ── Item comparison / equip (UM-69) ───────────────────────────────────────
#
# Wire layout for CMSG_AUTOEQUIP_ITEM verified against TrinityCore branch
# `3.3.5` (src/server/game/Handlers/ItemHandler.cpp,
# HandleAutoEquipItemOpcode) — see agent/loot.py's build_autoequip_item for
# the byte-level detail (opcode 0x10A fixed in #260; not yet confirmed live).
# Scoring/usability heuristics live in agent/item_compare.py (v1: primary
# stat for class + item level tiebreak, no talent/spec awareness).


def _resolve_item_template(world, bag: int, slot: int):
    """(item_guid, entry, template_dict) for the item at bag/slot, or
    (None, None, None) if there's no known item there. If the item's guid/
    entry are known but its template (name/stats/armor/...) hasn't come
    back from CMSG_ITEM_QUERY_SINGLE yet, returns (guid, entry, None) —
    also requests the query (world.items.want_item) so it'll be available
    on a later call. Callers treat a None template as "not enough
    information yet", never as a crash."""
    item_guid = _find_item_guid(world, bag, slot)
    if item_guid is None:
        return None, None, None
    item_obj = world.get_object(item_guid)
    if item_obj is None or item_obj.entry is None:
        return item_guid, None, None
    world.items.want_item(item_obj.entry)
    template = world.items.items.get(item_obj.entry)
    return item_guid, item_obj.entry, template


@register
class CompareItemsAction(Action):
    name = "compare_items"
    description = ("Compare two items at given bag/slot positions for the character's own class "
                    "(UM-69 v1 heuristic: primary stat for class + item level as a tiebreaker, no "
                    "talent/spec awareness — see agent.item_compare's docstring). Returns which "
                    "item scores higher and a human-readable reason.")
    params = {
        "bag_a": {"type": "integer", "description": "Bag byte of the first item (255 = equipped items/backpack)."},
        "slot_a": {"type": "integer", "description": "Inventory slot of the first item."},
        "bag_b": {"type": "integer", "description": "Bag byte of the second item (255 = equipped items/backpack)."},
        "slot_b": {"type": "integer", "description": "Inventory slot of the second item."},
    }
    required = ("bag_a", "slot_a", "bag_b", "slot_b")

    def check(self, session, world, bag_a: int, slot_a: int, bag_b: int, slot_b: int, **_) -> str | None:
        _, _, template_a = _resolve_item_template(world, bag_a, slot_a)
        if template_a is None:
            return f"no known item template at bag={bag_a} slot={slot_a} (not queried yet, or empty slot)"
        _, _, template_b = _resolve_item_template(world, bag_b, slot_b)
        if template_b is None:
            return f"no known item template at bag={bag_b} slot={slot_b} (not queried yet, or empty slot)"
        if template_a.get("inventory_type") != template_b.get("inventory_type"):
            return (f"items are for different equip slots (inventory_type "
                    f"{template_a.get('inventory_type')} vs {template_b.get('inventory_type')})")
        return None

    def execute(self, session, world, bag_a: int, slot_a: int, bag_b: int, slot_b: int, **_) -> ActionResult:
        _, entry_a, template_a = _resolve_item_template(world, bag_a, slot_a)
        _, entry_b, template_b = _resolve_item_template(world, bag_b, slot_b)
        class_id = getattr(session, "class_", 0)
        result = item_compare.compare(template_a, template_b, class_id)
        detail = {
            "entry_a": entry_a, "entry_b": entry_b,
            "usability_error_a": item_compare.usability_error(template_a, class_id),
            "usability_error_b": item_compare.usability_error(template_b, class_id),
            **result,
        }
        return ActionResult(ok=True, detail=detail)


@register
class EquipItemAction(Action):
    name = "equip_item"
    description = ("Equip the item at bag/slot (CMSG_AUTOEQUIP_ITEM) — the server picks the equip "
                    "slot from the item itself. Refuses up front if the item's cached template says "
                    "it can't be used by this character (wrong armor type, class-restricted); "
                    "otherwise waits for the server's confirmation/failure response before "
                    "returning, so ok=True means the item was actually equipped.")
    params = {
        "bag": {"type": "integer", "description": "Bag byte of the item (255 = equipped items/backpack)."},
        "slot": {"type": "integer", "description": "Inventory slot of the item."},
    }
    required = ("bag", "slot")
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, bag: int, slot: int, **_) -> str | None:
        item_guid, _, template = _resolve_item_template(world, bag, slot)
        if item_guid is None:
            return f"no known item at bag={bag} slot={slot}"
        if template is not None:
            error = item_compare.usability_error(template, getattr(session, "class_", 0))
            if error is not None:
                return error
        return None

    def execute(self, session, world, bag: int, slot: int, **_) -> ActionResult:
        item_guid, entry, _ = _resolve_item_template(world, bag, slot)
        sent_at = time.monotonic()
        send(session, lootmod.CMSG_AUTOEQUIP_ITEM, lootmod.build_autoequip_item(bag, slot))

        def find_failure():
            for e in session.events:
                if e.get("t", 0) >= sent_at and e.get("kind") == "inventory_change_failure":
                    return e
            return None

        failure = _wait_for_value(find_failure, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"bag": bag, "slot": slot, "item_guid": item_guid, "entry": entry}
        if failure is not None:
            detail["failure"] = failure
            return ActionResult(ok=False, error=failure.get("reason_name", "equip failed"), detail=detail)
        return ActionResult(ok=True, detail=detail)
