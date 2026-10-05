"""Tests for WorldState's player-trade state (perception/trade_state.py)."""

import unittest

from agent import perception as per
from agent import trade


class TradeStateTest(unittest.TestCase):
    """UM-59: world.trade's phase machine, driven by agent.trade.
    parse_trade_status/parse_trade_status_extended shaped dicts, and the
    optimistic local mutators agent/actions/trade.py calls (the server never echoes our
    own offer back — see agent/trade.py's docstring)."""

    def test_defaults_to_none(self):
        ws = per.WorldState()
        self.assertIsNone(ws.get_trade())
        self.assertIsNone(ws.snapshot()["trade"])

    def test_start_trade_request_sets_requested_phase(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        trade = ws.get_trade()
        self.assertEqual(trade["phase"], "requested")
        self.assertEqual(trade["partner_guid"], 0x999)
        self.assertTrue(trade["initiated_by_me"])

    def test_incoming_begin_trade_opens_request_and_fires_event(self):
        ws = per.WorldState()
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_BEGIN_TRADE,
                                         "status_name": "begin_trade", "trader_guid": 0x555})
        self.assertEqual(result, ("trade_requested", {"by": 0x555}))
        trade_state = ws.get_trade()
        self.assertEqual(trade_state["phase"], "requested")
        self.assertEqual(trade_state["partner_guid"], 0x555)
        self.assertFalse(trade_state["initiated_by_me"])

    def test_status_with_no_active_trade_is_ignored(self):
        ws = per.WorldState()
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_BACK_TO_TRADE,
                                         "status_name": "back_to_trade"})
        self.assertIsNone(result)
        self.assertIsNone(ws.get_trade())

    def test_open_window_advances_phase(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_OPEN_WINDOW,
                                         "status_name": "open_window"})
        self.assertIsNone(result)
        self.assertEqual(ws.get_trade()["phase"], "open")

    def test_trade_accept_sets_their_accepted(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        ws.apply_trade_status({"status": trade.TRADE_STATUS_TRADE_ACCEPT, "status_name": "trade_accept"})
        self.assertTrue(ws.get_trade()["their_accepted"])
        self.assertFalse(ws.get_trade()["my_accepted"])

    def test_back_to_trade_resets_both_accepted_flags(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        ws.set_my_trade_accepted()
        ws.apply_trade_status({"status": trade.TRADE_STATUS_TRADE_ACCEPT, "status_name": "trade_accept"})
        self.assertTrue(ws.get_trade()["my_accepted"])
        self.assertTrue(ws.get_trade()["their_accepted"])
        ws.apply_trade_status({"status": trade.TRADE_STATUS_BACK_TO_TRADE, "status_name": "back_to_trade"})
        self.assertFalse(ws.get_trade()["my_accepted"])
        self.assertFalse(ws.get_trade()["their_accepted"])

    def test_not_on_taplist_rejects_without_closing_trade(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_NOT_ON_TAPLIST,
                                         "status_name": "not_on_taplist", "slot": 2})
        self.assertEqual(result, ("trade_offer_rejected", {"slot": 2, "reason": "not_on_taplist"}))
        self.assertIsNotNone(ws.get_trade())  # window stays open

    def test_trade_complete_fires_summary_and_clears_state(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        ws.set_my_trade_gold(500)
        ws.set_my_trade_item(0, {"entry": 6948, "name": "Buckler"})
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_TRADE_COMPLETE,
                                         "status_name": "trade_complete"})
        kind, fields = result
        self.assertEqual(kind, "trade_completed")
        self.assertEqual(fields["summary"]["my_gold"], 500)
        self.assertEqual(fields["summary"]["my_items"], [{"entry": 6948, "name": "Buckler"}])
        self.assertIsNone(ws.get_trade())

    def test_cancel_fires_trade_cancelled_and_clears_state(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_TRADE_CANCELED,
                                         "status_name": "trade_canceled"})
        self.assertEqual(result[0], "trade_cancelled")
        self.assertEqual(result[1]["reason"], "trade_canceled")
        self.assertIsNone(ws.get_trade())

    def test_close_window_cancel_uses_result_name_as_reason(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        result = ws.apply_trade_status({"status": trade.TRADE_STATUS_CLOSE_WINDOW,
                                         "status_name": "close_window", "result": 29,
                                         "result_name": "not_enough_money"})
        self.assertEqual(result, ("trade_cancelled",
                                   {"reason": "not_enough_money",
                                    "summary": {"partner_guid": 0x999, "my_gold": 0, "their_gold": 0,
                                                "my_items": [], "their_items": []}}))
        self.assertIsNone(ws.get_trade())

    def test_extended_updates_their_offer_and_resolves_cached_item_name(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        ws.items.items[6948] = {"entry": 6948, "name": "Weather Beaten Buckler", "found": True}
        changed = ws.apply_trade_status_extended({
            "is_trader_data": True, "money": 250,
            "items": {0: {"slot": 0, "entry": 6948, "count": 1}},
        })
        self.assertEqual(changed["gold"], 250)
        self.assertEqual(changed["items"][0]["name"], "Weather Beaten Buckler")
        trade_state = ws.get_trade()
        self.assertEqual(trade_state["their_gold"], 250)
        self.assertEqual(trade_state["their_items"][0]["name"], "Weather Beaten Buckler")

    def test_extended_queues_item_query_for_unresolved_entry(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        ws.apply_trade_status_extended({
            "is_trader_data": True, "money": 0,
            "items": {0: {"slot": 0, "entry": 12345, "count": 1}},
        })
        self.assertIn(12345, ws.items.drain())

    def test_extended_own_data_flag_is_ignored(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        changed = ws.apply_trade_status_extended({"is_trader_data": False, "money": 999, "items": {}})
        self.assertIsNone(changed)
        self.assertEqual(ws.get_trade()["their_gold"], 0)

    def test_extended_with_no_active_trade_is_ignored(self):
        ws = per.WorldState()
        changed = ws.apply_trade_status_extended({"is_trader_data": True, "money": 999, "items": {}})
        self.assertIsNone(changed)

    def test_my_trade_item_mutators(self):
        ws = per.WorldState()
        ws.start_trade_request(0x999, initiated_by_me=True)
        ws.set_my_trade_item(1, {"entry": 159, "name": "Refreshing Spring Water"})
        self.assertEqual(ws.get_trade()["my_items"][1]["entry"], 159)
        ws.clear_my_trade_item(1)
        self.assertNotIn(1, ws.get_trade()["my_items"])

    def test_mutators_are_no_ops_without_an_active_trade(self):
        ws = per.WorldState()
        ws.set_my_trade_item(0, {"entry": 1})  # no trade pending — must not raise
        ws.set_my_trade_gold(100)
        ws.set_my_trade_accepted()
        self.assertIsNone(ws.get_trade())


if __name__ == '__main__':
    unittest.main()
