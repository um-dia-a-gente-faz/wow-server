"""Tests for agent.actions.movement (split out of test_actions.py, issue #248).
"""

import math
import unittest

from agent import actions as ac
from agent import movement as mv
from agent import packets as pk
from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo
from agent.tests.actions_helpers import fake_session, object_at


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    def sleep(self, dt: float):
        self.t += dt


def fast_session(guid=0xF130000000000099, position=(530, 0.0, 0.0, 0.0, 0.0)):
    """A fake_session pre-wired with a Mover on a FakeClock, so move_to/
    move_towards/stop_movement actions run instantly in tests instead of
    sleeping for real between ticks."""
    sess = fake_session(player_guid=guid, player_position=position)
    world = per.WorldState()
    clock = FakeClock()
    sess._mover = mv.Mover(sess, world, clock=clock.clock, sleep=clock.sleep)
    return sess, world


class SetTargetActionTest(unittest.TestCase):
    def test_check_fails_for_unperceived_guid(self):
        world = per.WorldState()
        err = ac.SetTargetAction().check(None, world, guid=5)
        self.assertIsNotNone(err)

    def test_check_passes_for_perceived_guid(self):
        world = per.WorldState()
        world.update_object(object_at(5, 1.0, 2.0, 3.0))
        err = ac.SetTargetAction().check(None, world, guid=5)
        self.assertIsNone(err)

    def test_execute_times_out_when_target_field_never_updates(self):
        world = per.WorldState()
        world.set_my_guid(1)
        world.update_object(object_at(1, 0.0, 0.0, 0.0, object_type="player"))
        world.update_object(object_at(5, 1.0, 2.0, 3.0))
        sess = fake_session()

        action = ac.SetTargetAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, guid=5)

        self.assertIn(ac.CMSG_SET_SELECTION, [op for op, _ in sess._sent])
        self.assertFalse(result.ok)  # nothing ever set target_guid in this fake world

    def test_execute_confirms_once_target_field_updates(self):
        world = per.WorldState()
        world.set_my_guid(1)
        world.update_object(object_at(1, 0.0, 0.0, 0.0, object_type="player"))
        world.update_object(object_at(5, 1.0, 2.0, 3.0))
        sess = fake_session()

        # UNIT_FIELD_TARGET (a guid-typed field spanning two slots) already
        # set before execute() runs — simplest way to exercise the "found on
        # the first poll" path without a real background thread in the test.
        world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=1,
                                            fields={uf.UNIT_FIELD_TARGET: 5, uf.UNIT_FIELD_TARGET + 1: 0}))
        self.assertEqual(world.get_my_object().target_guid, 5)

        action = ac.SetTargetAction()
        action.confirm_timeout = 0.5
        action.confirm_interval = 0.01
        result = action.execute(sess, world, guid=5)
        self.assertTrue(result.ok)


class FaceActionTest(unittest.TestCase):
    def test_check_requires_guid_or_xy(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.FaceAction().check(sess, world))

    def test_check_rejects_both_guid_and_xy(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.FaceAction().check(sess, world, guid=1, x=1.0, y=1.0))

    def test_check_requires_own_position(self):
        sess = fake_session(player_position=None)
        world = per.WorldState()
        self.assertIsNotNone(ac.FaceAction().check(sess, world, x=1.0, y=1.0))

    def test_check_requires_known_target_position(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.FaceAction().check(sess, world, guid=99))

    def test_execute_faces_east_toward_xy(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 3.14))
        world = per.WorldState()
        result = ac.FaceAction().execute(sess, world, x=10.0, y=0.0)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(result.detail["orientation"], 0.0, places=4)
        self.assertAlmostEqual(sess.player_position[4], 0.0, places=4)
        self.assertEqual(sess.player_position[1:4], (0.0, 0.0, 0.0))  # position unchanged, only facing

    def test_execute_faces_toward_a_guid(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 0.0, 10.0, 0.0))  # due north (+y)
        result = ac.FaceAction().execute(sess, world, guid=5)
        self.assertAlmostEqual(result.detail["orientation"], math.pi / 2, places=4)

    def test_execute_sends_msg_move_set_facing(self):
        sess = fake_session(player_guid=0x42, player_position=(530, 1.0, 2.0, 3.0, 0.0))
        world = per.WorldState()
        ac.FaceAction().execute(sess, world, x=11.0, y=2.0)
        opcodes = [op for op, _ in sess._sent]
        self.assertIn(mv.MSG_MOVE_SET_FACING, opcodes)
        _, payload = sess._sent[0]
        guid, off = pk.unpack_packed_guid(payload, 0)
        self.assertEqual(guid, 0x42)


class MoveToActionTest(unittest.TestCase):
    def test_check_requires_own_position(self):
        sess = fake_session(player_position=None)
        world = per.WorldState()
        self.assertIsNotNone(ac.MoveToAction().check(sess, world, x=1.0, y=1.0))

    def test_execute_arrives_and_returns_ok(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        result = ac.MoveToAction().execute(sess, world, x=5.0, y=0.0, stop_distance=1.0)
        self.assertTrue(result.ok)
        opcodes = [op for op, _ in sess._sent]
        self.assertEqual(opcodes[0], mv.MSG_MOVE_START_FORWARD)
        self.assertEqual(opcodes[-1], mv.MSG_MOVE_STOP)

    def test_execute_reuses_the_same_mover_as_stop_movement(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        ac.MoveToAction().execute(sess, world, x=5.0, y=0.0)
        self.assertIs(sess._mover, mv.get_mover(sess, world))


class MoveTowardsActionTest(unittest.TestCase):
    def test_check_fails_when_target_has_no_position(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.MoveTowardsAction().check(sess, world, guid=99))

    def test_execute_chases_and_arrives(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world.update_object(object_at(5, 5.0, 0.0, 0.0))
        result = ac.MoveTowardsAction().execute(sess, world, guid=5, stop_distance=1.0)
        self.assertTrue(result.ok)


class StopMovementActionTest(unittest.TestCase):
    def test_returns_ok_with_was_moving_false_when_idle(self):
        sess, world = fast_session()
        result = ac.StopMovementAction().execute(sess, world)
        self.assertTrue(result.ok)
        self.assertFalse(result.detail["was_moving"])
