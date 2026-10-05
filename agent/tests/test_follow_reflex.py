"""Unit tests for agent.reflexes.follow: the follow-leader/assist reflex
(UM-58). All movement is driven through a FakeClock-backed Mover (same
pattern as test_actions_movement.py's fast_session) so tick() calls resolve
instantly, deterministically, and without real threads or sleeping."""

import unittest
from types import SimpleNamespace

from agent import actions as ac
from agent import movement as mv
from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo
from agent.reflexes import follow as fl


UNIT_FLAG_IN_COMBAT = fl.UNIT_FLAG_IN_COMBAT


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    def sleep(self, dt: float):
        self.t += dt


def object_at(guid, x, y, z, object_type="unit"):
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT,
        guid=guid,
        object_type=uo.TYPEID_PLAYER if object_type == "player" else uo.TYPEID_UNIT,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": 0.0},
        fields={},
    )


def fast_session(guid=0xF130000000000099, position=(530, 0.0, 0.0, 0.0, 0.0)):
    """Mirrors test_actions_movement.py's fast_session: a fake session pre-wired with
    a Mover on a FakeClock, so move_towards resolves instantly in tests."""
    sent = []
    sess = SimpleNamespace(player_guid=guid, player_position=position, events=[])
    sess._send_packet = lambda opcode, payload=b'': sent.append((opcode, payload))
    sess._sent = sent
    world = per.WorldState()
    if position is not None:
        world.set_my_map(position[0])
    clock = FakeClock()
    sess._mover = mv.Mover(sess, world, clock=clock.clock, sleep=clock.sleep)
    return sess, world


def set_target_guid(world, guid, target_guid):
    world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=guid,
                                        fields={uf.UNIT_FIELD_TARGET: target_guid, uf.UNIT_FIELD_TARGET + 1: 0}))


def set_faction(world, guid, faction):
    world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=guid,
                                        fields={uf.UNIT_FIELD_FACTIONTEMPLATE: faction}))


def set_unit_flags(world, guid, flags):
    world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=guid,
                                        fields={uf.UNIT_FIELD_FLAGS: flags}))


def set_health(world, guid, health):
    world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=guid,
                                        fields={uf.UNIT_FIELD_HEALTH: health}))


class FollowReflexToggleTest(unittest.TestCase):
    def test_disabled_by_default(self):
        reflex = fl.FollowReflex()
        self.assertFalse(reflex.enabled)

    def test_tick_is_noop_when_disabled(self):
        sess, world = fast_session()
        reflex = fl.FollowReflex()
        reflex.tick(sess, world)  # no leader configured, nothing to do
        self.assertEqual(sess._sent, [])

    def test_start_enables_and_sets_state(self):
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, leader_name="Rubens", distance=4.0)
        self.assertTrue(reflex.enabled)
        self.assertEqual(reflex.leader_guid, 5)
        self.assertEqual(reflex.leader_name, "Rubens")
        self.assertEqual(reflex.distance, 4.0)

    def test_stop_disables_and_records_follow_stopped_event(self):
        sess, world = fast_session()
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5)
        reflex.stop(sess, world, reason="llm_requested")
        self.assertFalse(reflex.enabled)
        self.assertEqual(sess.events[-1]["kind"], "follow_stopped")
        self.assertEqual(sess.events[-1]["reason"], "llm_requested")

    def test_stop_when_never_started_records_nothing(self):
        sess, world = fast_session()
        reflex = fl.FollowReflex()
        reflex.stop(sess, world)
        self.assertEqual(sess.events, [])


class FollowMovementTest(unittest.TestCase):
    def test_walks_towards_leader_when_far(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world.update_object(object_at(5, 20.0, 0.0, 0.0, object_type="player"))
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)

        reflex.tick(sess, world)

        opcodes = [op for op, _ in sess._sent]
        self.assertIn(mv.MSG_MOVE_START_FORWARD, opcodes)
        self.assertIn(mv.MSG_MOVE_STOP, opcodes)
        self.assertTrue(reflex.enabled)  # arrived, still following

    def test_stops_and_faces_leader_when_already_within_range(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world.update_object(object_at(5, 2.0, 0.0, 0.0, object_type="player"))  # within distance+margin
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)

        reflex.tick(sess, world)

        opcodes = [op for op, _ in sess._sent]
        self.assertNotIn(mv.MSG_MOVE_START_FORWARD, opcodes)
        self.assertIn(mv.MSG_MOVE_SET_FACING, opcodes)

    def test_no_movement_when_already_within_stop_distance(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world.update_object(object_at(5, 1.0, 0.0, 0.0, object_type="player"))
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)
        reflex.tick(sess, world)
        opcodes = [op for op, _ in sess._sent]
        self.assertNotIn(mv.MSG_MOVE_START_FORWARD, opcodes)


class LeaderLostTest(unittest.TestCase):
    def test_leader_missing_from_perception_records_leader_lost_and_disables(self):
        sess, world = fast_session()
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)

        reflex.tick(sess, world)  # guid 5 was never perceived

        self.assertFalse(reflex.enabled)
        self.assertEqual(sess.events[-1]["kind"], "leader_lost")

    def test_leader_with_unknown_position_is_treated_as_lost(self):
        sess, world = fast_session()
        world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=5,
                                            object_type=uo.TYPEID_PLAYER, movement={}, fields={}))
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)

        reflex.tick(sess, world)

        self.assertFalse(reflex.enabled)
        self.assertEqual(sess.events[-1]["kind"], "leader_lost")

    def test_own_position_unknown_is_treated_as_lost(self):
        sess, world = fast_session(position=None)
        world.update_object(object_at(5, 1.0, 0.0, 0.0, object_type="player"))
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)

        reflex.tick(sess, world)

        self.assertFalse(reflex.enabled)
        self.assertEqual(sess.events[-1]["kind"], "leader_lost")


class AssistTest(unittest.TestCase):
    def _combat_ready_leader_and_target(self, world, leader_guid=5, target_guid=9,
                                         leader_pos=(1.0, 0.0, 0.0)):
        world.update_object(object_at(leader_guid, *leader_pos, object_type="player"))
        world.update_object(object_at(target_guid, 1.0, 1.0, 0.0, object_type="unit"))
        set_faction(world, target_guid, 14)  # different from our (unset -> None) faction: hostile per is_hostile_to
        set_unit_flags(world, target_guid, UNIT_FLAG_IN_COMBAT)
        set_target_guid(world, leader_guid, target_guid)
        return world.get_object(leader_guid)

    def test_no_assist_when_assist_off(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        self._combat_ready_leader_and_target(world)
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)  # assist defaults to False

        reflex.tick(sess, world)

        opcodes = [op for op, _ in sess._sent]
        self.assertNotIn(ac.CMSG_ATTACKSWING, opcodes)

    def test_assists_hostile_in_combat_target(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        self._combat_ready_leader_and_target(world)
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)
        reflex.set_assist(True)

        reflex.tick(sess, world)

        opcodes = [op for op, _ in sess._sent]
        self.assertIn(ac.CMSG_SET_SELECTION, opcodes)
        self.assertIn(ac.CMSG_ATTACKSWING, opcodes)
        self.assertEqual(reflex._assist_target_guid, 9)
        self.assertEqual(sess.events[-1]["kind"], "leader_changed_target")

    def test_does_not_reissue_attack_for_same_target(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        self._combat_ready_leader_and_target(world)
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)
        reflex.set_assist(True)

        reflex.tick(sess, world)
        attack_opcodes_after_first = [op for op, _ in sess._sent if op in (ac.CMSG_SET_SELECTION, ac.CMSG_ATTACKSWING)]
        reflex.tick(sess, world)
        attack_opcodes_after_second = [op for op, _ in sess._sent if op in (ac.CMSG_SET_SELECTION, ac.CMSG_ATTACKSWING)]

        # Facing the (stationary) leader may resend MSG_MOVE_SET_FACING each
        # tick, but the assist attack itself must not be reissued.
        self.assertEqual(attack_opcodes_after_first, attack_opcodes_after_second)

    def test_stops_attacking_when_target_dies(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        self._combat_ready_leader_and_target(world)
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)
        reflex.set_assist(True)
        reflex.tick(sess, world)
        self.assertIsNotNone(reflex._assist_target_guid)

        set_health(world, 9, 0)
        reflex.tick(sess, world)

        opcodes = [op for op, _ in sess._sent]
        self.assertIn(ac.CMSG_ATTACKSTOP, opcodes)
        self.assertIsNone(reflex._assist_target_guid)

    def test_stops_attacking_when_leader_clears_target(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        self._combat_ready_leader_and_target(world)
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)
        reflex.set_assist(True)
        reflex.tick(sess, world)

        set_target_guid(world, 5, 0)
        reflex.tick(sess, world)

        opcodes = [op for op, _ in sess._sent]
        self.assertIn(ac.CMSG_ATTACKSTOP, opcodes)
        self.assertIsNone(reflex._assist_target_guid)

    def test_does_not_assist_known_friendly_target(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        leader = self._combat_ready_leader_and_target(world)
        # Give our own player object the same faction as the target -> friendly.
        world.set_my_guid(0xF130000000000099)
        world.update_object(object_at(0xF130000000000099, 0.0, 0.0, 0.0, object_type="player"))
        set_faction(world, 0xF130000000000099, 14)
        set_faction(world, 9, 14)
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)
        reflex.set_assist(True)

        reflex.tick(sess, world)

        opcodes = [op for op, _ in sess._sent]
        self.assertNotIn(ac.CMSG_ATTACKSWING, opcodes)

    def test_does_not_assist_when_target_not_in_combat(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world.update_object(object_at(5, 1.0, 0.0, 0.0, object_type="player"))
        world.update_object(object_at(9, 1.0, 1.0, 0.0, object_type="unit"))
        set_faction(world, 9, 14)
        set_target_guid(world, 5, 9)  # no UNIT_FLAG_IN_COMBAT set on either side
        reflex = fl.FollowReflex()
        reflex.start(leader_guid=5, distance=3.0)
        reflex.set_assist(True)

        reflex.tick(sess, world)

        opcodes = [op for op, _ in sess._sent]
        self.assertNotIn(ac.CMSG_ATTACKSWING, opcodes)


class AssistOffTurnsOffCurrentAttackTest(unittest.TestCase):
    def test_assist_action_off_stops_current_attack(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world.update_object(object_at(5, 1.0, 0.0, 0.0, object_type="player"))
        world.update_object(object_at(9, 1.0, 1.0, 0.0, object_type="unit"))
        set_faction(world, 9, 14)
        set_unit_flags(world, 9, UNIT_FLAG_IN_COMBAT)
        set_target_guid(world, 5, 9)

        reflex = fl.get_follow_reflex(sess)
        reflex.start(leader_guid=5, distance=3.0)
        reflex.set_assist(True)
        reflex.tick(sess, world)
        self.assertIsNotNone(reflex._assist_target_guid)

        result = ac.REGISTRY["assist"].run(sess, world, on=False)

        self.assertTrue(result.ok)
        self.assertIsNone(reflex._assist_target_guid)
        opcodes = [op for op, _ in sess._sent]
        self.assertIn(ac.CMSG_ATTACKSTOP, opcodes)


class LlmOverridePausesFollowTest(unittest.TestCase):
    def test_move_to_pauses_an_active_follow(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world.update_object(object_at(5, 20.0, 0.0, 0.0, object_type="player"))
        reflex = fl.get_follow_reflex(sess)
        reflex.start(leader_guid=5, distance=3.0)

        ac.MoveToAction().execute(sess, world, x=1.0, y=0.0)

        self.assertFalse(reflex.enabled)
        self.assertEqual(sess.events[-1]["kind"], "follow_stopped")
        self.assertEqual(sess.events[-1]["reason"], "llm_override")

    def test_stop_movement_pauses_an_active_follow(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world.update_object(object_at(5, 1.0, 0.0, 0.0, object_type="player"))
        reflex = fl.get_follow_reflex(sess)
        reflex.start(leader_guid=5, distance=3.0)

        ac.StopMovementAction().execute(sess, world)

        self.assertFalse(reflex.enabled)

    def test_move_to_is_a_noop_on_follow_state_when_nothing_following(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        ac.MoveToAction().execute(sess, world, x=1.0, y=0.0)
        self.assertEqual(sess.events, [])  # no spurious follow_stopped with nothing following


class FollowToolActionsTest(unittest.TestCase):
    def test_follow_action_check_fails_when_player_not_perceived(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        err = ac.REGISTRY["follow"].check(sess, world, player_name="Rubens")
        self.assertIsNotNone(err)

    def test_follow_action_starts_reflex_by_resolved_name(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world.update_object(object_at(5, 1.0, 0.0, 0.0, object_type="player"))
        world.get_object(5).name = "Rubens"

        result = ac.REGISTRY["follow"].run(sess, world, player_name="Rubens", distance=4.0)

        self.assertTrue(result.ok)
        reflex = fl.get_follow_reflex(sess)
        self.assertTrue(reflex.enabled)
        self.assertEqual(reflex.leader_guid, 5)
        self.assertEqual(reflex.distance, 4.0)

    def test_assist_action_requires_active_follow(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        err = ac.REGISTRY["assist"].check(sess, world, on=True)
        self.assertIsNotNone(err)

    def test_stop_following_action_disables_reflex(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        reflex = fl.get_follow_reflex(sess)
        reflex.start(leader_guid=5, distance=3.0)

        result = ac.REGISTRY["stop_following"].run(sess, world)

        self.assertTrue(result.ok)
        self.assertTrue(result.detail["was_following"])
        self.assertFalse(reflex.enabled)

    def test_catalog_includes_the_three_tools(self):
        names = {schema["name"] for schema in ac.catalog()}
        self.assertIn("follow", names)
        self.assertIn("assist", names)
        self.assertIn("stop_following", names)


if __name__ == "__main__":
    unittest.main()
