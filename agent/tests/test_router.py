"""PacketRouter: registration, unknown opcodes, tick hooks, and the session's
error isolation (WoWSession._dispatch_guarded) on top of it."""

import struct
import unittest

from agent import session as se
from agent.packets import ProtocolError
from agent.wire import PacketTruncated
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


class RouterCountersTest(unittest.TestCase):
    """The sink gets (kind, opcode) once per event; it never changes dispatch."""

    def setUp(self):
        self.events = []
        self.r = PacketRouter(count=lambda kind, op: self.events.append((kind, op)))

    def test_packets_counts_every_dispatched_packet(self):
        self.r.register(0x10, lambda c, p: None)
        self.r.dispatch("ctx", 0x10, b'')
        self.r.dispatch("ctx", 0x10, b'')
        self.assertEqual(self.events, [("packets", 0x10)] * 2)

    def test_unhandled_counts_and_also_counts_as_a_packet(self):
        self.assertFalse(self.r.dispatch("ctx", 0x99, b''))
        self.assertEqual(self.events, [("packets", 0x99), ("unhandled", 0x99)])

    def test_truncated_counts_and_still_raises(self):
        def h(c, p):
            raise PacketTruncated(0x10, 0, 4)
        self.r.register(0x10, h)
        with self.assertRaises(PacketTruncated):
            self.r.dispatch("ctx", 0x10, b'')
        self.assertEqual(self.events, [("packets", 0x10), ("truncated", 0x10)])

    def test_truncated_does_not_bump_handler_errors(self):
        def h(c, p):
            raise PacketTruncated(0x10, 0, 4)
        self.r.register(0x10, h)
        with self.assertRaises(PacketTruncated):
            self.r.dispatch("ctx", 0x10, b'')
        self.assertNotIn(("handler_errors", 0x10), self.events)

    def test_handler_errors_count_raw_parse_errors_and_logic_bugs(self):
        def protocol_error(c, p):
            raise ProtocolError("x")
        self.r.register(0x10, lambda c, p: struct.unpack('<I', p))
        self.r.register(0x11, lambda c, p: None.missing)
        self.r.register(0x12, protocol_error)
        with self.assertRaises(ProtocolError):
            self.r.dispatch("ctx", 0x10, b'ab')
        with self.assertRaises(AttributeError):
            self.r.dispatch("ctx", 0x11, b'')
        with self.assertRaises(ProtocolError):
            self.r.dispatch("ctx", 0x12, b'')
        self.assertEqual([e for e in self.events if e[0] == "handler_errors"],
                         [("handler_errors", 0x10), ("handler_errors", 0x11), ("handler_errors", 0x12)])

    def test_build_router_feeds_the_process_metrics(self):
        from agent import metrics
        metrics.ROUTER_COUNTS.clear()
        self.addCleanup(metrics.ROUTER_COUNTS.clear)
        handlers.build_router().dispatch("ctx", 0xFFFE, b'')
        self.assertEqual(metrics.ROUTER_COUNTS, {("packets", 0xFFFE): 1, ("unhandled", 0xFFFE): 1})


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
