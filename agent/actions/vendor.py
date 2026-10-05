"""NPC interaction actions: interact, gossip_select, buy_item, sell_item, train_spell (UM-40).

Split out of the former agent/actions.py (issue #248).
"""

import time

from .. import npc
from .. import quests as qu
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


# ── NPC interaction (UM-40) ───────────────────────────────────────────────
# Depends on UM-36 (this Action framework) and UM-35 (name cache, which
# feeds npc_flags detection via agent/perception.py — see ObjectInfo.
# is_gossip/is_vendor/is_trainer/is_quest_giver). Opcodes/layouts live in
# agent/npc.py; response parsing lands in agent.perception.WorldState's
# ui_state, exposed to the LLM as snapshot()'s 'window' key.

@register
class InteractAction(Action):
    name = "interact"
    description = ("Interact with a nearby NPC or gameobject by handle: opens its gossip, "
                    "vendor, or trainer window (whichever its flags indicate), or activates "
                    "it directly if it's a gameobject. Must be within "
                    f"{npc.INTERACT_RANGE_YD} yd — use move_towards first if not.")
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the NPC or gameobject to interact with."},
    }
    required = ("guid",)

    def check(self, session, world, guid: int, **_) -> str | None:
        target = world.get_object(guid)
        if target is None:
            return f"guid {guid:#x} is not currently perceived"
        if session.player_position is None:
            return "own position unknown"
        distance = target.distance_to(session.player_position)
        if distance is not None and distance > npc.INTERACT_RANGE_YD:
            return (f"guid {guid:#x} is {distance:.1f} yd away, out of interact range "
                    f"({npc.INTERACT_RANGE_YD} yd) — try move_towards first")
        return None

    def execute(self, session, world, guid: int, **_) -> ActionResult:
        target = world.get_object(guid)

        if target.object_type == "gameobject":
            send(session, npc.CMSG_GAMEOBJ_USE, npc.build_gameobj_use(guid))
            return ActionResult(ok=True, detail={"guid": guid, "kind": "gameobject_use"})

        # Gossip takes priority over a bare questgiver hello — real NPCs
        # with vendor/trainer/quest flags almost always also have gossip
        # and reach their quest/vendor/trainer window *through* the gossip
        # menu (agent.npc.parse_gossip_message already carries the offered
        # quests list); only a questgiver with no gossip flag needs the
        # dedicated CMSG_QUESTGIVER_HELLO flow (UM-41).
        if target.is_gossip():
            send(session, npc.CMSG_GOSSIP_HELLO, npc.build_gossip_hello(guid))
            return ActionResult(ok=True, detail={"guid": guid, "kind": "gossip_hello"})
        if target.is_quest_giver():
            send(session, qu.CMSG_QUESTGIVER_HELLO, qu.build_questgiver_hello(guid))
            return ActionResult(ok=True, detail={"guid": guid, "kind": "questgiver_hello"})
        if target.is_vendor():
            send(session, npc.CMSG_LIST_INVENTORY, npc.build_list_inventory(guid))
            return ActionResult(ok=True, detail={"guid": guid, "kind": "list_inventory"})
        if target.is_trainer():
            send(session, npc.CMSG_TRAINER_LIST, npc.build_trainer_list(guid))
            return ActionResult(ok=True, detail={"guid": guid, "kind": "trainer_list"})
        if target.is_lootable():
            return ActionResult(ok=False, error="lootable corpses are out of scope for interact() (see UM-42)")
        return ActionResult(ok=False, error="no known interaction for this target "
                                             "(no gossip/vendor/trainer npc flag, not a gameobject)")


@register
class GossipSelectAction(Action):
    name = "gossip_select"
    description = "Select an option in the currently open gossip window by its index."
    params = {
        "option_index": {"type": "integer", "description": "The option's `index` from the open gossip window."},
        "code": {"type": "string", "description": "Text for options that require a text-entry box (rare; "
                                                    "the window's option marks `coded: true` when needed)."},
    }
    required = ("option_index",)

    def check(self, session, world, option_index: int, code: str | None = None, **_) -> str | None:
        window = world.get_ui_state()
        if window is None or window.get("kind") != "gossip":
            return "no gossip window is open"
        if not any(o["index"] == option_index for o in window.get("options", [])):
            return f"option_index {option_index} is not present in the open gossip menu"
        return None

    def execute(self, session, world, option_index: int, code: str | None = None, **_) -> ActionResult:
        window = world.get_ui_state()
        guid, menu_id = window["npc_guid"], window["menu_id"]
        send(session, npc.CMSG_GOSSIP_SELECT_OPTION,
                              npc.build_gossip_select_option(guid, menu_id, option_index, code=code))
        return ActionResult(ok=True, detail={"guid": guid, "menu_id": menu_id, "option_index": option_index})


def _find_vendor_item(window: dict, slot: int | None, entry: int | None) -> dict | None:
    for item in window.get("items", []):
        if slot is not None and item["slot"] == slot:
            return item
        if slot is None and entry is not None and item["entry"] == entry:
            return item
    return None


@register
class BuyItemAction(Action):
    name = "buy_item"
    description = ("Buy an item from the vendor window currently open for vendor_guid, "
                    "identified by vendor slot or item entry (from the window's items list). "
                    "Waits for the server's confirmation/failure response before returning, so "
                    "ok=True means the purchase actually went through.")
    params = {
        "vendor_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the open vendor."},
        "slot": {"type": "integer", "description": "Vendor slot from the open window's items list."},
        "entry": {"type": "integer", "description": "Item entry, as an alternative to slot."},
        "count": {"type": "integer", "description": "How many to buy. Default 1."},
    }
    required = ("vendor_guid",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, vendor_guid: int, slot: int | None = None,
              entry: int | None = None, count: int = 1, **_) -> str | None:
        if slot is None and entry is None:
            return "buy_item requires slot or entry"
        window = world.get_ui_state()
        if window is None or window.get("kind") != "vendor" or window.get("vendor_guid") != vendor_guid:
            return "no vendor window is open for that guid"
        if _find_vendor_item(window, slot, entry) is None:
            return "item not found in the open vendor window (by slot or entry)"
        return None

    def execute(self, session, world, vendor_guid: int, slot: int | None = None,
                entry: int | None = None, count: int = 1, **_) -> ActionResult:
        window = world.get_ui_state()
        item = _find_vendor_item(window, slot, entry)
        sent_at = time.monotonic()
        send(session, npc.CMSG_BUY_ITEM,
                              npc.build_buy_item(vendor_guid, item["entry"], item["slot"], count))

        # SMSG_BUY_ITEM (personal ack on success, via Player::BuyItemFromVendorSlot
        # -> SendDirectMessage) and SMSG_BUY_FAILED (personal ack on failure, via
        # Player::SendBuyError) are both sent directly to us — mirror
        # CastSpellAction's SMSG_CAST_FAILED pattern and wait for whichever
        # arrives first, tied to this vendor/item.
        def find_outcome():
            for e in session.events:
                if e.get("t", 0) < sent_at or e.get("vendor_guid") != vendor_guid:
                    continue
                if e.get("kind") == "buy_item" and e.get("slot") == item["slot"]:
                    return e
                if e.get("kind") == "buy_failed" and e.get("item_entry") == item["entry"]:
                    return e
            return None

        outcome = _wait_for_value(find_outcome, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"vendor_guid": vendor_guid, "slot": item["slot"], "entry": item["entry"], "count": count}
        if outcome is None:
            detail["error"] = "no buy confirmation seen (timed out)"
            return ActionResult(ok=False, error="no buy confirmation seen (timed out)", detail=detail)
        detail["outcome"] = outcome
        if outcome["kind"] == "buy_failed":
            return ActionResult(ok=False, error=outcome.get("reason_name", "buy failed"), detail=detail)
        return ActionResult(ok=True, detail=detail)


@register
class SellItemAction(Action):
    name = "sell_item"
    description = ("Sell an item at bag/slot to the vendor window currently open for "
                    "vendor_guid. Resolves the item's GUID from bag/slot (UM-42's inventory "
                    "model) and waits for the server's confirmation/failure response before "
                    "returning.")
    params = {
        "vendor_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the vendor to sell to."},
        "bag": {"type": "integer", "description": "Bag byte of the item (255 = equipped items/backpack)."},
        "slot": {"type": "integer", "description": "Inventory slot of the item."},
        "count": {"type": "integer", "description": "How many to sell from the stack. Default: whole stack."},
    }
    required = ("vendor_guid", "bag", "slot")
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, vendor_guid: int, bag: int, slot: int,
              count: int | None = None, **_) -> str | None:
        window = world.get_ui_state()
        if window is None or window.get("kind") != "vendor" or window.get("vendor_guid") != vendor_guid:
            return "no vendor window is open for that guid"
        if _find_item_guid(world, bag, slot) is None:
            return f"no known item at bag={bag} slot={slot}"
        return None

    def execute(self, session, world, vendor_guid: int, bag: int, slot: int,
                count: int | None = None, **_) -> ActionResult:
        item_guid = _find_item_guid(world, bag, slot)
        sent_at = time.monotonic()
        send(session, npc.CMSG_SELL_ITEM,
                              npc.build_sell_item(vendor_guid, item_guid, count or 0))

        # Same fire-and-forget pattern as buy_item: only a failure carries an
        # explicit personal ack (SMSG_SELL_ITEM has no confirmation payload
        # worth waiting on), so wait briefly for sell_failed and otherwise
        # treat the timeout as success.
        def find_failure():
            for e in session.events:
                if (e.get("t", 0) >= sent_at and e.get("kind") == "sell_failed"
                        and e.get("vendor_guid") == vendor_guid and e.get("item_guid") == item_guid):
                    return e
            return None

        failure = _wait_for_value(find_failure, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"vendor_guid": vendor_guid, "bag": bag, "slot": slot, "item_guid": item_guid,
                   "count": count}
        if failure is not None:
            detail["failure"] = failure
            return ActionResult(ok=False, error=failure.get("reason_name", "sell failed"), detail=detail)
        return ActionResult(ok=True, detail=detail)


@register
class TrainSpellAction(Action):
    name = "train_spell"
    description = ("Learn a spell from the trainer window currently open for trainer_guid. "
                    "Waits for the server's confirmation/failure response before returning, so "
                    "ok=True means the spell was actually learned.")
    params = {
        "trainer_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the open trainer."},
        "spell_id": {"type": "integer", "description": "Spell ID from the open trainer window's spells list."},
    }
    required = ("trainer_guid", "spell_id")
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, trainer_guid: int, spell_id: int, **_) -> str | None:
        window = world.get_ui_state()
        if window is None or window.get("kind") != "trainer" or window.get("trainer_guid") != trainer_guid:
            return "no trainer window is open for that guid"
        if not any(s["spell_id"] == spell_id for s in window.get("spells", [])):
            return f"spell {spell_id} is not offered by the open trainer window"
        return None

    def execute(self, session, world, trainer_guid: int, spell_id: int, **_) -> ActionResult:
        sent_at = time.monotonic()
        send(session, npc.CMSG_TRAINER_BUY_SPELL, npc.build_train_spell(trainer_guid, spell_id))

        def find_outcome():
            for e in session.events:
                if (e.get("t", 0) >= sent_at and e.get("kind") in ("train_succeeded", "train_failed")
                        and e.get("trainer_guid") == trainer_guid and e.get("spell_id") == spell_id):
                    return e
            return None

        outcome = _wait_for_value(find_outcome, timeout=self.confirm_timeout, interval=self.confirm_interval)
        detail = {"trainer_guid": trainer_guid, "spell_id": spell_id}
        if outcome is None:
            return ActionResult(ok=False, error="no training confirmation seen (timed out)", detail=detail)
        detail["outcome"] = outcome
        if outcome["kind"] == "train_failed":
            return ActionResult(ok=False, error=outcome.get("reason_name", "training failed"), detail=detail)
        return ActionResult(ok=True, detail=detail)
