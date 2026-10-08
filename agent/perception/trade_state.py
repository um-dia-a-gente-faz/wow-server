"""WorldState's player-trade state (UM-59)."""

import time

from .. import trade as trade_mod


class TradeMixin:
    # ── Trade (UM-59) ──────────────────────────────────────────────────────
    # Every offered item's name is resolved best-effort via self.items (the
    # same item-template cache build_equipment_and_inventory uses) — a bare
    # `entry` is kept even if the name hasn't resolved yet, same policy as
    # everywhere else in this class.

    @staticmethod

    def _new_trade_state(partner_guid: int, initiated_by_me: bool) -> dict:
        """A trade is now pending — either we just sent CMSG_INITIATE_TRADE
        (optimistic, before any server reply) or we just received an
        incoming TRADE_STATUS_BEGIN_TRADE. `initiated_by_me` gates
        accept_trade_request() — only the *receiving* side can accept an
        incoming request, matching the real client (the initiator has no
        "incoming request" popup to click)."""
        now = time.monotonic()
        return {
            "phase": "requested", "partner_guid": partner_guid,
            "initiated_by_me": initiated_by_me,
            "my_gold": 0, "their_gold": 0, "my_items": {}, "their_items": {},
            "my_accepted": False, "their_accepted": False,
            "opened_at": now, "last_activity_at": now,
        }

    def get_trade(self) -> dict | None:
        """Thread-safe read of the currently open/pending trade (UM-59),
        for agent/actions/trade.py to check against without racing the recv thread's
        apply_trade_status/apply_trade_status_extended."""
        with self._lock:
            return self.trade

    def start_trade_request(self, partner_guid: int, initiated_by_me: bool):
        with self._lock:
            self.trade = self._new_trade_state(partner_guid, initiated_by_me)

    def clear_trade(self):
        with self._lock:
            self.trade = None

    def set_my_trade_item(self, trade_slot: int, item: dict):
        """Optimistic local update after we send CMSG_SET_TRADE_ITEM — the
        server never echoes our own offer back to us (see agent/trade.py's
        docstring), so this is the only place `my_items` ever gets set."""
        with self._lock:
            if self.trade is not None:
                self.trade["my_items"][trade_slot] = item
                self.trade["last_activity_at"] = time.monotonic()

    def clear_my_trade_item(self, trade_slot: int):
        with self._lock:
            if self.trade is not None:
                self.trade["my_items"].pop(trade_slot, None)
                self.trade["last_activity_at"] = time.monotonic()

    def set_my_trade_gold(self, copper: int):
        with self._lock:
            if self.trade is not None:
                self.trade["my_gold"] = copper
                self.trade["last_activity_at"] = time.monotonic()

    def set_my_trade_accepted(self):
        with self._lock:
            if self.trade is not None:
                self.trade["my_accepted"] = True
                self.trade["last_activity_at"] = time.monotonic()

    def apply_trade_status(self, data: dict) -> tuple[str, dict] | None:
        """SMSG_TRADE_STATUS (agent.trade.parse_trade_status): advance the
        trade phase and return (event_kind, event_fields) for session.py to
        record via _record_event, or None if this status doesn't warrant
        one. Deciding "does this status end the trade, and with what event"
        lives here (not in session.py) because it needs the trade state
        from *before* this status arrived — e.g. building trade_completed's
        summary. Non-terminal statuses (open/accept/back-to-trade/a single
        rejected slot) are handled individually below; every other status
        this module doesn't special-case ends the trade — matches a real
        client, which closes its trade UI on any status it doesn't
        recognize as "still negotiating" (agent/trade.py's docstring has
        the full status-by-status reasoning from TradeHandler.cpp)."""
        status = data["status"]
        with self._lock:
            if status == trade_mod.TRADE_STATUS_BEGIN_TRADE:
                partner_guid = data["trader_guid"]
                self.trade = self._new_trade_state(partner_guid, initiated_by_me=False)
                return ("trade_requested", {"by": partner_guid})

            if self.trade is None:
                return None  # status about a trade we're not tracking (e.g. a stale reply) — nothing to update

            if status == trade_mod.TRADE_STATUS_OPEN_WINDOW:
                self.trade["phase"] = "open"
                return None
            if status == trade_mod.TRADE_STATUS_TRADE_ACCEPT:
                self.trade["their_accepted"] = True
                self.trade["last_activity_at"] = time.monotonic()
                return None
            if status == trade_mod.TRADE_STATUS_BACK_TO_TRADE:
                self.trade["my_accepted"] = False
                self.trade["their_accepted"] = False
                self.trade["last_activity_at"] = time.monotonic()
                return None
            if status == trade_mod.TRADE_STATUS_NOT_ON_TAPLIST:
                return ("trade_offer_rejected", {"slot": data.get("slot"), "reason": "not_on_taplist"})

            # Every other status ends the trade (see _NON_TERMINAL_TRADE_STATUSES).
            summary = {
                "partner_guid": self.trade["partner_guid"],
                "my_gold": self.trade["my_gold"], "their_gold": self.trade["their_gold"],
                "my_items": list(self.trade["my_items"].values()),
                "their_items": list(self.trade["their_items"].values()),
            }
            self.trade = None
            if status == trade_mod.TRADE_STATUS_TRADE_COMPLETE:
                return ("trade_completed", {"summary": summary})
            reason = data.get("status_name", f"status_{status}")
            if status == trade_mod.TRADE_STATUS_CLOSE_WINDOW:
                reason = data.get("result_name", reason)
            return ("trade_cancelled", {"reason": reason, "summary": summary})

    def apply_trade_status_extended(self, data: dict) -> dict | None:
        """SMSG_TRADE_STATUS_EXTENDED (agent.trade.parse_trade_status_extended):
        only ever describes the *other* side's offer in practice (see
        agent/trade.py's docstring) — backfill `their_items`/`their_gold`
        with item names resolved via the item cache, and return the new
        offer for session.py to fire trade_offer_changed with, or None if
        there's no trade to update (a stale reply) or this is somehow our
        own data (`is_trader_data` False — not modeled, nothing to do)."""
        if not data.get("is_trader_data"):
            return None
        with self._lock:
            if self.trade is None:
                return None
            items = {}
            for slot, item in data["items"].items():
                entry = item["entry"]
                name = None
                cached = self.items.items.get(entry)
                if cached is not None:
                    name = cached["name"]
                else:
                    self.items.want_item(entry)
                items[slot] = {**item, "name": name}
            self.trade["their_items"] = items
            self.trade["their_gold"] = data["money"]
            self.trade["last_activity_at"] = time.monotonic()
            return {"gold": data["money"], "items": list(items.values())}
