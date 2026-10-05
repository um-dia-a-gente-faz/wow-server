"""Tests for agent.actions.trade (split out of test_actions.py, issue #248).
"""

import threading
import time
import unittest

from agent import actions as ac
from agent import perception as per
from agent import trade as tr
from agent import update_fields as uf
from agent import update_object as uo
from agent.tests.actions_helpers import fake_session, object_at, append_event_after, npc_object, self_player_with_slots


class OpenTradeActionTest(unittest.TestCase):
    def test_check_fails_for_unperceived_guid(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.OpenTradeAction().check(sess, world, guid=5))

    def test_check_fails_for_non_player_target(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 2.0, 0.0, 0.0))
        err = ac.OpenTradeAction().check(sess, world, guid=5)
        self.assertIsNotNone(err)
        self.assertIn("not a player", err)

    def test_check_fails_out_of_range(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 50.0, 0.0, 0.0, object_type="player"))
        err = ac.OpenTradeAction().check(sess, world, guid=5)
        self.assertIsNotNone(err)
        self.assertIn("move_towards", err)

    def test_check_fails_when_trade_already_pending(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 2.0, 0.0, 0.0, object_type="player"))
        world.start_trade_request(5, initiated_by_me=True)
        err = ac.OpenTradeAction().check(sess, world, guid=5)
        self.assertIsNotNone(err)
        self.assertIn("already pending", err)

    def test_execute_starts_request_optimistically_and_sends_packet(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 2.0, 0.0, 0.0, object_type="player"))
        result = ac.OpenTradeAction().execute(sess, world, guid=5)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (tr.CMSG_INITIATE_TRADE, tr.build_initiate_trade(5)))
        self.assertEqual(world.get_trade()["phase"], "requested")

    def test_execute_fails_on_rejection_event(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 2.0, 0.0, 0.0, object_type="player"))
        append_event_after(sess, 0.02, {"kind": "trade_cancelled", "reason": "target_to_far"})
        result = ac.OpenTradeAction().execute(sess, world, guid=5)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "target_to_far")


class AcceptTradeRequestActionTest(unittest.TestCase):
    def test_check_fails_without_pending_request(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.AcceptTradeRequestAction().check(fake_session(), world))

    def test_check_fails_when_initiated_by_me(self):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=True)
        err = ac.AcceptTradeRequestAction().check(fake_session(), world)
        self.assertIsNotNone(err)
        self.assertIn("wait for the other side", err)

    def test_execute_sends_begin_trade_and_succeeds_without_reply(self):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=False)
        sess = fake_session()
        action = ac.AcceptTradeRequestAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (tr.CMSG_BEGIN_TRADE, tr.build_begin_trade()))

    def test_execute_fails_on_trade_cancelled_event(self):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=False)
        sess = fake_session()
        action = ac.AcceptTradeRequestAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_cancelled", "reason": "busy"})
        result = action.execute(sess, world)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "busy")


class OfferItemActionTest(unittest.TestCase):
    def _world_with_open_trade_and_item(self, item_flags=0):
        world = per.WorldState()
        world.set_my_guid(0x1)
        item_guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: item_guid}))
        world.update_object(uo.UpdateBlock(
            update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=item_guid, object_type=uo.TYPEID_ITEM,
            movement={"update_flags": 0},
            fields={uf.OBJECT_FIELD_ENTRY: 6948, uf.ITEM_FIELD_STACK_COUNT: 1,
                    uf.ITEM_FIELD_FLAGS: item_flags},
        ))
        world.start_trade_request(5, initiated_by_me=True)
        world.apply_trade_status({"status": tr.TRADE_STATUS_OPEN_WINDOW, "status_name": "open_window"})
        return world, item_guid

    def test_check_fails_without_open_trade(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.OfferItemAction().check(fake_session(), world, bag=255, slot=23))

    def test_check_fails_for_unknown_item(self):
        world, _ = self._world_with_open_trade_and_item()
        err = ac.OfferItemAction().check(fake_session(), world, bag=255, slot=5)
        self.assertIsNotNone(err)
        self.assertIn("no known item", err)

    def test_check_fails_for_soulbound_item(self):
        world, _ = self._world_with_open_trade_and_item(item_flags=tr.ITEM_FIELD_FLAG_SOULBOUND)
        err = ac.OfferItemAction().check(fake_session(), world, bag=255, slot=23)
        self.assertIsNotNone(err)
        self.assertIn("soulbound", err)

    def test_check_fails_for_out_of_range_trade_slot(self):
        world, _ = self._world_with_open_trade_and_item()
        err = ac.OfferItemAction().check(fake_session(), world, bag=255, slot=23, trade_slot=6)
        self.assertIsNotNone(err)

    def test_execute_auto_picks_first_free_slot_and_succeeds(self):
        world, item_guid = self._world_with_open_trade_and_item()
        sess = fake_session()
        action = ac.OfferItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_status", "status_name": "back_to_trade"})
        result = action.execute(sess, world, bag=255, slot=23)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (tr.CMSG_SET_TRADE_ITEM, tr.build_set_trade_item(0, 255, 23)))
        self.assertEqual(world.get_trade()["my_items"][0]["entry"], 6948)

    def test_execute_skips_already_offered_slots(self):
        world, _ = self._world_with_open_trade_and_item()
        world.set_my_trade_item(0, {"entry": 1})
        sess = fake_session()
        action = ac.OfferItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_status", "status_name": "back_to_trade"})
        result = action.execute(sess, world, bag=255, slot=23)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail["trade_slot"], 1)

    def test_execute_fails_on_not_on_taplist(self):
        world, _ = self._world_with_open_trade_and_item()
        sess = fake_session()
        action = ac.OfferItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_status", "status_name": "not_on_taplist"})
        result = action.execute(sess, world, bag=255, slot=23)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_on_taplist")
        self.assertNotIn(0, world.get_trade()["my_items"])

    def test_execute_times_out_as_failure(self):
        world, _ = self._world_with_open_trade_and_item()
        sess = fake_session()
        action = ac.OfferItemAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, bag=255, slot=23)
        self.assertFalse(result.ok)

    def test_execute_reoffering_same_item_in_same_slot_skips_the_round_trip(self):
        # Regression test (found live testing, 2026-09-17): TradeData::
        # SetItem early-returns with NO reply at all when this exact item
        # guid is already in this exact trade slot, so waiting for
        # back_to_trade would time out even on a legitimate no-op.
        world, item_guid = self._world_with_open_trade_and_item()
        world.set_my_trade_item(0, {"guid": item_guid, "entry": 6948})
        sess = fake_session()
        action = ac.OfferItemAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, bag=255, slot=23, trade_slot=0)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent, [])  # no packet sent at all

    def test_execute_reports_failure_not_typeerror_if_trade_cancelled_after_check(self):
        # Regression test found in review (same TOCTOU category as UM-60's
        # mailbox actions): the partner could cancel the trade via a
        # concurrent update on the recv thread between check() and
        # execute(). A bare trade["my_items"] on a None trade would raise
        # TypeError — think.py's `except TypeError` does catch that, but
        # mislabels it as "bad params" even though the LLM's params were fine.
        world, _ = self._world_with_open_trade_and_item()
        self.assertIsNone(ac.OfferItemAction().check(fake_session(), world, bag=255, slot=23))
        world.clear_trade()  # simulate the partner cancelling in between
        result = ac.OfferItemAction().execute(fake_session(), world, bag=255, slot=23)
        self.assertFalse(result.ok)
        self.assertIn("no trade window is open", result.error)

    def test_execute_reports_failure_not_typeerror_if_item_vanishes_after_check(self):
        world, item_guid = self._world_with_open_trade_and_item()
        self.assertIsNone(ac.OfferItemAction().check(fake_session(), world, bag=255, slot=23))
        world.remove_guids([item_guid])  # simulate the item being moved/consumed in between
        result = ac.OfferItemAction().execute(fake_session(), world, bag=255, slot=23)
        self.assertFalse(result.ok)
        self.assertIn("no longer there", result.error)


class OfferGoldActionTest(unittest.TestCase):
    def _world_with_open_trade(self):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=True)
        world.apply_trade_status({"status": tr.TRADE_STATUS_OPEN_WINDOW, "status_name": "open_window"})
        return world

    def test_check_fails_without_open_trade(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.OfferGoldAction().check(fake_session(), world, amount=100))

    def test_check_fails_for_negative_amount(self):
        world = self._world_with_open_trade()
        self.assertIsNotNone(ac.OfferGoldAction().check(fake_session(), world, amount=-1))

    def test_check_fails_for_insufficient_gold(self):
        world = self._world_with_open_trade()
        sess = fake_session()
        sess.coinage = 50
        err = ac.OfferGoldAction().check(sess, world, amount=100)
        self.assertIsNotNone(err)
        self.assertIn("not enough gold", err)

    def test_check_passes_with_enough_gold(self):
        world = self._world_with_open_trade()
        sess = fake_session()
        sess.coinage = 500
        self.assertIsNone(ac.OfferGoldAction().check(sess, world, amount=100))

    def test_execute_succeeds_on_back_to_trade(self):
        world = self._world_with_open_trade()
        sess = fake_session()
        sess.coinage = 500
        action = ac.OfferGoldAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_status", "status_name": "back_to_trade"})
        result = action.execute(sess, world, amount=100)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (tr.CMSG_SET_TRADE_GOLD, tr.build_set_trade_gold(100)))
        self.assertEqual(world.get_trade()["my_gold"], 100)

    def test_execute_fails_on_close_window(self):
        world = self._world_with_open_trade()
        sess = fake_session()
        action = ac.OfferGoldAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_status", "status_name": "close_window",
                                         "result_name": "not_enough_money"})
        result = action.execute(sess, world, amount=100)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_enough_money")
        self.assertEqual(world.get_trade()["my_gold"], 0)

    def test_execute_offering_the_same_amount_again_skips_the_round_trip(self):
        # Regression test (found live testing, 2026-09-17): TradeData::
        # SetMoney early-returns with NO reply at all when `amount` already
        # equals what's offered — my_gold starts at 0, so offer_gold(0)
        # right after opening a trade always timed out even on success.
        world = self._world_with_open_trade()
        sess = fake_session()
        action = ac.OfferGoldAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, amount=0)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent, [])  # no packet sent at all

    def test_execute_reports_failure_not_typeerror_if_trade_cancelled_after_check(self):
        # Same TOCTOU regression as OfferItemAction — found in review.
        world = self._world_with_open_trade()
        sess = fake_session()
        sess.coinage = 500
        self.assertIsNone(ac.OfferGoldAction().check(sess, world, amount=100))
        world.clear_trade()
        result = ac.OfferGoldAction().execute(sess, world, amount=100)
        self.assertFalse(result.ok)
        self.assertIn("no trade window is open", result.error)


class AcceptTradeActionTest(unittest.TestCase):
    def _world_with_their_offer(self, gold=0, entries=()):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=True)
        world.apply_trade_status({"status": tr.TRADE_STATUS_OPEN_WINDOW, "status_name": "open_window"})
        items = {i: {"slot": i, "entry": e} for i, e in enumerate(entries)}
        world.apply_trade_status_extended({"is_trader_data": True, "money": gold, "items": items})
        return world

    def test_check_fails_without_open_trade(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.AcceptTradeAction().check(fake_session(), world))

    def test_check_fails_when_gold_mismatch(self):
        world = self._world_with_their_offer(gold=100)
        err = ac.AcceptTradeAction().check(fake_session(), world, expected_their_gold=0)
        self.assertIsNotNone(err)
        self.assertIn("offer has changed", err)

    def test_check_fails_when_items_mismatch(self):
        world = self._world_with_their_offer(entries=(6948,))
        err = ac.AcceptTradeAction().check(fake_session(), world, expected_their_item_entries=[])
        self.assertIsNotNone(err)

    def test_check_passes_when_offer_matches(self):
        world = self._world_with_their_offer(gold=100, entries=(6948, 159))
        err = ac.AcceptTradeAction().check(fake_session(), world, expected_their_gold=100,
                                            expected_their_item_entries=[159, 6948])
        self.assertIsNone(err)  # order-independent

    def test_execute_returns_ok_waiting_when_no_reply(self):
        world = self._world_with_their_offer()
        sess = fake_session()
        action = ac.AcceptTradeAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (tr.CMSG_ACCEPT_TRADE, tr.build_accept_trade()))
        self.assertTrue(world.get_trade()["my_accepted"])

    def test_execute_returns_ok_on_trade_completed(self):
        world = self._world_with_their_offer()
        sess = fake_session()
        action = ac.AcceptTradeAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_completed", "summary": {}})
        result = action.execute(sess, world)
        self.assertTrue(result.ok)

    def test_execute_fails_on_trade_cancelled(self):
        world = self._world_with_their_offer()
        sess = fake_session()
        action = ac.AcceptTradeAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_cancelled", "reason": "trade_canceled"})
        result = action.execute(sess, world)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "trade_canceled")


class CancelTradeActionTest(unittest.TestCase):
    def test_check_fails_without_active_trade(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.CancelTradeAction().check(fake_session(), world))

    def test_execute_sends_cancel_and_reports_ok(self):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=True)
        sess = fake_session()
        action = ac.CancelTradeAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (tr.CMSG_CANCEL_TRADE, tr.build_cancel_trade()))

    def test_execute_reports_ok_and_clears_on_cancel_confirmation(self):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=True)
        sess = fake_session()
        action = ac.CancelTradeAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01

        def cancel_after(delay):
            time.sleep(delay)
            world.apply_trade_status({"status": tr.TRADE_STATUS_TRADE_CANCELED,
                                       "status_name": "trade_canceled"})
            sess.events.append({"kind": "trade_cancelled", "reason": "trade_canceled",
                                 "t": time.monotonic()})
        t = threading.Thread(target=cancel_after, args=(0.02,))
        t.start()
        result = action.execute(sess, world)
        t.join()
        self.assertTrue(result.ok)
        self.assertIsNone(world.get_trade())
