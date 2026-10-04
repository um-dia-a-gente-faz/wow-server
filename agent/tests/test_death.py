"""Unit tests for agent.death: the release-spirit/corpse-run/reclaim/
spirit-healer-fallback state machine (UM-43). All movement is driven
through a FakeClock-backed Mover (same pattern as test_follow_reflex.py),
so no real threads or sleeping are involved. No network."""

import struct
import unittest
from types import SimpleNamespace

from agent import actions as ac
from agent import death as dt
from agent import movement as mv
from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo


PLAYER_FLAGS_GHOST = per.PLAYER_FLAGS_GHOST
UNIT_NPC_FLAG_SPIRITHEALER = per.UNIT_NPC_FLAG_SPIRITHEALER


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    def sleep(self, dt_: float):
        self.t += dt_


def fast_session(guid=0xF130000000000099, position=(0, 0.0, 0.0, 0.0, 0.0)):
    sent = []
    sess = SimpleNamespace(player_guid=guid, player_position=position, events=[],
                            corpse_position=None, corpse_reclaim_ready_at=None)
    sess._send_packet = lambda opcode, payload=b'': sent.append((opcode, payload))
    sess._sent = sent
    sess._record_event = lambda kind, **fields: sess.events.append({"kind": kind, **fields})
    world = per.WorldState()
    world.set_my_guid(guid)
    if position is not None:
        world.set_my_map(position[0])
    clock = FakeClock()
    sess._mover = mv.Mover(sess, world, clock=clock.clock, sleep=clock.sleep)
    return sess, world, clock


def create_self(world, guid, x, y, z, health, player_flags=0, max_health=100):
    world.update_object(uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=uo.TYPEID_PLAYER,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": 0.0},
        fields={uf.UNIT_FIELD_HEALTH: health, uf.UNIT_FIELD_MAXHEALTH: max_health,
                uf.PLAYER_FLAGS: player_flags},
    ))


def set_player_flags(world, guid, flags):
    world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=guid,
                                        fields={uf.PLAYER_FLAGS: flags}))


def create_corpse(world, guid, x, y, z):
    world.update_object(uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=uo.TYPEID_CORPSE,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": 0.0},
        fields={},
    ))


def create_spirit_healer(world, guid, x, y, z):
    world.update_object(uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=uo.TYPEID_UNIT,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": 0.0},
        fields={uf.UNIT_NPC_FLAGS: UNIT_NPC_FLAG_SPIRITHEALER},
    ))


class ReleaseSpiritActionTest(unittest.TestCase):
    def test_check_rejects_when_alive(self):
        sess, world, _ = fast_session()
        create_self(world, sess.player_guid, 0, 0, 0, health=100)
        action = ac.REGISTRY["release_spirit"]
        self.assertEqual(action.check(sess, world), "not dead")

    def test_check_rejects_already_ghost(self):
        sess, world, _ = fast_session()
        create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
        action = ac.REGISTRY["release_spirit"]
        self.assertEqual(action.check(sess, world), "already released — already a ghost")

    def test_execute_sends_repop_request_and_confirms_ghost(self):
        sess, world, _ = fast_session()
        create_self(world, sess.player_guid, 0, 0, 0, health=0)

        def become_ghost():
            set_player_flags(world, sess.player_guid, PLAYER_FLAGS_GHOST)

        action = ac.REGISTRY["release_spirit"]
        # confirm polling calls _is_ghost(world) repeatedly; flip it true
        # right after the packet is sent by monkeypatching _wait_for's
        # underlying check via a one-shot side effect on the first poll.
        orig_wait_for = ac._wait_for

        def wait_for_and_confirm(predicate, timeout=2.0, interval=0.1):
            become_ghost()
            return orig_wait_for(predicate, timeout=timeout, interval=interval)

        dt.actions._wait_for = wait_for_and_confirm
        try:
            result = action.execute(sess, world)
        finally:
            dt.actions._wait_for = orig_wait_for

        self.assertTrue(result.ok)
        self.assertEqual(sess._sent, [(dt.CMSG_REPOP_REQUEST, struct.pack('<B', 0)), (dt.MSG_CORPSE_QUERY, b'')])


class CorpseRunTest(unittest.TestCase):
    def test_straight_line_success(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        result = dt._corpse_run(sess, world, (0, 5.0, 0.0, 0.0))
        self.assertTrue(result["ok"])

    def test_detour_recovers_from_a_single_stuck(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        mover = sess._mover
        calls = []
        orig_move_to = mover.move_to

        def scripted_move_to(x, y, z=None, **kwargs):
            calls.append((x, y))
            if len(calls) == 1:
                return {"ok": False, "error": "stuck", "detail": {}}
            return orig_move_to(x, y, z, **kwargs)

        mover.move_to = scripted_move_to
        result = dt._corpse_run(sess, world, (0, 10.0, 0.0, 0.0))
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(calls), 2)

    def test_gives_up_after_max_detours(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        mover = sess._mover
        mover.move_to = lambda *a, **k: {"ok": False, "error": "stuck", "detail": {}}
        result = dt._corpse_run(sess, world, (0, 10.0, 0.0, 0.0), max_detours=3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "stuck")

    def test_non_stuck_failure_returns_immediately(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        mover = sess._mover
        calls = []
        mover.move_to = lambda *a, **k: (calls.append(a) or {"ok": False, "error": "target lost", "detail": {}})
        result = dt._corpse_run(sess, world, (0, 10.0, 0.0, 0.0))
        self.assertEqual(result["error"], "target lost")
        self.assertEqual(len(calls), 1)


class ReclaimCorpseActionTest(unittest.TestCase):
    def test_check_requires_ghost(self):
        sess, world, _ = fast_session()
        create_self(world, sess.player_guid, 0, 0, 0, health=100)
        action = ac.REGISTRY["reclaim_corpse"]
        self.assertEqual(action.check(sess, world), "not a ghost — call release_spirit first")

    def test_unknown_corpse_position_is_queried(self):
        sess, world, _ = fast_session()
        create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
        action = ac.REGISTRY["reclaim_corpse"]
        self.assertIsNone(action.check(sess, world))
        sent = []

        def answer(opcode, payload=b''):
            sent.append(opcode)
            if opcode == dt.MSG_CORPSE_QUERY:
                sess.corpse_position = (0, 5.0, 0.0, 0.0)

        sess._send_packet = answer
        self.assertTrue(dt._query_corpse(sess))
        self.assertEqual(sent, [dt.MSG_CORPSE_QUERY])

    def test_check_respects_reclaim_cooldown(self):
        sess, world, clock = fast_session()
        create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
        sess.corpse_position = (0, 5.0, 0.0, 0.0)
        sess.corpse_reclaim_ready_at = clock.clock() + 30
        # check() uses time.monotonic(), not the fake clock — just assert
        # it's a cooldown-shaped rejection when ready_at is far in the future.
        import time as real_time
        sess.corpse_reclaim_ready_at = real_time.monotonic() + 30
        action = ac.REGISTRY["reclaim_corpse"]
        self.assertIn("cooldown", action.check(sess, world))

    def test_execute_moves_finds_corpse_and_reclaims(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
        sess.corpse_position = (0, 5.0, 0.0, 0.0)
        create_corpse(world, 0xC001, 5.0, 0.0, 0.0)

        def resurrect_on_reclaim(opcode, payload=b''):
            sess._sent.append((opcode, payload))
            if opcode == dt.CMSG_RECLAIM_CORPSE:
                set_player_flags(world, sess.player_guid, 0)

        sess._send_packet = resurrect_on_reclaim

        action = ac.REGISTRY["reclaim_corpse"]
        result = action.execute(sess, world)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.detail["method"], "corpse")
        self.assertEqual(result.detail["corpse_guid"], 0xC001)
        self.assertEqual(sess.events[-1]["kind"], "resurrect")
        self.assertEqual(sess.events[-1]["method"], "corpse")

    def test_execute_falls_back_to_spirit_healer_when_stuck_and_no_corpse(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
        sess.corpse_position = (0, 500.0, 0.0, 0.0)  # far/unreachable, no corpse object perceived
        create_spirit_healer(world, 0xEA10, 1.0, 0.0, 0.0)

        sess._mover.move_to = lambda *a, **k: {"ok": False, "error": "stuck", "detail": {}}

        def resurrect_on_activate(opcode, payload=b''):
            sess._sent.append((opcode, payload))
            if opcode == dt.CMSG_SPIRIT_HEALER_ACTIVATE:
                set_player_flags(world, sess.player_guid, 0)

        sess._send_packet = resurrect_on_activate

        action = ac.REGISTRY["reclaim_corpse"]
        result = action.execute(sess, world)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.detail["method"], "spirit_healer")
        self.assertEqual(result.detail["guid"], 0xEA10)
        self.assertEqual(sess.events[-1]["kind"], "resurrect")
        self.assertEqual(sess.events[-1]["method"], "spirit_healer")

    def test_execute_fails_when_stuck_and_no_spirit_healer_either(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
        sess.corpse_position = (0, 500.0, 0.0, 0.0)
        sess._mover.move_to = lambda *a, **k: {"ok": False, "error": "stuck", "detail": {}}

        action = ac.REGISTRY["reclaim_corpse"]
        result = action.execute(sess, world)
        self.assertFalse(result.ok)
        self.assertIn("no spirit healer", result.error)


if __name__ == "__main__":
    unittest.main()
