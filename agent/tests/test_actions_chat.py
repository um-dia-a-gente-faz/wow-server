"""Tests for agent.actions.chat (split out of test_actions.py, issue #248).
"""

import struct
import unittest

from agent import actions as ac
from agent.reflexes import follow as _follow_reflex  # noqa: F401 -- registers follow/assist
from agent import perception as per
from agent.tests.actions_helpers import fake_session, object_at, append_event_after


class LanguageSelectionTest(unittest.TestCase):
    def test_horde_race_uses_orcish(self):
        self.assertEqual(ac._racial_language(fake_session(race=10)), ac.LANG_ORCISH)  # Blood Elf
        self.assertEqual(ac._racial_language(fake_session(race=2)), ac.LANG_ORCISH)   # Orc

    def test_alliance_race_uses_common(self):
        self.assertEqual(ac._racial_language(fake_session(race=1)), ac.LANG_COMMON)   # Human
        self.assertEqual(ac._racial_language(fake_session(race=11)), ac.LANG_COMMON)  # Draenei

    def test_unknown_race_defaults_to_orcish(self):
        self.assertEqual(ac._racial_language(fake_session(race=0)), ac.LANG_ORCISH)

    def test_never_sends_universal(self):
        for race in (0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11):
            with self.subTest(race=race):
                self.assertNotEqual(ac._racial_language(fake_session(race=race)), ac.LANG_UNIVERSAL)


class MessageEncodingTest(unittest.TestCase):
    def test_utf8_accents_preserved(self):
        encoded = ac._encode_message("Olá, tudo bem?")
        self.assertEqual(encoded.decode("utf-8"), "Olá, tudo bem?")

    def test_newlines_stripped(self):
        self.assertNotIn(b'\n', ac._encode_message("line one\nline two"))
        self.assertNotIn(b'\r', ac._encode_message("a\r\nb"))

    def test_capped_at_255_bytes(self):
        encoded = ac._encode_message("x" * 500)
        self.assertLessEqual(len(encoded), 255)

    def test_cap_does_not_split_multibyte_char(self):
        # 'á' is 2 bytes in UTF-8; pad so the cut would land mid-character.
        message = "x" * 254 + "á"
        encoded = ac._encode_message(message)
        encoded.decode("utf-8")  # raises UnicodeDecodeError if truncated mid-sequence


class ChatWireFormatTest(unittest.TestCase):
    def _decode_chat_payload(self, payload, expect_target=False):
        slash_cmd, lang = struct.unpack_from('<ii', payload, 0)
        off = 8
        target = None
        if expect_target:
            end = payload.index(b'\x00', off)
            target = payload[off:end].decode('utf-8')
            off = end + 1
        end = payload.index(b'\x00', off)
        text = payload[off:end].decode('utf-8')
        return slash_cmd, lang, target, text

    def test_say(self):
        sess = fake_session()
        ac.say(sess, "hello")
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_MESSAGECHAT)
        slash_cmd, lang, target, text = self._decode_chat_payload(payload)
        self.assertEqual(slash_cmd, ac.CHAT_MSG_SAY)
        self.assertEqual(lang, ac.LANG_ORCISH)
        self.assertIsNone(target)
        self.assertEqual(text, "hello")

    def test_yell(self):
        sess = fake_session()
        ac.yell(sess, "for the horde")
        _, payload = sess._sent[0]
        slash_cmd, _, _, text = self._decode_chat_payload(payload)
        self.assertEqual(slash_cmd, ac.CHAT_MSG_YELL)
        self.assertEqual(text, "for the horde")

    def test_whisper_includes_real_target(self):
        sess = fake_session()
        ac.whisper(sess, "Rubens", "Olá!")
        _, payload = sess._sent[0]
        slash_cmd, _, target, text = self._decode_chat_payload(payload, expect_target=True)
        self.assertEqual(slash_cmd, ac.CHAT_MSG_WHISPER)
        self.assertEqual(target, "Rubens")
        self.assertEqual(text, "Olá!")

    def test_emote(self):
        sess = fake_session()
        ac.emote(sess, "waves")
        _, payload = sess._sent[0]
        slash_cmd, _, _, text = self._decode_chat_payload(payload)
        self.assertEqual(slash_cmd, ac.CHAT_MSG_EMOTE)
        self.assertEqual(text, "waves")

    def test_text_emote(self):
        sess = fake_session()
        ac.text_emote(sess, 66, target_guid=5)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_TEXT_EMOTE)
        emote_id, sound, guid = struct.unpack('<iiQ', payload)
        self.assertEqual(emote_id, 66)
        self.assertEqual(sound, 0)
        self.assertEqual(guid, 5)


class GroupActionsTest(unittest.TestCase):
    def test_invite_to_group(self):
        sess = fake_session()
        ac.invite_to_group(sess, "Rubens")
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_GROUP_INVITE)
        end = payload.index(b'\x00')
        self.assertEqual(payload[:end].decode(), "Rubens")
        roles = struct.unpack_from('<I', payload, end + 1)[0]
        self.assertEqual(roles, 0)

    def test_accept_group_clears_pending_invite(self):
        sess = fake_session()
        self.assertIsNotNone(sess.pending_invite)
        ac.accept_group(sess)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_GROUP_ACCEPT)
        self.assertIsNone(sess.pending_invite)

    def test_leave_group_sends_no_payload(self):
        sess = fake_session()
        ac.leave_group(sess)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_GROUP_DISBAND)
        self.assertEqual(payload, b'')


class SendChatMessageBackCompatTest(unittest.TestCase):
    def test_whisper_without_target_raises(self):
        sess = fake_session()
        with self.assertRaises(ValueError):
            ac.send_chat_message(sess, "hi", channel="whisper")

    def test_channel_dispatch(self):
        sess = fake_session()
        ac.send_chat_message(sess, "hi", channel="yell")
        _, payload = sess._sent[0]
        slash_cmd = struct.unpack_from('<i', payload, 0)[0]
        self.assertEqual(slash_cmd, ac.CHAT_MSG_YELL)


class CloseWindowActionTest(unittest.TestCase):
    def test_closes_open_window(self):
        world = per.WorldState()
        world.ui_state = {"kind": "gossip"}
        result = ac.CloseWindowAction().execute(fake_session(), world)
        self.assertTrue(result.ok)
        self.assertTrue(result.detail["was_open"])
        self.assertIsNone(world.get_ui_state())

    def test_noop_when_already_closed(self):
        world = per.WorldState()
        result = ac.CloseWindowAction().execute(fake_session(), world)
        self.assertTrue(result.ok)
        self.assertFalse(result.detail["was_open"])


class SocialActionsRegistrationTest(unittest.TestCase):
    # UM-98: chat is deferred (docs/adr/0001-jev-in-the-think-loop.md), so
    # the brain is offered no text-generating chat action; party mechanics
    # need no free text and stay.
    CHAT_ACTIONS = ("say", "yell", "whisper", "emote", "channel_say")

    def test_registry_keeps_party_actions(self):
        for name in ("invite_to_group", "accept_group", "follow", "assist", "stop_following"):
            self.assertIn(name, ac.REGISTRY)

    def test_chat_actions_not_registered(self):
        for name in self.CHAT_ACTIONS:
            self.assertNotIn(name, ac.REGISTRY)

    def test_catalog_offers_no_chat_action(self):
        names = {tool["name"] for tool in ac.catalog()}
        self.assertFalse(names & set(self.CHAT_ACTIONS))
        self.assertTrue({"invite_to_group", "accept_group", "follow", "assist"} <= names)


class SayActionTest(unittest.TestCase):
    def test_execute_sends_say(self):
        sess = fake_session()
        result = ac.SayAction().execute(sess, None, message="hello")
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_MESSAGECHAT)
        slash_cmd = struct.unpack_from('<i', payload, 0)[0]
        self.assertEqual(slash_cmd, ac.CHAT_MSG_SAY)


class YellActionTest(unittest.TestCase):
    def test_execute_sends_yell(self):
        sess = fake_session()
        result = ac.YellAction().execute(sess, None, message="for the horde")
        self.assertTrue(result.ok)
        _, payload = sess._sent[0]
        slash_cmd = struct.unpack_from('<i', payload, 0)[0]
        self.assertEqual(slash_cmd, ac.CHAT_MSG_YELL)


class EmoteActionTest(unittest.TestCase):
    def test_execute_sends_emote(self):
        sess = fake_session()
        result = ac.EmoteAction().execute(sess, None, text="waves")
        self.assertTrue(result.ok)
        _, payload = sess._sent[0]
        slash_cmd = struct.unpack_from('<i', payload, 0)[0]
        self.assertEqual(slash_cmd, ac.CHAT_MSG_EMOTE)


class InviteToGroupActionTest(unittest.TestCase):
    def test_execute_succeeds_when_no_failure_arrives(self):
        sess = fake_session()
        action = ac.InviteToGroupAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, None, name="Rubens")
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_GROUP_INVITE)

    def test_execute_fails_on_group_invite_failed(self):
        sess = fake_session()
        action = ac.InviteToGroupAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "group_invite_failed", "target_name": "Rubens",
                                         "result": 5, "result_name": "already_in_group"})
        result = action.execute(sess, None, name="Rubens")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "already_in_group")


class WhisperActionTest(unittest.TestCase):
    def test_check_fails_for_unknown_name(self):
        world = per.WorldState()
        err = ac.WhisperAction().check(fake_session(), world, target_name="Nobody", message="hi")
        self.assertIsNotNone(err)

    def test_check_passes_for_perceived_player_name(self):
        world = per.WorldState()
        world.update_object(object_at(5, 1.0, 2.0, 3.0, object_type="player"))
        world.objects[5].name = "Rubens"
        err = ac.WhisperAction().check(fake_session(), world, target_name="Rubens", message="hi")
        self.assertIsNone(err)

    def test_check_passes_for_recent_chat_sender(self):
        world = per.WorldState()
        sess = fake_session()
        sess.chat_inbox = [{"sender_name": "Rubens", "text": "hi", "kind": "say"}]
        err = ac.WhisperAction().check(sess, world, target_name="rubens", message="hi")
        self.assertIsNone(err)

    def test_execute_sends_whisper(self):
        world = per.WorldState()
        sess = fake_session()
        sess.chat_inbox = [{"sender_name": "Rubens", "text": "hi", "kind": "say"}]
        action = ac.WhisperAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.run(sess, world, target_name="Rubens", message="hi there")
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_MESSAGECHAT)

    def test_execute_fails_on_whisper_failed(self):
        world = per.WorldState()
        sess = fake_session()
        sess.chat_inbox = [{"sender_name": "Rubens", "text": "hi", "kind": "say"}]
        action = ac.WhisperAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "whisper_failed", "target_name": "Rubens"})
        result = action.run(sess, world, target_name="Rubens", message="hi there")
        self.assertFalse(result.ok)
        self.assertIn("Rubens", result.error)


class AcceptGroupActionTest(unittest.TestCase):
    def test_check_fails_without_pending_invite(self):
        sess = fake_session()
        sess.pending_invite = None
        err = ac.AcceptGroupAction().check(sess, None)
        self.assertIsNotNone(err)

    def test_check_passes_with_pending_invite(self):
        sess = fake_session()
        err = ac.AcceptGroupAction().check(sess, None)
        self.assertIsNone(err)

    def test_execute_sends_accept_and_clears_pending_invite(self):
        sess = fake_session()
        result = ac.AcceptGroupAction().run(sess, None)
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_GROUP_ACCEPT)
        self.assertIsNone(sess.pending_invite)
        self.assertEqual(result.detail["inviter_name"], "Rubens")
