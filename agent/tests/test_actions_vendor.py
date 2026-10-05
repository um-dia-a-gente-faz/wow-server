"""Tests for agent.actions.vendor (split out of test_actions.py, issue #248).
"""

import unittest

from agent import actions as ac
from agent import npc
from agent import perception as per
from agent.tests.actions_helpers import fake_session, append_event_after, npc_object, self_player_with_slots


class InteractActionTest(unittest.TestCase):
    def test_check_fails_for_unperceived_guid(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.InteractAction().check(sess, world, guid=5))

    def test_check_out_of_range_suggests_move_towards(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 100.0, 0.0, 0.0, npc_flags=per.UNIT_NPC_FLAG_VENDOR))
        err = ac.InteractAction().check(sess, world, guid=5)
        self.assertIsNotNone(err)
        self.assertIn("move_towards", err)

    def test_execute_sends_gossip_hello_for_gossip_flag(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 2.0, 0.0, 0.0, npc_flags=per.UNIT_NPC_FLAG_GOSSIP))
        result = ac.InteractAction().execute(sess, world, guid=5)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_GOSSIP_HELLO, npc.build_gossip_hello(5)))

    def test_execute_sends_list_inventory_for_vendor_only_flag(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 2.0, 0.0, 0.0, npc_flags=per.UNIT_NPC_FLAG_VENDOR))
        result = ac.InteractAction().execute(sess, world, guid=5)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_LIST_INVENTORY, npc.build_list_inventory(5)))

    def test_execute_sends_trainer_list_for_trainer_only_flag(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 2.0, 0.0, 0.0, npc_flags=per.UNIT_NPC_FLAG_TRAINER))
        result = ac.InteractAction().execute(sess, world, guid=5)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_TRAINER_LIST, npc.build_trainer_list(5)))

    def test_execute_sends_gameobj_use_for_gameobject(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 2.0, 0.0, 0.0, object_type="gameobject"))
        result = ac.InteractAction().execute(sess, world, guid=5)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_GAMEOBJ_USE, npc.build_gameobj_use(5)))

    def test_execute_reports_no_interaction_for_plain_unit(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 2.0, 0.0, 0.0, npc_flags=0))
        result = ac.InteractAction().execute(sess, world, guid=5)
        self.assertFalse(result.ok)


class GossipSelectActionTest(unittest.TestCase):
    def test_check_fails_without_open_window(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.GossipSelectAction().check(fake_session(), world, option_index=0))

    def test_check_fails_for_unknown_option(self):
        world = per.WorldState()
        world.ui_state = {"kind": "gossip", "npc_guid": 5, "menu_id": 1,
                           "options": [{"index": 0}]}
        self.assertIsNotNone(ac.GossipSelectAction().check(fake_session(), world, option_index=9))

    def test_execute_sends_select_option(self):
        world = per.WorldState()
        world.ui_state = {"kind": "gossip", "npc_guid": 5, "menu_id": 1,
                           "options": [{"index": 0}, {"index": 1}]}
        sess = fake_session()
        result = ac.GossipSelectAction().execute(sess, world, option_index=1)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_GOSSIP_SELECT_OPTION,
                                          npc.build_gossip_select_option(5, 1, 1)))


class BuyItemActionTest(unittest.TestCase):
    def _window(self):
        return {"kind": "vendor", "vendor_guid": 5,
                "items": [{"slot": 1, "entry": 6948, "price": 500},
                          {"slot": 2, "entry": 159, "price": 10}]}

    def test_check_fails_without_open_window(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.BuyItemAction().check(fake_session(), world, vendor_guid=5, slot=1))

    def test_check_fails_for_unknown_slot(self):
        world = per.WorldState()
        world.ui_state = self._window()
        self.assertIsNotNone(ac.BuyItemAction().check(fake_session(), world, vendor_guid=5, slot=9))

    def test_execute_by_slot_succeeds_on_buy_item_ack(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.BuyItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "buy_item", "vendor_guid": 5, "slot": 2,
                                         "new_count": 3, "stacks": 3})
        result = action.execute(sess, world, vendor_guid=5, slot=2, count=3)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_BUY_ITEM, npc.build_buy_item(5, 159, 2, 3)))

    def test_execute_by_entry_succeeds_on_buy_item_ack(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.BuyItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "buy_item", "vendor_guid": 5, "slot": 1,
                                         "new_count": -1, "stacks": 1})
        result = action.execute(sess, world, vendor_guid=5, entry=6948, count=1)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_BUY_ITEM, npc.build_buy_item(5, 6948, 1, 1)))

    def test_execute_fails_on_buy_failed(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.BuyItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "buy_failed", "vendor_guid": 5, "item_entry": 159,
                                         "reason": 2, "reason_name": "not_enough_money"})
        result = action.execute(sess, world, vendor_guid=5, slot=2, count=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_enough_money")

    def test_execute_no_response_times_out_as_failure(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.BuyItemAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, vendor_guid=5, slot=2, count=1)
        self.assertFalse(result.ok)


class SellItemActionTest(unittest.TestCase):
    def test_check_fails_without_open_window(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.SellItemAction().check(fake_session(), world, vendor_guid=5, bag=255, slot=23))

    def test_check_fails_for_unknown_item(self):
        world = per.WorldState()
        world.ui_state = {"kind": "vendor", "vendor_guid": 5, "items": []}
        self.assertIsNotNone(ac.SellItemAction().check(fake_session(), world, vendor_guid=5, bag=255, slot=23))

    def test_execute_succeeds_when_no_failure_arrives(self):
        world = per.WorldState()
        world.set_my_guid(0x1)
        item_guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: item_guid}))
        world.ui_state = {"kind": "vendor", "vendor_guid": 5, "items": []}
        sess = fake_session()
        action = ac.SellItemAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, vendor_guid=5, bag=255, slot=23, count=1)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_SELL_ITEM, npc.build_sell_item(5, item_guid, 1)))

    def test_execute_fails_on_sell_failed(self):
        world = per.WorldState()
        world.set_my_guid(0x1)
        item_guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: item_guid}))
        world.ui_state = {"kind": "vendor", "vendor_guid": 5, "items": []}
        sess = fake_session()
        action = ac.SellItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "sell_failed", "vendor_guid": 5, "item_guid": item_guid,
                                         "reason": 2, "reason_name": "cant_sell_item"})
        result = action.execute(sess, world, vendor_guid=5, bag=255, slot=23, count=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "cant_sell_item")


class TrainSpellActionTest(unittest.TestCase):
    def _window(self):
        return {"kind": "trainer", "trainer_guid": 5, "spells": [{"spell_id": 587}]}

    def test_check_fails_without_open_window(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.TrainSpellAction().check(fake_session(), world,
                                                          trainer_guid=5, spell_id=587))

    def test_check_fails_for_unknown_spell(self):
        world = per.WorldState()
        world.ui_state = self._window()
        self.assertIsNotNone(ac.TrainSpellAction().check(fake_session(), world,
                                                          trainer_guid=5, spell_id=999))

    def test_execute_succeeds_on_train_succeeded(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.TrainSpellAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "train_succeeded", "trainer_guid": 5, "spell_id": 587})
        result = action.execute(sess, world, trainer_guid=5, spell_id=587)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_TRAINER_BUY_SPELL, npc.build_train_spell(5, 587)))

    def test_execute_fails_on_train_failed(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.TrainSpellAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "train_failed", "trainer_guid": 5, "spell_id": 587,
                                         "reason": 1, "reason_name": "not_enough_money"})
        result = action.execute(sess, world, trainer_guid=5, spell_id=587)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_enough_money")

    def test_execute_no_response_times_out(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.TrainSpellAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, trainer_guid=5, spell_id=587)
        self.assertFalse(result.ok)
