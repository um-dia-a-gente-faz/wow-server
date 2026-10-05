"""Player trade actions (UM-59).

Split out of the former agent/actions.py (issue #248).
"""

import time

from .. import trade as tr
from .. import update_fields as uf
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


# ── Player trade (UM-59) ──────────────────────────────────────────────────
# Wire layouts verified against TrinityCore branch `3.3.5`, see
# agent/trade.py's docstring for the sources and, importantly, why our own
# offer (my_items/my_gold in world.trade) is tracked optimistically
# client-side rather than read back from a server packet.
#
# Confirmation shape: unlike most other actions' "wait for success-or-
# failure, timeout = failure", trade's own accept/open/accept_request calls
# routinely get *no* server reply at all when they succeed (the other side
# just hasn't acted yet — that's the normal, common case, not a stall). So
# those three wait briefly for a definite rejection and treat "nothing came
# back" as ok=True ("sent, no rejection seen yet"), never as a timeout
# failure. offer_item/offer_gold are different: TradeData::SetItem/SetMoney
# *always* answer the sender (TRADE_STATUS_BACK_TO_TRADE on success,
# TRADE_STATUS_TRADE_CANCELED/NOT_ON_TAPLIST/CLOSE_WINDOW on rejection), so
# those two use the normal wait-for-outcome-or-timeout-fails pattern.

TRADE_CONFIRM_TIMEOUT_S = DEFAULT_CONFIRM_TIMEOUT_S


def _find_trade_status_event(session, sent_at: float, status_names: tuple) -> dict | None:
    for e in session.events:
        if e.get("t", 0) < sent_at or e.get("kind") != "trade_status":
            continue
        if e.get("status_name") in status_names:
            return e
    return None


def _find_event_since(session, sent_at: float, kind: str) -> dict | None:
    for e in session.events:
        if e.get("t", 0) >= sent_at and e.get("kind") == kind:
            return e
    return None


def _resolve_offered_item(world, bag: int, slot: int):
    """(item_guid, entry, name, count, is_soulbound) for the item at
    bag/slot, or None if there's no known item there. `is_soulbound` reads
    the item *instance*'s own ITEM_FIELD_FLAGS (Item::IsSoulBound(),
    ItemTemplate.h ITEM_FIELD_FLAG_SOULBOUND) — distinct from the static
    template's Bonding type: a BoE item only becomes soulbound once bound,
    and this flag reflects that actual current state, not the item type."""
    item_guid = _find_item_guid(world, bag, slot)
    if item_guid is None:
        return None
    item_obj = world.get_object(item_guid)
    if item_obj is None:
        return None
    decoded = uf.decode_item_fields(item_obj.raw_fields) if item_obj.raw_fields else {}
    flags = decoded.get("item_flags") or 0
    is_soulbound = bool(flags & tr.ITEM_FIELD_FLAG_SOULBOUND)
    name = item_obj.name or None
    if name is None and item_obj.entry is not None:
        cached = world.items.items.get(item_obj.entry)
        if cached:
            name = cached["name"]
    return {"guid": item_guid, "entry": item_obj.entry, "name": name,
            "count": decoded.get("count"), "is_soulbound": is_soulbound}


@register
class OpenTradeAction(Action):
    name = "open_trade"
    description = (f"Request a trade with a nearby player. Must be within {tr.TRADE_DISTANCE_YD:.1f} "
                    "yd. The other side sees the request and decides via accept_trade_request.")
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the player to trade with."},
    }
    required = ("guid",)
    confirm_timeout = TRADE_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, guid: int, **_) -> str | None:
        if world.get_trade() is not None:
            return "a trade is already pending/open — cancel_trade first"
        target = world.get_object(guid)
        if target is None:
            return f"guid {guid:#x} is not currently perceived"
        if not target.is_player():
            return f"guid {guid:#x} is not a player"
        if session.player_position is not None:
            distance = target.distance_to(session.player_position)
            if distance is not None and distance > tr.TRADE_DISTANCE_YD:
                return (f"guid {guid:#x} is {distance:.1f} yd away, out of trade range "
                        f"({tr.TRADE_DISTANCE_YD:.1f} yd) — try move_towards first")
        return None

    def execute(self, session, world, guid: int, **_) -> ActionResult:
        sent_at = time.monotonic()
        world.start_trade_request(guid, initiated_by_me=True)
        send(session, tr.CMSG_INITIATE_TRADE, tr.build_initiate_trade(guid))

        failure = _wait_for_value(lambda: _find_event_since(session, sent_at, "trade_cancelled"),
                                   timeout=self.confirm_timeout, interval=self.confirm_interval)
        if failure is not None:
            return ActionResult(ok=False, error=failure.get("reason", "trade request rejected"),
                                 detail=failure)
        return ActionResult(ok=True, detail={"guid": guid, "trade": world.get_trade()})


@register
class AcceptTradeRequestAction(Action):
    name = "accept_trade_request"
    description = "Accept a pending incoming trade request (see the trade window's `phase`: 'requested')."
    params = {}
    required = ()
    confirm_timeout = TRADE_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, **_) -> str | None:
        trade = world.get_trade()
        if trade is None or trade["phase"] != "requested":
            return "no incoming trade request is pending"
        if trade["initiated_by_me"]:
            return "you initiated this trade; wait for the other side to accept, don't accept your own request"
        return None

    def execute(self, session, world, **_) -> ActionResult:
        sent_at = time.monotonic()
        send(session, tr.CMSG_BEGIN_TRADE, tr.build_begin_trade())

        failure = _wait_for_value(lambda: _find_event_since(session, sent_at, "trade_cancelled"),
                                   timeout=self.confirm_timeout, interval=self.confirm_interval)
        if failure is not None:
            return ActionResult(ok=False, error=failure.get("reason", "trade request expired"),
                                 detail=failure)
        return ActionResult(ok=True, detail={"trade": world.get_trade()})


@register
class OfferItemAction(Action):
    name = "offer_item"
    description = ("Put an item from your bag/slot into the open trade's next free slot "
                    "(0-5; pass trade_slot to pick a specific one). Offers the item's whole "
                    "stack — trade doesn't support splitting a partial count. Rejected if the "
                    "item is soulbound.")
    params = {
        "bag": {"type": "integer", "description": "Bag byte of the item (255 = equipped items/backpack)."},
        "slot": {"type": "integer", "description": "Inventory slot of the item."},
        "trade_slot": {"type": "integer", "description": "Which of the 6 trade slots (0-5) to put "
                                                           "it in. Default: the first free one."},
    }
    required = ("bag", "slot")
    confirm_timeout = TRADE_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def _pick_trade_slot(self, world, trade_slot: int | None) -> int | str:
        trade = world.get_trade()
        if trade_slot is not None:
            if not (0 <= trade_slot < tr.TRADE_SLOT_TRADED_COUNT):
                return f"trade_slot must be 0-{tr.TRADE_SLOT_TRADED_COUNT - 1}"
            return trade_slot
        for s in range(tr.TRADE_SLOT_TRADED_COUNT):
            if s not in trade["my_items"]:
                return s
        return "all 6 trade slots are already offered — pass trade_slot to replace one"

    def check(self, session, world, bag: int, slot: int, trade_slot: int | None = None, **_) -> str | None:
        trade = world.get_trade()
        if trade is None or trade["phase"] != "open":
            return "no trade window is open"
        item = _resolve_offered_item(world, bag, slot)
        if item is None:
            return f"no known item at bag={bag} slot={slot}"
        if item["is_soulbound"]:
            return f"item {item['entry']} is soulbound and can't be traded"
        picked = self._pick_trade_slot(world, trade_slot)
        if isinstance(picked, str):
            return picked
        return None

    def execute(self, session, world, bag: int, slot: int, trade_slot: int | None = None,
                **_) -> ActionResult:
        # Re-checked, not just trusted from check() — found in review
        # (same category as UM-60's mailbox-action fixes): the trade could
        # be cancelled by the partner, or the item moved/consumed, via a
        # concurrent update on the recv thread between check() and
        # execute(). A bare `item["guid"]`/`trade["my_items"]` below would
        # raise TypeError on a None — think.py's `except TypeError` does
        # catch that, but mislabels it as "bad params" even though the
        # LLM's params were fine.
        trade = world.get_trade()
        if trade is None or trade["phase"] != "open":
            return ActionResult(ok=False, error="no trade window is open")
        item = _resolve_offered_item(world, bag, slot)
        if item is None:
            return ActionResult(ok=False, error=f"item at bag={bag} slot={slot} is no longer there")
        picked_slot = self._pick_trade_slot(world, trade_slot)
        if isinstance(picked_slot, str):
            return ActionResult(ok=False, error=picked_slot)
        detail = {"bag": bag, "slot": slot, "trade_slot": picked_slot, "item": item}
        # TradeData::SetItem (TradeData.cpp) early-returns with no reply at
        # all — not even TRADE_STATUS_BACK_TO_TRADE — when this exact item
        # guid is already sitting in this exact trade slot. Found live
        # testing: re-offering the same item into the same slot always
        # timed out even though nothing was actually wrong. Skip the round
        # trip in that case instead of waiting for a confirmation that will
        # never come.
        if trade["my_items"].get(picked_slot, {}).get("guid") == item["guid"]:
            detail["note"] = "already offered at that trade slot — no packet sent"
            return ActionResult(ok=True, detail=detail)
        sent_at = time.monotonic()
        send(session, tr.CMSG_SET_TRADE_ITEM, tr.build_set_trade_item(picked_slot, bag, slot))

        outcome = _wait_for_value(
            lambda: _find_trade_status_event(session, sent_at, ("back_to_trade", "trade_canceled", "not_on_taplist")),
            timeout=self.confirm_timeout, interval=self.confirm_interval)
        if outcome is None:
            return ActionResult(ok=False, error="no trade confirmation seen (timed out)", detail=detail)
        if outcome["status_name"] != "back_to_trade":
            detail["outcome"] = outcome
            return ActionResult(ok=False, error=outcome["status_name"], detail=detail)
        world.set_my_trade_item(picked_slot, item)
        return ActionResult(ok=True, detail=detail)


@register
class OfferGoldAction(Action):
    name = "offer_gold"
    description = "Set the gold (in copper) offered in the open trade. Replaces any previous amount."
    params = {
        "amount": {"type": "integer", "description": "Copper to offer (100 copper = 1 silver, "
                                                       "10000 = 1 gold). 0 clears a previous offer."},
    }
    required = ("amount",)
    confirm_timeout = TRADE_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, amount: int, **_) -> str | None:
        trade = world.get_trade()
        if trade is None or trade["phase"] != "open":
            return "no trade window is open"
        if amount < 0:
            return "amount must be >= 0"
        have = getattr(session, "coinage", 0) or 0
        if amount > have:
            return f"not enough gold (have {have}, tried to offer {amount})"
        return None

    def execute(self, session, world, amount: int, **_) -> ActionResult:
        detail = {"amount": amount}
        # Re-checked, not just trusted from check() — found in review, same
        # TOCTOU category as OfferItemAction above: the trade could be
        # cancelled by the partner between check() and execute().
        trade = world.get_trade()
        if trade is None or trade["phase"] != "open":
            return ActionResult(ok=False, error="no trade window is open", detail=detail)
        # TradeData::SetMoney (TradeData.cpp) early-returns with no reply at
        # all when `amount` equals what's already offered (0 the first time,
        # since my_gold starts at 0) — found live testing: offer_gold(0)
        # right after opening a trade always timed out even though nothing
        # was wrong. Skip the round trip in that case, same fix as
        # OfferItemAction.
        if trade["my_gold"] == amount:
            detail["note"] = "already offering that amount — no packet sent"
            return ActionResult(ok=True, detail=detail)
        sent_at = time.monotonic()
        send(session, tr.CMSG_SET_TRADE_GOLD, tr.build_set_trade_gold(amount))

        outcome = _wait_for_value(
            lambda: _find_trade_status_event(session, sent_at, ("back_to_trade", "close_window")),
            timeout=self.confirm_timeout, interval=self.confirm_interval)
        if outcome is None:
            return ActionResult(ok=False, error="no trade confirmation seen (timed out)", detail=detail)
        if outcome["status_name"] != "back_to_trade":
            detail["outcome"] = outcome
            return ActionResult(ok=False, error=outcome.get("result_name", outcome["status_name"]), detail=detail)
        world.set_my_trade_gold(amount)
        return ActionResult(ok=True, detail=detail)


@register
class AcceptTradeAction(Action):
    name = "accept_trade"
    description = ("Accept the open trade — but only given what you currently believe the other "
                    "side is offering (from the trade window's `their_gold`/`their_items`). "
                    "Rejected if that no longer matches the live offer, so re-check the window "
                    "before calling this again; a change on either side after an accept un-accepts "
                    "both sides automatically (server behavior), so this never lets a stale offer "
                    "through silently.")
    params = {
        "expected_their_gold": {"type": "integer", "description": "Gold you believe the other "
                                                                    "side has offered. Default 0."},
        "expected_their_item_entries": {"type": "array", "items": {"type": "integer"},
                                         "description": "Item entries you believe the other side "
                                                         "has offered (any order). Omit/empty if "
                                                         "you believe they're offering no items."},
    }
    required = ()
    confirm_timeout = TRADE_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, expected_their_gold: int = 0,
              expected_their_item_entries: list | None = None, **_) -> str | None:
        trade = world.get_trade()
        if trade is None or trade["phase"] != "open":
            return "no trade window is open"
        actual_gold = trade["their_gold"]
        if actual_gold != expected_their_gold:
            return (f"the other side's offer has changed: expected {expected_their_gold} copper, "
                    f"they're now offering {actual_gold} — re-check the trade window")
        actual_entries = sorted(item["entry"] for item in trade["their_items"].values())
        expected_entries = sorted(expected_their_item_entries or [])
        if actual_entries != expected_entries:
            return (f"the other side's offer has changed: expected items {expected_entries}, "
                    f"they're now offering {actual_entries} — re-check the trade window")
        return None

    def execute(self, session, world, expected_their_gold: int = 0,
                expected_their_item_entries: list | None = None, **_) -> ActionResult:
        sent_at = time.monotonic()
        send(session, tr.CMSG_ACCEPT_TRADE, tr.build_accept_trade())
        world.set_my_trade_accepted()

        def find_outcome():
            return (_find_event_since(session, sent_at, "trade_completed")
                    or _find_event_since(session, sent_at, "trade_cancelled"))

        outcome = _wait_for_value(find_outcome, timeout=self.confirm_timeout,
                                   interval=self.confirm_interval)
        if outcome is None:
            # The common case: the other side hasn't accepted yet — the
            # server gives the accepting player no ack at all until *both*
            # sides have (see this section's confirmation-shape note above).
            return ActionResult(ok=True, detail={"status": "accepted_locally_waiting_on_other_side"})
        if outcome["kind"] == "trade_completed":
            return ActionResult(ok=True, detail=outcome)
        return ActionResult(ok=False, error=outcome.get("reason", "trade cancelled"), detail=outcome)


@register
class CancelTradeAction(Action):
    name = "cancel_trade"
    description = "Cancel/decline the pending or open trade."
    params = {}
    required = ()
    confirm_timeout = TRADE_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, **_) -> str | None:
        if world.get_trade() is None:
            return "no trade is pending or open"
        return None

    def execute(self, session, world, **_) -> ActionResult:
        sent_at = time.monotonic()
        send(session, tr.CMSG_CANCEL_TRADE, tr.build_cancel_trade())
        # CMSG_CANCEL_TRADE has no rejection path (Player::TradeCancel always
        # succeeds and always answers the sender, per agent/trade.py) — wait
        # for that answer as a nicety, but a slow/missed event here still
        # isn't a real failure the way a timeout is for e.g. buy_item.
        event = _wait_for_value(lambda: _find_event_since(session, sent_at, "trade_cancelled"),
                                 timeout=self.confirm_timeout, interval=self.confirm_interval)
        return ActionResult(ok=True, detail=event or {"status": "cancel_sent"})
