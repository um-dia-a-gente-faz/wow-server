"""UM-92: free-text and player-name tool arguments are validated before
they reach the game. The rejected inputs below are the ones the model
actually sent in the 2026-09-19 live run."""

import unittest

from agent import actions as ac
from agent import perception as per
from agent.reflexes import follow as fr
from agent.tests.builders import FakeSession


def _Sent():
    sess = FakeSession(chat_inbox=[], events=[], race=10, player_position=(530, 0.0, 0.0, 0.0, 0.0))
    sess.packets = sess.sent
    return sess


class ChatTextTest(unittest.TestCase):
    def test_rejects_live_junk_and_json(self):
        for bad in ("}", "  ", "", ": ", "}}", '{"message": "hi"}', "[1, 2]", "}}]:,", "hi\x00there"):
            self.assertIsNotNone(ac.chat_text_error(bad), bad)

    def test_rejects_non_string(self):
        self.assertIsNotNone(ac.chat_text_error(None))
        self.assertIsNotNone(ac.chat_text_error(42))

    def test_accepts_normal_chat(self):
        for good in ("Hello!", "lol", "ok", "Anyone near the Mana Wyrms?", "Olá, tudo bem?", "5", ":) hi"):
            self.assertIsNone(ac.chat_text_error(good), good)


class PlayerNameTest(unittest.TestCase):
    def test_rejects_live_junk(self):
        for bad in ("}}dotspans", ": ", "", "A", "Rubens2", "Two Words", "Averyveryverylongname", None):
            self.assertIsNotNone(ac.player_name_error(bad), bad)

    def test_accepts_real_names(self):
        for good in ("Rubens", "Luaprata", "Farstrider", "Zé", "Ox"):
            self.assertIsNone(ac.player_name_error(good), good)


class ActionChecksTest(unittest.TestCase):
    """The chat actions are no longer registered on main (chat generation was
    deferred — docs/adr/0001, UM-98), so those tests drive the classes
    directly. Their validators matter again the moment chat is re-registered.
    """

    def setUp(self):
        self.session = _Sent()
        self.world = per.WorldState()

    def test_say_junk_never_sends(self):
        result = ac.SayAction().run(self.session, self.world, message="}")
        self.assertFalse(result.ok)
        self.assertEqual(self.session.packets, [])

    def test_say_normal_sends(self):
        result = ac.SayAction().run(self.session, self.world, message="Hello there")
        self.assertTrue(result.ok)
        self.assertEqual(len(self.session.packets), 1)

    def test_yell_and_emote_junk_rejected(self):
        self.assertFalse(ac.YellAction().run(self.session, self.world, message="{}").ok)
        self.assertFalse(ac.EmoteAction().run(self.session, self.world, text=": ").ok)
        self.assertEqual(self.session.packets, [])

    def test_whisper_bad_name_or_text_rejected(self):
        self.session.chat_inbox = [{"sender_name": "Rubens"}]
        err = ac.WhisperAction().check(self.session, self.world, target_name="}}dotspans", message="hi")
        self.assertIn("not a valid character name", err)
        err = ac.WhisperAction().check(self.session, self.world, target_name="Rubens", message="}")
        self.assertIn("not a chat message", err)
        self.assertIsNone(ac.WhisperAction().check(self.session, self.world, target_name="Rubens", message="hi"))

    def test_invite_bad_name_rejected(self):
        result = ac.REGISTRY["invite_to_group"].run(self.session, self.world, name=": ")
        self.assertFalse(result.ok)
        self.assertEqual(self.session.packets, [])

    def test_send_mail_bad_recipient_rejected_before_mailbox_check(self):
        err = ac.REGISTRY["send_mail"].check(self.session, self.world, to="}}x", subject="Hi", body="")
        self.assertIn("not a valid character name", err)

    def test_follow_bad_name_rejected(self):
        err = fr.FollowAction().check(self.session, self.world, player_name="}}dotspans")
        self.assertIn("not a valid character name", err)


if __name__ == "__main__":
    unittest.main()
