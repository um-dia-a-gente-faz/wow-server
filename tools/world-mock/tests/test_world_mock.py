"""End-to-end: the agent's real auth + world client code against tools/world-mock.

Covers the #253 acceptance: login, update objects, a quest turn-in action and
the server's confirmation, all over real localhost sockets."""

import logging
import pathlib
import sys
import time
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

from agent import actions as ac  # noqa: E402
from agent import opcodes as op  # noqa: E402
from agent import quests as qu  # noqa: E402
from agent.__main__ import _authenticate_and_login  # noqa: E402
from agent.auth import AuthRejected, auth_logon  # noqa: E402


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class WorldMockTest(unittest.TestCase):
    def setUp(self):
        self.mock = server.WorldMock()
        self.addCleanup(self.mock.close)
        self.cfg = SimpleNamespace(wow_host="127.0.0.1", wow_auth_port=self.mock.auth_port,
                                   account=server.DEFAULT_ACCOUNT, password=server.DEFAULT_PASSWORD,
                                   verbose_packets=False, dump_packets_dir="")

    def test_wrong_password_is_rejected(self):
        with self.assertRaises(AuthRejected) as cm:
            auth_logon("127.0.0.1", self.mock.auth_port, server.DEFAULT_ACCOUNT, "hunter2x")
        self.assertEqual(cm.exception.code, 4)
        self.assertNotIn("hunter2x", str(cm.exception))

    def test_unknown_account_is_rejected_with_the_challenge_result(self):
        with self.assertRaises(AuthRejected) as cm:
            auth_logon("127.0.0.1", self.mock.auth_port, "NOSUCH", "x")
        self.assertEqual(cm.exception.code, 4)

    def test_login_then_quest_turn_in(self):
        sess, chars = _authenticate_and_login(self.cfg, logging.getLogger("test"))
        self.addCleanup(lambda: sess.sock and sess.sock.close())
        self.assertEqual([c["name"] for c in chars], ["Luaprata", "Dawnrunner", "Jevrun"])
        sess.login_character(chars[0]["guid"])
        world = sess.world_state
        try:
            # update objects: own player, the questgiver, and the quest in the log
            self.assertTrue(wait_for(lambda: world.get_object(server.QUESTGIVER_GUID) is not None
                                     and world.build_quest_log()), "login burst not applied")
            self.assertEqual(sess.player_position[0], server.MAP_ID)
            self.assertEqual(world.build_quest_log()[0]["quest_id"], server.QUEST_ID)
            # the tick asked for the quest text and the mock answered with the captured fixture
            self.assertTrue(wait_for(lambda: self.mock.packets_received(op.CMSG_QUEST_QUERY)))

            npc = server.QUESTGIVER_GUID
            res = ac.REGISTRY["complete_quest"].run(sess, world, npc_guid=npc, quest_id=server.QUEST_ID)
            self.assertTrue(res.ok, res.error)
            self.assertTrue(wait_for(lambda: (world.get_ui_state() or {}).get("kind") == "quest_offer_reward"))
            self.assertEqual(len(world.get_ui_state()["reward_choice_items"]), 2)

            res = ac.REGISTRY["turn_in_quest"].run(sess, world, npc_guid=npc, quest_id=server.QUEST_ID,
                                                   reward_choice=1)
            self.assertTrue(res.ok, res.error)
            # confirmation comes from the server, not from the send
            self.assertTrue(wait_for(lambda: any(e["kind"] == "quest_turned_in" for e in sess.events)))
            self.assertTrue(wait_for(lambda: not world.build_quest_log()))
            self.assertEqual(self.mock.packets_received(op.CMSG_QUESTGIVER_CHOOSE_REWARD),
                             [qu.build_questgiver_choose_reward(npc, server.QUEST_ID, 1)])
            self.assertEqual(sess.dropped_packets, 0)
        finally:
            sess.logout()


if __name__ == "__main__":
    unittest.main()
