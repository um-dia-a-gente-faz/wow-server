"""Unit tests for agent.death: the release-spirit/corpse-run/reclaim/
spirit-healer-fallback state machine (UM-43). All movement is driven
through a FakeClock-backed Mover (same pattern as test_follow_reflex.py),
so no real threads or sleeping are involved. No network."""

import struct
import unittest
from types import SimpleNamespace
from unittest import mock

from agent import actions as ac
from agent import candidates
from agent import death as dt
from agent import movement as mv
from agent import perception as per
from agent import think
from agent import update_fields as uf
from agent import update_object as uo
from agent.handlers import death as hd
from agent.tests.builders import FakeSession
from agent.tests.test_think import FakeLLMClient
from agent.tests.restore import join_new_threads
from agent.unmet import UnmetIntents


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
    sess = FakeSession(player_guid=guid, player_position=position, events=[],
                            corpse_position=None, corpse_reclaim_ready_at=None)
    world = per.WorldState()
    world.set_my_guid(guid)
    if position is not None:
        world.set_my_map(position[0])
    clock = FakeClock()
    sess.mover = mv.Mover(sess, world, clock=clock.clock, sleep=clock.sleep)
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


def set_fields(world, guid, fields):
    world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=guid, fields=fields))


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

    def test_execute_sends_the_same_bytes_as_before_the_port_rename(self):
        # #411: the packets captured from the code before session.send_packet replaced
        # session._send_packet in agent.death. Replaced at the sending end, so the test sees
        # what reaches the transport, through FakeSession.send_packet.
        sess, world, _ = fast_session()
        create_self(world, sess.player_guid, 0, 0, 0, health=1)

        def become_ghost(opcode, payload=b''):
            sess.sent.append((opcode, payload))
            if opcode == dt.CMSG_REPOP_REQUEST:
                set_player_flags(world, sess.player_guid, PLAYER_FLAGS_GHOST)

        sess._send_packet = become_ghost
        result = ac.REGISTRY["release_spirit"].execute(sess, world)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(sess.sent, [(dt.CMSG_REPOP_REQUEST, bytes.fromhex("00")),
                                     (dt.MSG_CORPSE_QUERY, b"")])


class CorpseRunTest(unittest.TestCase):
    def setUp(self):
        join_new_threads(self)

    def test_straight_line_success(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        result = dt._corpse_run(sess, world, (0, 5.0, 0.0, 0.0))
        self.assertTrue(result["ok"])

    def test_detour_recovers_from_a_single_stuck(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        mover = sess.mover
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
        mover = sess.mover
        mover.move_to = lambda *a, **k: {"ok": False, "error": "stuck", "detail": {}}
        result = dt._corpse_run(sess, world, (0, 10.0, 0.0, 0.0), max_detours=3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "stuck")

    def test_non_stuck_failure_returns_immediately(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        mover = sess.mover
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

        sess.mover.move_to = lambda *a, **k: {"ok": False, "error": "stuck", "detail": {}}

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

    def test_execute_queries_unknown_corpse_then_runs_and_reclaims(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
        create_corpse(world, 0xC001, 5.0, 0.0, 0.0)
        targets = []
        orig_run = dt._corpse_run

        def answer(opcode, payload=b''):
            sess._sent.append((opcode, payload))
            if opcode == dt.MSG_CORPSE_QUERY:
                sess.corpse_position = (0, 5.0, 0.0, 0.0)
            if opcode == dt.CMSG_RECLAIM_CORPSE:
                set_player_flags(world, sess.player_guid, 0)

        sess._send_packet = answer
        with mock.patch.object(dt, "_corpse_run",
                               lambda s, w, target, **k: targets.append(target) or orig_run(s, w, target, **k)):
            result = ac.REGISTRY["reclaim_corpse"].execute(sess, world)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.detail["method"], "corpse")
        self.assertEqual(sess._sent[0], (dt.MSG_CORPSE_QUERY, b''))
        self.assertEqual(targets, [(0, 5.0, 0.0, 0.0)])

    def test_execute_falls_back_to_spirit_healer_when_corpse_query_times_out(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
        create_spirit_healer(world, 0xEA10, 1.0, 0.0, 0.0)

        def answer(opcode, payload=b''):  # never answers MSG_CORPSE_QUERY
            sess._sent.append((opcode, payload))
            if opcode == dt.CMSG_SPIRIT_HEALER_ACTIVATE:
                set_player_flags(world, sess.player_guid, 0)

        sess._send_packet = answer
        with mock.patch.object(dt, "RESURRECT_CONFIRM_TIMEOUT_S", 0.01), \
                mock.patch.object(dt, "_corpse_run", side_effect=AssertionError("no corpse run")):
            result = ac.REGISTRY["reclaim_corpse"].execute(sess, world)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.detail["method"], "spirit_healer")
        self.assertEqual(sess._sent[0], (dt.MSG_CORPSE_QUERY, b''))

    def test_execute_fails_when_stuck_and_no_spirit_healer_either(self):
        sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
        create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
        sess.corpse_position = (0, 500.0, 0.0, 0.0)
        sess.mover.move_to = lambda *a, **k: {"ok": False, "error": "stuck", "detail": {}}

        action = ac.REGISTRY["reclaim_corpse"]
        result = action.execute(sess, world)
        self.assertFalse(result.ok)
        self.assertIn("no spirit healer", result.error)



class _Clock:
    """Fake monotonic clock: no real sleeps, shared by the loop guard and check()'s cooldown."""
    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t


class CorpsePositionOnResurrectTest(unittest.TestCase):
    def test_corpse_position_is_cleared_once_alive_again(self):
        sess, world, _ = fast_session()
        create_self(world, sess.player_guid, 0, 0, 0, health=100)
        sess.corpse_position = (0, 5.0, 0.0, 0.0)
        hd.check_death_transition(SimpleNamespace(state=sess), world.get_my_object())
        self.assertIsNone(sess.corpse_position)

    def test_corpse_position_is_kept_while_still_a_ghost(self):
        sess, world, _ = fast_session()
        create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
        sess.corpse_position = (0, 5.0, 0.0, 0.0)
        hd.check_death_transition(SimpleNamespace(state=sess), world.get_my_object())
        self.assertEqual(sess.corpse_position, (0, 5.0, 0.0, 0.0))


class GhostRelogTest(unittest.TestCase):
    """A character that logs in as a ghost (released before the disconnect) is offered and allowed reclaim_corpse."""

    def test_ghost_snapshot_offers_and_allows_reclaim_corpse(self):
        sess, world, _ = fast_session()
        create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
        snap = world.snapshot(my_position=sess.player_position, corpse_position=None)
        self.assertTrue(snap["is_ghost"])
        names = [c["action"] for c in candidates.generate(snap, my_guid=world.my_guid)]
        self.assertIn("reclaim_corpse", names)
        self.assertNotIn("release_spirit", names)
        self.assertIsNone(ac.REGISTRY["reclaim_corpse"].check(sess, world))


class ReclaimLoopGuardWalkTest(unittest.TestCase):
    """#521: an LLM that asks for reclaim_corpse every cycle is alive once the reclaim delay (and the loop guard's
    cooldown) has passed. Real think_and_act, ThinkState, ReclaimCorpseAction.check(), a fake clock and 3 s cycles;
    only execute() is stubbed, since its confirmation waits on the wire."""

    CYCLE_S = 3.0

    def _walk(self, delay_s):
        sess, world, _ = fast_session()
        create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
        clock = _Clock()
        state = think.ThinkState(clock=clock.now)
        llm = FakeLLMClient(action_name="reclaim_corpse", params={})
        resurrected = []

        def fake_execute(action, session, world, **_):
            resurrected.append(clock.t)
            return ac.ActionResult(ok=True, detail={"method": "corpse"})

        fake_time = SimpleNamespace(monotonic=clock.now)
        with mock.patch.object(dt, "time", fake_time), mock.patch.object(hd, "time", fake_time), \
             mock.patch.object(dt.ReclaimCorpseAction, "execute", fake_execute):
            # SMSG_CORPSE_RECLAIM_DELAY, through the real handler, right after the release.
            hd.handle_corpse_reclaim_delay(SimpleNamespace(state=sess), struct.pack('<I', int(delay_s * 1000)))
            while not resurrected and clock.t < 20 * 60:
                think.think_and_act(sess, world, llm, my_position=sess.player_position,
                                    registry=ac.REGISTRY, state=state)
                clock.t += self.CYCLE_S
        return resurrected[0] if resurrected else None

    def test_alive_within_delay_plus_cooldown_plus_one_cycle(self):
        for delay_s in (30.0, 120.0):
            with self.subTest(delay_s=delay_s):
                alive_at = self._walk(delay_s)
                self.assertIsNotNone(alive_at, "still a ghost after 20 minutes")
                self.assertLessEqual(alive_at, delay_s + think.LOOP_GUARD_COOLDOWN_S + self.CYCLE_S)
                self.assertGreaterEqual(alive_at, delay_s)  # never before the server's delay

GRAVEYARD = (0, 50.0, 0.0, 0.0)


def _unreachable_corpse_session(graveyard=GRAVEYARD):
    """A ghost at the corpse-query position (5 yd out), no corpse object in perception, and the
    spirit healer only at the graveyard: it appears in perception once the fake mover arrives there."""
    sess, world, _clock = fast_session(position=(0, 0.0, 0.0, 0.0, 0.0))
    create_self(world, sess.player_guid, 0, 0, 0, health=1, player_flags=PLAYER_FLAGS_GHOST)
    sess.corpse_position = (0, 5.0, 0.0, 0.0)
    sess.graveyard_position = graveyard
    return sess, world


def _arrive_at_graveyard(sess, world, healer_at=GRAVEYARD, moves=None):
    """A fake mover: a move to the graveyard puts a spirit healer into perception there."""
    def move_to(x, y, z=None, **_kw):
        if moves is not None:
            moves.append((x, y))
        if (x, y) == (healer_at[1], healer_at[2]):
            create_spirit_healer(world, 0xEA10, x, y, 0.0)
        return {"ok": True, "error": None, "detail": {}}

    sess.mover.move_to = move_to
    sess.mover.move_towards = lambda guid, **_kw: {"ok": True, "error": None, "detail": {}}


def _resurrect_on_activate(sess, world):
    def answer(opcode, payload=b''):
        sess._sent.append((opcode, payload))
        if opcode == dt.CMSG_SPIRIT_HEALER_ACTIVATE:
            set_player_flags(world, sess.player_guid, 0)
    sess._send_packet = answer


class CorpseUnreachableTest(unittest.TestCase):
    """#763: the corpse run arrives where the corpse was, and there is no corpse object there
    (an instance death: ActualMapID is the dungeon entrance). The way back is the graveyard's spirit healer."""

    def test_walks_to_graveyard_and_activates_the_healer_flagged(self):
        sess, world = _unreachable_corpse_session()
        moves = []
        _arrive_at_graveyard(sess, world, moves=moves)
        _resurrect_on_activate(sess, world)

        result = ac.REGISTRY["reclaim_corpse"].execute(sess, world)

        self.assertTrue(result.ok, result.error)
        self.assertIn((GRAVEYARD[1], GRAVEYARD[2]), moves)
        self.assertEqual(result.detail["method"], "spirit_healer")
        self.assertEqual(result.detail["guid"], 0xEA10)
        self.assertIs(result.detail["corpse_unreachable"], True)
        self.assertIn((dt.CMSG_SPIRIT_HEALER_ACTIVATE, struct.pack('<Q', 0xEA10)), sess._sent)

    def test_failure_when_the_graveyard_has_no_healer_is_still_flagged(self):
        sess, world = _unreachable_corpse_session()
        _arrive_at_graveyard(sess, world, healer_at=(0, 999.0, 0.0, 0.0))  # no healer at the graveyard

        result = ac.REGISTRY["reclaim_corpse"].execute(sess, world)

        self.assertFalse(result.ok)
        self.assertIn("no spirit healer", result.error)
        self.assertIs(result.detail["corpse_unreachable"], True)

    def test_graveyard_on_another_map_is_not_walked_and_the_failure_is_flagged(self):
        sess, world = _unreachable_corpse_session(graveyard=(1, 50.0, 0.0, 0.0))
        moves = []
        _arrive_at_graveyard(sess, world, moves=moves)

        result = ac.REGISTRY["reclaim_corpse"].execute(sess, world)

        self.assertFalse(result.ok)
        self.assertEqual(moves, [(5.0, 0.0)])  # only the corpse run; no walk to the other map's graveyard
        self.assertIs(result.detail["corpse_unreachable"], True)

    def test_a_reachable_corpse_is_not_flagged(self):
        sess, world = _unreachable_corpse_session()
        create_corpse(world, 0xC001, 5.0, 0.0, 0.0)
        sess.mover.move_to = lambda *a, **k: {"ok": True, "error": None, "detail": {}}
        sess._send_packet = lambda opcode, payload=b'': None  # never confirms: falls through to the healer path

        with mock.patch.object(dt, "RESURRECT_CONFIRM_TIMEOUT_S", 0.01):
            result = ac.REGISTRY["reclaim_corpse"].execute(sess, world)

        self.assertNotIn("corpse_unreachable", result.detail or {})


class CorpseUnreachableRecordTest(unittest.TestCase):
    """#763 record: one unmet intent per death. UnmetIntents' own 60 s dedup is kept out of the
    way by a clock that moves 90 s between cycles, so only the per-death flag can stop a second record."""

    CYCLE_S = 90.0

    def setUp(self):
        self.sess, self.world = _unreachable_corpse_session()
        self.clock = _Clock()
        self.unmet = UnmetIntents("Luaprata", "explore", clock=self.clock.now)
        self.state = think.ThinkState(clock=self.clock.now)
        self.llm = FakeLLMClient(action_name="reclaim_corpse", params={})
        _arrive_at_graveyard(self.sess, self.world, healer_at=(0, 999.0, 0.0, 0.0))  # the healer never shows up

    def _think(self):
        return think.think_and_act(self.sess, self.world, self.llm, my_position=self.sess.player_position,
                                   registry=ac.REGISTRY, state=self.state, unmet=self.unmet)

    def _next_cycle(self):
        self.clock.t += self.CYCLE_S

    def _records(self):
        return [r for r in self.unmet.recent() if r["kind"] == "corpse_unreachable"]

    def test_two_cycles_in_one_death_record_once(self):
        self._think()
        self._next_cycle()
        self._think()
        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["wanted"]["action"], "reclaim_corpse")

    def test_a_second_death_records_again(self):
        self._think()
        self._next_cycle()
        self._think()
        self.assertEqual(len(self._records()), 1)

        # resurrected (alive, not a ghost), then a second death
        set_fields(self.world, self.sess.player_guid, {uf.UNIT_FIELD_HEALTH: 100, uf.PLAYER_FLAGS: 0})
        hd.check_death_transition(SimpleNamespace(state=self.sess), self.world.get_my_object())
        set_fields(self.world, self.sess.player_guid, {uf.UNIT_FIELD_HEALTH: 1, uf.PLAYER_FLAGS: PLAYER_FLAGS_GHOST})
        self.sess.corpse_position = (0, 5.0, 0.0, 0.0)
        self._next_cycle()
        self._think()

        self.assertEqual(len(self._records()), 2)


if __name__ == "__main__":
    unittest.main()
