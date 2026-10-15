"""#321: the real agent against the death-with-corpse scenario (tools/world-mock/death_scenario.py).

Dead on login -> release_spirit -> reclaim_corpse (corpse perceived) or spirit-healer fallback
(corpse not perceived) -> alive again, confirmed by the server's VALUES update."""

import logging
import pathlib
import struct
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import death_scenario as ds  # noqa: E402
import server  # noqa: E402

from agent import actions as ac  # noqa: E402
from agent import candidates  # noqa: E402
from agent.login import authenticate_and_login  # noqa: E402


class DeathScenarioTest(unittest.TestCase):
    def login(self, **kw):
        mock = ds.DeathMock(**kw)
        self.addCleanup(mock.close)
        cfg = SimpleNamespace(wow_host="127.0.0.1", wow_auth_port=mock.auth_port, account=server.DEFAULT_ACCOUNT,
                              password=server.DEFAULT_PASSWORD, verbose_packets=False, dump_packets_dir="")
        sess, chars = ds.run_step("authenticate", lambda: authenticate_and_login(cfg, logging.getLogger("test")))
        self.addCleanup(lambda: sess.sock and sess.sock.close())
        ds.run_step("login character", lambda: sess.login_character(chars[0]["guid"]))
        self.addCleanup(sess.logout)
        world = sess.world_state
        ds.wait_step("dead after login", lambda: world.get_my_object() is not None and world.get_my_object().is_dead())
        return mock, sess, world

    def test_dead_character_is_offered_release_spirit_and_the_registry_knows_it(self):
        _mock, sess, world = self.login()
        snap = world.snapshot(my_position=sess.player_position, corpse_position=None)
        names = [c["action"] for c in candidates.generate(snap, my_guid=world.my_guid)]
        self.assertIn("release_spirit", names)
        self.assertTrue(set(names) <= set(ac.REGISTRY), set(names) - set(ac.REGISTRY))

    def test_release_then_reclaim_corpse(self):
        mock, sess, world = self.login()
        res = ds.run_step("release_spirit", lambda: ac.REGISTRY["release_spirit"].run(sess, world))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(mock.packets_received(ds.CMSG_REPOP_REQUEST), [b'\x00'])
        self.assertTrue(world.get_my_object().is_ghost())
        res = ds.run_step("reclaim_corpse", lambda: ac.REGISTRY["reclaim_corpse"].run(sess, world))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.detail["method"], "corpse")
        self.assertEqual(mock.packets_received(ds.CMSG_RECLAIM_CORPSE), [struct.pack('<Q', ds.CORPSE_GUID)])
        self.assertEqual(mock.resurrected_by, "corpse")
        ds.wait_step("alive after the corpse reclaim", lambda: not world.get_my_object().is_ghost() and not world.get_my_object().is_dead())
        self.assertTrue(any(e["kind"] == "resurrect" for e in sess.events))

    def test_corpse_not_perceived_falls_back_to_the_spirit_healer(self):
        mock, sess, world = self.login(corpse_perceived=False)
        self.assertTrue(ds.run_step("release_spirit", lambda: ac.REGISTRY["release_spirit"].run(sess, world)).ok)
        res = ds.run_step("reclaim_corpse", lambda: ac.REGISTRY["reclaim_corpse"].run(sess, world))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.detail["method"], "spirit_healer")
        self.assertEqual(mock.packets_received(ds.CMSG_RECLAIM_CORPSE), [])
        self.assertEqual(mock.packets_received(ds.CMSG_SPIRIT_HEALER_ACTIVATE), [struct.pack('<Q', ds.HEALER_GUID)])
        self.assertEqual(mock.resurrected_by, "spirit_healer")
        ds.wait_step("alive after the spirit healer", lambda: not world.get_my_object().is_ghost())

    def test_reclaim_before_the_delay_elapsed_is_refused_without_a_packet(self):
        mock, sess, world = self.login(reclaim_delay_ms=60000)
        self.assertTrue(ds.run_step("release_spirit", lambda: ac.REGISTRY["release_spirit"].run(sess, world)).ok)
        ds.wait_step("reclaim delay received", lambda: sess.corpse_reclaim_ready_at is not None)
        res = ds.run_step("reclaim_corpse", lambda: ac.REGISTRY["reclaim_corpse"].run(sess, world))
        self.assertFalse(res.ok)
        self.assertIn("cooldown", res.error)
        self.assertEqual(mock.packets_received(ds.CMSG_RECLAIM_CORPSE), [])


if __name__ == "__main__":
    unittest.main()
