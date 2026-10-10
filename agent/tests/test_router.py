"""PacketRouter: registration, unknown opcodes, tick hooks, and the session's
error isolation (WoWSession._dispatch_guarded) on top of it."""

import struct
import unittest

from agent import session as se
from agent.packets import ProtocolError
from agent import handlers
from agent.router import Context, PacketRouter
from agent.tests.builders import make_session


class PacketRouterTest(unittest.TestCase):
    def test_dispatch_calls_registered_handler(self):
        r, seen = PacketRouter(), []
        r.register(0x10, lambda ctx, payload: seen.append((ctx, payload)))
        self.assertTrue(r.dispatch("ctx", 0x10, b'ab'))
        self.assertEqual(seen, [("ctx", b'ab')])

    def test_unknown_opcode_returns_false(self):
        self.assertFalse(PacketRouter().dispatch("ctx", 0x99, b''))

    def test_raw_parse_errors_become_protocol_error_but_logic_bugs_do_not(self):
        r = PacketRouter()
        r.register(0x10, lambda c, p: struct.unpack('<I', p))
        r.register(0x11, lambda c, p: None.missing)
        with self.assertRaises(ProtocolError) as cm:
            r.dispatch("ctx", 0x10, b'ab')
        self.assertIsInstance(cm.exception.__cause__, struct.error)
        with self.assertRaises(AttributeError):
            r.dispatch("ctx", 0x11, b'')

    def test_duplicate_registration_is_an_error(self):
        r = PacketRouter()
        r.register_all({0x10: lambda c, p: None})
        with self.assertRaises(ValueError):
            r.register(0x10, lambda c, p: None)

    def test_tick_runs_every_hook_in_order(self):
        r, order = PacketRouter(), []
        r.on_tick(lambda ctx: order.append(1))
        r.on_tick(lambda ctx: order.append(2))
        r.tick("ctx")
        self.assertEqual(order, [1, 2])

    def test_context_send_uses_the_transports_current_send(self):
        sent = []

        class T:
            def _send_packet(self, op, payload=b''):
                sent.append((op, payload))
        Context("state", T()).send(0x1, b'x')
        self.assertEqual(sent, [(0x1, b'x')])


class SessionDispatchTest(unittest.TestCase):
    def test_unknown_opcode_is_not_handled(self):
        self.assertFalse(make_session()._dispatch(0xFFFF, b''))

    def test_every_domain_registered_on_the_session_router(self):
        router = make_session().router
        for op in (se.SMSG_TIME_SYNC_REQ, se.SMSG_LOGOUT_COMPLETE, 0x0A9, 0x12A, 0x096):
            self.assertIn(op, router._handlers)

    def test_two_sessions_can_have_different_routers(self):
        stub = PacketRouter()
        stub.register(0x10, lambda ctx, payload: None)
        a, b = make_session(router=stub), make_session()
        self.assertIsNot(a.router, b.router)
        self.assertTrue(a._dispatch(0x10, b''))
        self.assertFalse(b._dispatch(0x10, b''))
        self.assertFalse(a._dispatch(0x0A9, b''))

    def test_each_session_gets_a_fresh_router(self):
        self.assertIsNot(make_session().router, make_session().router)

    def test_build_router_order_is_declared_and_pinned(self):
        self.assertEqual([m.__name__.rpartition('.')[2] for m in handlers.MODULES],
                         ["death", "world", "chat", "group", "mail", "npc", "spells", "trade", "loot", "quests"])
        router = handlers.build_router()
        self.assertEqual([f.__name__ for f in router._ticks],
                         ["send_name_queries", "send_npc_text_queries", "send_item_queries", "send_quest_queries"])
        ops = list(router._handlers)
        self.assertEqual((ops[0], ops[-1]), (0x269, 0x198))  # death first, quests last; the session adds its lifecycle opcodes after

    def test_guarded_dispatch_isolates_handler_errors(self):
        sess = make_session()
        # Truncated SMSG_UPDATE_OBJECT: the handler raises, the session survives.
        with self.assertLogs('agent.session', level='WARNING'):
            self.assertTrue(sess._dispatch_guarded(0x0A9, b'\x01'))
        self.assertEqual(sess.dropped_packets, 1)

    def test_guarded_dispatch_passes_unknown_opcode_through_as_false(self):
        self.assertFalse(make_session()._dispatch_guarded(0xFFFF, b''))


if __name__ == '__main__':
    unittest.main()
