"""Tests for WorldState's NPC window and mailbox state (perception/windows.py)."""

import unittest

from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo
from agent.tests.test_perception import create_block


class NpcUiStateTest(unittest.TestCase):
    """UM-40: gossip/vendor/trainer windows land in WorldState.ui_state and
    surface through snapshot()'s 'window' key."""

    def test_snapshot_window_defaults_to_none(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, x=0, y=0, z=0))
        self.assertIsNone(ws.snapshot()["window"])

    def test_gossip_message_opens_window_and_queues_text(self):
        ws = per.WorldState()
        data = {"npc_guid": 5, "menu_id": 1, "text_id": 999, "options": [], "quests": []}
        ws.apply_gossip_message(data)
        window = ws.get_ui_state()
        self.assertEqual(window["kind"], "gossip")
        self.assertEqual(window["npc_guid"], 5)
        self.assertNotIn("body_text", window)  # not cached yet
        self.assertEqual(ws.npc_texts.drain(), [(999, 5)])

    def test_npc_text_update_backfills_open_gossip_window(self):
        ws = per.WorldState()
        ws.apply_gossip_message({"npc_guid": 5, "menu_id": 1, "text_id": 999,
                                  "options": [], "quests": []})
        ws.apply_npc_text_update({"text_id": 999, "found": True,
                                   "options": [{"text0": "Welcome!", "text1": "", "probability": 1.0,
                                                "language": 0, "emotes": []}]})
        self.assertEqual(ws.get_ui_state()["body_text"], "Welcome!")

    def test_gossip_complete_closes_window(self):
        ws = per.WorldState()
        ws.apply_gossip_message({"npc_guid": 5, "menu_id": 1, "text_id": 999,
                                  "options": [], "quests": []})
        ws.apply_gossip_complete()
        self.assertIsNone(ws.get_ui_state())

    def test_list_inventory_opens_vendor_window(self):
        ws = per.WorldState()
        ws.apply_list_inventory({"vendor_guid": 5, "items": [], "reason": None})
        self.assertEqual(ws.get_ui_state()["kind"], "vendor")
        self.assertEqual(ws.handles.resolve(ws.snapshot()["window"]["vendor_guid"]), 5)

    def test_trainer_list_opens_trainer_window(self):
        ws = per.WorldState()
        ws.apply_trainer_list({"trainer_guid": 5, "trainer_type": 0, "spells": [], "greeting": ""})
        self.assertEqual(ws.get_ui_state()["kind"], "trainer")

    def test_close_window_clears_state(self):
        ws = per.WorldState()
        ws.apply_list_inventory({"vendor_guid": 5, "items": [], "reason": None})
        ws.close_window()
        self.assertIsNone(ws.get_ui_state())


class MailboxStateTest(unittest.TestCase):
    """UM-60: is_mailbox() detection, world.mailbox's open/fill flow, and
    has_new_mail."""

    def test_is_mailbox_from_gameobject_type(self):
        ws = per.WorldState()
        ws.update_object(create_block(5, object_type=uo.TYPEID_GAMEOBJECT, fields={0x03: 187640}))
        ws.apply_gameobject_query_response({"entry": 187640, "found": True, "name": "Mailbox",
                                             "type": per.GAMEOBJECT_TYPE_MAILBOX})
        self.assertTrue(ws.get_object(5).is_mailbox())

    def test_is_mailbox_resolved_from_a_pre_warmed_name_cache(self):
        # Regression test (found live testing, 2026-09-17): NameCache
        # persists gameobject templates to disk across runs. A gameobject
        # whose entry is already cached (this test simulates that by
        # pre-populating names.gameobjects before the object is even
        # perceived) must still get gameobject_type backfilled — a second
        # live agent process, on its very first perception of the same
        # mailbox, saw is_mailbox() stuck at False forever because only
        # .name was being backfilled from the cached branch.
        ws = per.WorldState()
        ws.names.gameobjects[182363] = {"entry": 182363, "found": True, "name": "Mailbox",
                                          "type": per.GAMEOBJECT_TYPE_MAILBOX}
        ws.update_object(create_block(5, object_type=uo.TYPEID_GAMEOBJECT, fields={0x03: 182363}))
        obj = ws.get_object(5)
        self.assertEqual(obj.name, "Mailbox")
        self.assertTrue(obj.is_mailbox())

    def test_non_mailbox_gameobject_is_not_a_mailbox(self):
        ws = per.WorldState()
        ws.update_object(create_block(5, object_type=uo.TYPEID_GAMEOBJECT, fields={0x03: 1}))
        ws.apply_gameobject_query_response({"entry": 1, "found": True, "name": "Chair", "type": 6})
        self.assertFalse(ws.get_object(5).is_mailbox())

    def test_unresolved_gameobject_type_is_not_a_mailbox_yet(self):
        ws = per.WorldState()
        ws.update_object(create_block(5, object_type=uo.TYPEID_GAMEOBJECT, fields={0x03: 187640}))
        self.assertFalse(ws.get_object(5).is_mailbox())

    def test_is_mailbox_from_npc_flag(self):
        ws = per.WorldState()
        ws.update_object(create_block(5, object_type=uo.TYPEID_UNIT,
                                       fields={uf.UNIT_NPC_FLAGS: per.UNIT_NPC_FLAG_MAILBOX}))
        self.assertTrue(ws.get_object(5).is_mailbox())

    def test_open_mailbox_request_sets_pending_state(self):
        ws = per.WorldState()
        ws.open_mailbox_request(0x1234)
        mailbox = ws.get_mailbox()
        self.assertEqual(mailbox["mailbox_guid"], 0x1234)
        self.assertIsNone(mailbox["mails"])

    def test_mail_list_result_fills_pending_request_and_resolves_item_name(self):
        ws = per.WorldState()
        ws.items.items[20812] = {"entry": 20812, "name": "Tattered Pelt", "found": True}
        ws.open_mailbox_request(0x1234)
        ws.apply_mail_list_result({"total_records": 1, "mails": [{
            "mail_id": 1, "sender_type": 0, "sender_guid": 3, "alt_sender_id": None,
            "cod": 0, "package_id": 0, "stationery_id": 41, "money": 100, "flags": 0,
            "is_read": False, "days_left": 29.0, "mail_template_id": 0,
            "subject": "Hi", "body": "", "attachments": [{"position": 0, "attach_id": 22,
                                                            "entry": 20812, "count": 1}],
        }]})
        mailbox = ws.get_mailbox()
        self.assertEqual(mailbox["mailbox_guid"], 0x1234)  # kept from the request
        self.assertEqual(mailbox["total_records"], 1)
        self.assertEqual(mailbox["mails"][0]["attachments"][0]["name"], "Tattered Pelt")

    def test_mail_list_result_with_no_pending_request_is_ignored(self):
        ws = per.WorldState()
        ws.apply_mail_list_result({"total_records": 0, "mails": []})
        self.assertIsNone(ws.get_mailbox())

    def test_mail_list_result_queues_item_query_for_unresolved_entry(self):
        ws = per.WorldState()
        ws.open_mailbox_request(0x1234)
        ws.apply_mail_list_result({"total_records": 1, "mails": [{
            "mail_id": 1, "sender_type": 0, "sender_guid": 3, "alt_sender_id": None,
            "cod": 0, "package_id": 0, "stationery_id": 41, "money": 0, "flags": 0,
            "is_read": False, "days_left": 29.0, "mail_template_id": 0,
            "subject": "Hi", "body": "", "attachments": [{"position": 0, "attach_id": 22,
                                                            "entry": 99999, "count": 1}],
        }]})
        self.assertIn(99999, ws.items.drain())

    def test_received_mail_sets_flag(self):
        ws = per.WorldState()
        self.assertFalse(ws.snapshot()["has_new_mail"])
        ws.apply_received_mail({"delay": 0.0})
        self.assertTrue(ws.snapshot()["has_new_mail"])

    def test_opening_mailbox_clears_has_new_mail(self):
        ws = per.WorldState()
        ws.apply_received_mail({"delay": 0.0})
        ws.open_mailbox_request(0x1234)
        ws.apply_mail_list_result({"total_records": 0, "mails": []})
        self.assertFalse(ws.snapshot()["has_new_mail"])


if __name__ == '__main__':
    unittest.main()
