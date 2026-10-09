"""Tests for agent.actions.mail (split out of test_actions.py, issue #248).
"""

import threading
import time
import unittest

from agent import actions as ac
from agent import mail as mailmod
from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo
from agent.tests.actions_helpers import fake_session, append_event_after, item_object, self_player_with_slots


def mailbox_object(guid, x, y, z, entry=187640):
    """A gameobject CREATE block for a mailbox — callers still need to feed
    the matching apply_gameobject_query_response(type=GAMEOBJECT_TYPE_MAILBOX)
    for world.get_object(guid).is_mailbox() to return True, same lazy-
    resolution as name (see agent/mail.py's docstring)."""
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=uo.TYPEID_GAMEOBJECT,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": 0.0},
        fields={uf.OBJECT_FIELD_ENTRY: entry},
    )


def mail_list_entry(mail_id=1, sender_guid=3, subject="Subj", body="", money=0, cod=0,
                     attachments: list | None = None) -> dict:
    return {
        "mail_id": mail_id, "sender_type": 0, "sender_guid": sender_guid, "alt_sender_id": None,
        "cod": cod, "package_id": 0, "stationery_id": 41, "money": money, "flags": 0,
        "is_read": False, "days_left": 29.0, "mail_template_id": 0,
        "subject": subject, "body": body, "attachments": attachments or [],
    }


class OpenMailboxActionTest(unittest.TestCase):
    def _world_with_mailbox(self, dist=2.0):
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(mailbox_object(5, dist, 0.0, 0.0))
        world.apply_gameobject_query_response({"entry": 187640, "found": True, "name": "Mailbox",
                                                 "type": per.GAMEOBJECT_TYPE_MAILBOX})
        return world

    def test_check_fails_without_nearby_mailbox(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.OpenMailboxAction().check(sess, world))

    def test_check_fails_when_mailbox_out_of_range(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = self._world_with_mailbox(dist=50.0)
        self.assertIsNotNone(ac.OpenMailboxAction().check(sess, world))

    def test_execute_sends_get_mail_list_and_starts_pending_request(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = self._world_with_mailbox()
        action = ac.OpenMailboxAction()
        append_event_after_sets_mailbox = threading.Thread(
            target=lambda: (time.sleep(0.02), world.apply_mail_list_result({"total_records": 0, "mails": []})))
        append_event_after_sets_mailbox.start()
        result = action.execute(sess, world)
        append_event_after_sets_mailbox.join()
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (mailmod.CMSG_GET_MAIL_LIST, mailmod.build_get_mail_list(5)))
        self.assertEqual(world.get_mailbox()["mailbox_guid"], 5)

    def test_execute_times_out_as_failure(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = self._world_with_mailbox()
        action = ac.OpenMailboxAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world)
        self.assertFalse(result.ok)

    def test_execute_reports_failure_not_attributeerror_if_mailbox_vanishes_after_check(self):
        # Regression test found in review: check() passing doesn't guarantee
        # execute()'s own _find_nearby_mailbox() call still finds it — the
        # recv thread could process an OUT_OF_RANGE_OBJECTS in between. Must
        # degrade to ActionResult(ok=False), not raise AttributeError
        # (which think.py's `except TypeError` wouldn't catch).
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = self._world_with_mailbox()
        self.assertIsNone(ac.OpenMailboxAction().check(sess, world))
        world.remove_guids([5])  # simulate it leaving perception between check() and execute()
        result = ac.OpenMailboxAction().execute(sess, world)
        self.assertFalse(result.ok)
        self.assertIn("no longer in range", result.error)


class SendMailActionTest(unittest.TestCase):
    def _world_with_mailbox_and_item(self):
        world = per.WorldState()
        world.set_my_map(530)
        world.set_my_guid(0x1)
        item_guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: item_guid}))
        world.update_object(item_object(item_guid, 20812))
        world.update_object(mailbox_object(5, 2.0, 0.0, 0.0))
        world.apply_gameobject_query_response({"entry": 187640, "found": True, "name": "Mailbox",
                                                 "type": per.GAMEOBJECT_TYPE_MAILBOX})
        return world, item_guid

    def test_check_fails_without_nearby_mailbox(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.coinage = 1000
        world = per.WorldState()
        err = ac.SendMailAction().check(sess, world, to="Rubens", subject="Hi", body="Body")
        self.assertIsNotNone(err)

    def test_check_fails_for_insufficient_gold_including_postage(self):
        world, _ = self._world_with_mailbox_and_item()
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.coinage = 20  # less than the 30-copper postage alone
        err = ac.SendMailAction().check(sess, world, to="Rubens", subject="Hi", body="Body")
        self.assertIsNotNone(err)
        self.assertIn("not enough gold", err)

    def test_check_fails_for_soulbound_item(self):
        world = per.WorldState()
        world.set_my_map(530)
        world.set_my_guid(0x1)
        item_guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: item_guid}))
        world.update_object(uo.UpdateBlock(
            update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=item_guid, object_type=uo.TYPEID_ITEM,
            movement={"update_flags": 0},
            fields={uf.OBJECT_FIELD_ENTRY: 20812, uf.ITEM_FIELD_FLAGS: mailmod.ITEM_FIELD_FLAG_SOULBOUND},
        ))
        world.update_object(mailbox_object(5, 2.0, 0.0, 0.0))
        world.apply_gameobject_query_response({"entry": 187640, "found": True, "name": "Mailbox",
                                                 "type": per.GAMEOBJECT_TYPE_MAILBOX})
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.coinage = 1000
        err = ac.SendMailAction().check(sess, world, to="Rubens", subject="Hi", body="Body",
                                          bag=255, slot=23)
        self.assertIsNotNone(err)
        self.assertIn("soulbound", err)

    def test_execute_succeeds_and_records_mail_sent(self):
        world, item_guid = self._world_with_mailbox_and_item()
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.coinage = 1000
        append_event_after(sess, 0.02, {"kind": "mail_result", "mail_id": 0,
                                         "command": mailmod.MAIL_SEND, "command_name": "send",
                                         "error_code": mailmod.MAIL_OK, "error_name": "ok"})
        result = ac.SendMailAction().execute(sess, world, to="Rubens", subject="Hi", body="Body",
                                              gold=100, bag=255, slot=23)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (mailmod.CMSG_SEND_MAIL,
                                          mailmod.build_send_mail(5, "Rubens", "Hi", "Body",
                                                                   money=100, item_guid=item_guid)))
        sent_events = [e for e in sess.events if e["kind"] == "mail_sent"]
        self.assertEqual(len(sent_events), 1)
        self.assertEqual(sent_events[0]["to"], "Rubens")

    def test_execute_fails_and_records_mail_error_on_recipient_not_found(self):
        world, _ = self._world_with_mailbox_and_item()
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.coinage = 1000
        append_event_after(sess, 0.02, {"kind": "mail_result", "mail_id": 0,
                                         "command": mailmod.MAIL_SEND, "command_name": "send",
                                         "error_code": 4, "error_name": "recipient_not_found"})
        result = ac.SendMailAction().execute(sess, world, to="Nobody", subject="Hi", body="Body")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "recipient_not_found")
        error_events = [e for e in sess.events if e["kind"] == "mail_error"]
        self.assertEqual(len(error_events), 1)

    def test_execute_reports_failure_not_attributeerror_if_mailbox_vanishes_after_check(self):
        # Same TOCTOU regression as OpenMailboxAction — found in review.
        world, _ = self._world_with_mailbox_and_item()
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.coinage = 1000
        self.assertIsNone(ac.SendMailAction().check(sess, world, to="Rubens", subject="Hi", body="Body"))
        world.remove_guids([5])
        result = ac.SendMailAction().execute(sess, world, to="Rubens", subject="Hi", body="Body")
        self.assertFalse(result.ok)
        self.assertIn("no longer in range", result.error)

    def test_execute_reports_failure_not_typeerror_if_item_vanishes_after_check(self):
        # Same race as the mailbox one above, for the attached item —
        # found in review. A bare tuple-unpack of None would raise
        # TypeError (which think.py does catch, but mislabels as "bad
        # params" even though the LLM's params were fine).
        world, item_guid = self._world_with_mailbox_and_item()
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.coinage = 1000
        self.assertIsNone(ac.SendMailAction().check(sess, world, to="Rubens", subject="Hi", body="Body",
                                                      bag=255, slot=23))
        world.remove_guids([item_guid])  # simulate the item being moved/consumed between check() and execute()
        ac.SendMailAction().execute(sess, world, to="Rubens", subject="Hi", body="Body",
                                    bag=255, slot=23)


class TakeMailActionTest(unittest.TestCase):
    def test_check_fails_without_open_mailbox(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.TakeMailAction().check(fake_session(), world, mail_id=1))

    def test_check_fails_for_unknown_mail_id(self):
        world = per.WorldState()
        world.open_mailbox_request(5)
        world.apply_mail_list_result({"total_records": 0, "mails": []})
        self.assertIsNotNone(ac.TakeMailAction().check(fake_session(), world, mail_id=99))

    def test_execute_reports_failure_not_stopiteration_if_mail_vanishes_after_check(self):
        # Regression test found in review: check() passing doesn't
        # guarantee execute()'s own mail_id lookup still finds it — a
        # concurrent SMSG_MAIL_LIST_RESULT (recv thread) could replace
        # world.mailbox between the two calls. A bare next() would raise
        # StopIteration here, uncaught by think.py's `except TypeError`.
        world = per.WorldState()
        world.open_mailbox_request(5)
        world.apply_mail_list_result({"total_records": 1, "mails": [mail_list_entry(mail_id=1)]})
        self.assertIsNone(ac.TakeMailAction().check(fake_session(), world, mail_id=1))
        world.apply_mail_list_result({"total_records": 0, "mails": []})  # mail_id 1 gone now
        result = ac.TakeMailAction().execute(fake_session(), world, mail_id=1)
        self.assertFalse(result.ok)
        self.assertIn("no longer", result.error)

    def test_check_fails_when_cant_afford_cod(self):
        world = per.WorldState()
        world.open_mailbox_request(5)
        world.apply_mail_list_result({"total_records": 1, "mails": [
            mail_list_entry(mail_id=1, cod=500, attachments=[{"position": 0, "attach_id": 22, "entry": 1}])]})
        sess = fake_session()
        sess.coinage = 100
        err = ac.TakeMailAction().check(sess, world, mail_id=1)
        self.assertIsNotNone(err)
        self.assertIn("COD", err)

    def test_execute_takes_money_and_item(self):
        world = per.WorldState()
        world.open_mailbox_request(5)
        world.apply_mail_list_result({"total_records": 1, "mails": [
            mail_list_entry(mail_id=1, money=100,
                             attachments=[{"position": 0, "attach_id": 22, "entry": 20812}])]})
        sess = fake_session()
        action = ac.TakeMailAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01

        def respond():
            # Mirrors the real server: each reply only arrives after its
            # own request was actually sent — waits for len(sess._sent) to
            # reach 1 (money request sent) before answering it, then for 2
            # (item request sent) before answering that.
            while len(sess._sent) < 1:
                time.sleep(0.005)
            sess.events.append({"kind": "mail_result", "t": time.monotonic(), "mail_id": 1,
                                 "command": mailmod.MAIL_MONEY_TAKEN, "error_code": mailmod.MAIL_OK})
            while len(sess._sent) < 2:
                time.sleep(0.005)
            sess.events.append({"kind": "mail_result", "t": time.monotonic(), "mail_id": 1,
                                 "command": mailmod.MAIL_ITEM_TAKEN, "attach_id": 22,
                                 "error_code": mailmod.MAIL_OK})
        t = threading.Thread(target=respond)
        t.start()
        result = action.execute(sess, world, mail_id=1)
        t.join()
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (mailmod.CMSG_MAIL_TAKE_MONEY, mailmod.build_mail_take_money(5, 1)))
        self.assertEqual(sess._sent[1], (mailmod.CMSG_MAIL_TAKE_ITEM, mailmod.build_mail_take_item(5, 1, 22)))

    def test_execute_fails_when_item_take_fails(self):
        world = per.WorldState()
        world.open_mailbox_request(5)
        world.apply_mail_list_result({"total_records": 1, "mails": [
            mail_list_entry(mail_id=1, attachments=[{"position": 0, "attach_id": 22, "entry": 20812}])]})
        sess = fake_session()
        action = ac.TakeMailAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01

        def respond():
            time.sleep(0.02)
            sess.events.append({"kind": "mail_result", "t": time.monotonic(), "mail_id": 1,
                                 "command": mailmod.MAIL_ITEM_TAKEN, "attach_id": 22,
                                 "error_code": 6, "error_name": "internal_error"})
        t = threading.Thread(target=respond)
        t.start()
        result = action.execute(sess, world, mail_id=1)
        t.join()
        self.assertFalse(result.ok)

    def test_execute_matches_a_failure_reply_with_no_attach_id_field(self):
        # Regression test found in review: SMSG_SEND_MAIL_RESULT only
        # includes `attach_id` when the take-item succeeded (or the item
        # expired) — a failure for another reason (e.g. equip error) omits
        # it entirely, per agent/mail.py::parse_send_mail_result. Matching
        # by time order instead of attach_id means this failure is still
        # found, not mistaken for "no confirmation seen (timed out)".
        world = per.WorldState()
        world.open_mailbox_request(5)
        world.apply_mail_list_result({"total_records": 1, "mails": [
            mail_list_entry(mail_id=1, attachments=[{"position": 0, "attach_id": 22, "entry": 20812}])]})
        sess = fake_session()
        action = ac.TakeMailAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "mail_result", "mail_id": 1,
                                         "command": mailmod.MAIL_ITEM_TAKEN,
                                         "error_code": 1, "error_name": "equip_error"})
        result = action.execute(sess, world, mail_id=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.detail["outcomes"]["items"][0]["error_name"], "equip_error")


class DeleteMailActionTest(unittest.TestCase):
    def test_check_fails_without_open_mailbox(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.DeleteMailAction().check(fake_session(), world, mail_id=1))

    def test_execute_succeeds_on_ok_result(self):
        world = per.WorldState()
        world.open_mailbox_request(5)
        world.apply_mail_list_result({"total_records": 1, "mails": [mail_list_entry(mail_id=1)]})
        sess = fake_session()
        action = ac.DeleteMailAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "mail_result", "mail_id": 1,
                                         "command": mailmod.MAIL_DELETED, "error_code": mailmod.MAIL_OK,
                                         "error_name": "ok"})
        result = action.execute(sess, world, mail_id=1)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (mailmod.CMSG_MAIL_DELETE, mailmod.build_mail_delete(5, 1)))

    def test_execute_fails_on_cod_rejection(self):
        world = per.WorldState()
        world.open_mailbox_request(5)
        world.apply_mail_list_result({"total_records": 1, "mails": [mail_list_entry(mail_id=1, cod=100)]})
        sess = fake_session()
        action = ac.DeleteMailAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "mail_result", "mail_id": 1,
                                         "command": mailmod.MAIL_DELETED, "error_code": 6,
                                         "error_name": "internal_error"})
        result = action.execute(sess, world, mail_id=1)
        self.assertFalse(result.ok)
