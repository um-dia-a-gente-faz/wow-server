"""#321: the real agent against the death-with-corpse scenario (tools/world-mock/death_scenario.py).

Dead on login -> release_spirit -> reclaim_corpse (corpse perceived) or spirit-healer fallback
(corpse not perceived) -> alive again, confirmed by the server's VALUES update."""

import logging
import pathlib
import struct
import sys
import time
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import death_scenario as ds  # noqa: E402
import server  # noqa: E402

from agent import actions as ac  # noqa: E402
from agent import candidates  # noqa: E402
from agent.__main__ import _authenticate_and_login  # noqa: E402


def wait_for(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class DeathScenarioTest(unittest.TestCase):
    def login(self, **kw):
        mock = ds.DeathMock(**kw)
        self.addCleanup(mock.close)
        cfg = SimpleNamespace(wow_host="127.0.0.1", wow_auth_port=mock.auth_port, account=server.DEFAULT_ACCOUNT,
                              password=server.DEFAULT_PASSWORD, verbose_packets=False, dump_packets_dir="")
        sess, chars = _authenticate_and_login(cfg, logging.getLogger("test"))
        self.addCleanup(lambda: sess.sock and sess.sock.close())
        sess.login_character(chars[0]["guid"])
        self.addCleanup(sess.logout)
        world = sess.world_state
        self.assertTrue(wait_for(lambda: world.get_my_object() is not None and world.get_my_object().is_dead()),
                        "character not dead after login")
        return mock, sess, world

    def test_dead_character_is_offered_release_spirit_and_the_registry_knows_it(self):
        _mock, sess, world = self.login()
        snap = world.snapshot(my_position=sess.player_position, corpse_position=None)
        names = [c["action"] for c in candidates.generate(snap, my_guid=world.my_guid)]
        self.assertIn("release_spirit", names)
        self.assertTrue(set(names) <= set(ac.REGISTRY), set(names) - set(ac.REGISTRY))

    def test_release_then_reclaim_corpse(self):
        mock, sess, world = self.login()
        res = ac.REGISTRY["release_spirit"].run(sess, world)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(mock.packets_received(ds.CMSG_REPOP_REQUEST), [b'\x00'])
        self.assertTrue(world.get_my_object().is_ghost())
        res = ac.REGISTRY["reclaim_corpse"].run(sess, world)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.detail["method"], "corpse")
        self.assertEqual(mock.packets_received(ds.CMSG_RECLAIM_CORPSE), [struct.pack('<Q', ds.CORPSE_GUID)])
        self.assertEqual(mock.resurrected_by, "corpse")
        self.assertTrue(wait_for(lambda: not world.get_my_object().is_ghost() and not world.get_my_object().is_dead()))
        self.assertTrue(any(e["kind"] == "resurrect" for e in sess.events))

    def test_corpse_not_perceived_falls_back_to_the_spirit_healer(self):
        mock, sess, world = self.login(corpse_perceived=False)
        self.assertTrue(ac.REGISTRY["release_spirit"].run(sess, world).ok)
        res = ac.REGISTRY["reclaim_corpse"].run(sess, world)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.detail["method"], "spirit_healer")
        self.assertEqual(mock.packets_received(ds.CMSG_RECLAIM_CORPSE), [])
        self.assertEqual(mock.packets_received(ds.CMSG_SPIRIT_HEALER_ACTIVATE), [struct.pack('<Q', ds.HEALER_GUID)])
        self.assertEqual(mock.resurrected_by, "spirit_healer")
        self.assertTrue(wait_for(lambda: not world.get_my_object().is_ghost()))

    def test_reclaim_before_the_delay_elapsed_is_refused_without_a_packet(self):
        mock, sess, world = self.login(reclaim_delay_ms=60000)
        self.assertTrue(ac.REGISTRY["release_spirit"].run(sess, world).ok)
        self.assertTrue(wait_for(lambda: sess.corpse_reclaim_ready_at is not None))
        res = ac.REGISTRY["reclaim_corpse"].run(sess, world)
        self.assertFalse(res.ok)
        self.assertIn("cooldown", res.error)
        self.assertEqual(mock.packets_received(ds.CMSG_RECLAIM_CORPSE), [])


if __name__ == "__main__":
    unittest.main()
