"""Unit tests for agent.movement: the MovementInfo wire writer used by
agent.actions.FaceAction, and UM-38's straight-line movement engine."""

import math
import struct
import threading
import time
import unittest
from types import SimpleNamespace

from agent import movement as mv
from agent import packets as pk
from agent import perception as per
from agent import update_object as uo


class BuildMovementInfoTest(unittest.TestCase):
    def test_basic_shape_matches_packed_guid_plus_fields(self):
        body = mv.build_movement_info(0xF13000000000042, 1.0, 2.0, 3.0, 0.5)
        guid, off = pk.unpack_packed_guid(body, 0)
        self.assertEqual(guid, 0xF13000000000042)
        move_flags, move_flags2, client_time = struct.unpack_from('<IHI', body, off)
        off += 4 + 2 + 4
        x, y, z, o = struct.unpack_from('<4f', body, off)
        off += 16
        fall_time = struct.unpack_from('<I', body, off)[0]
        off += 4
        self.assertEqual(move_flags, 0)
        self.assertEqual(move_flags2, 0)
        self.assertEqual((x, y, z, o), (1.0, 2.0, 3.0, 0.5))
        self.assertEqual(fall_time, 0)
        self.assertEqual(off, len(body))  # nothing else written for the plain case

    def test_client_time_is_nonzero_and_fits_uint32(self):
        body = mv.build_movement_info(1, 0, 0, 0, 0)
        _, off = pk.unpack_packed_guid(body, 0)
        off += 4 + 2
        client_time = struct.unpack_from('<I', body, off)[0]
        self.assertGreater(client_time, 0)
        self.assertLess(client_time, 2 ** 32)

    def test_pitch_written_when_swimming(self):
        body = mv.build_movement_info(1, 0, 0, 0, 0, move_flags=uo.MOVEMENTFLAG_SWIMMING, pitch=0.75)
        _, off = pk.unpack_packed_guid(body, 0)
        off += 4 + 2 + 4 + 16  # flags, flags2, time, x/y/z/o
        pitch = struct.unpack_from('<f', body, off)[0]
        self.assertAlmostEqual(pitch, 0.75)

    def test_pitch_not_written_when_not_applicable(self):
        with_pitch = mv.build_movement_info(1, 0, 0, 0, 0, move_flags=uo.MOVEMENTFLAG_SWIMMING)
        without_pitch = mv.build_movement_info(1, 0, 0, 0, 0)
        self.assertEqual(len(with_pitch), len(without_pitch) + 4)

    def test_spline_elevation_written_when_flagged(self):
        body = mv.build_movement_info(1, 0, 0, 0, 0, move_flags=uo.MOVEMENTFLAG_SPLINE_ELEVATION,
                                       spline_elevation=12.5)
        elevation = struct.unpack_from('<f', body, len(body) - 4)[0]
        self.assertAlmostEqual(elevation, 12.5)

    def test_ontransport_not_supported(self):
        with self.assertRaises(NotImplementedError):
            mv.build_movement_info(1, 0, 0, 0, 0, move_flags=uo.MOVEMENTFLAG_ONTRANSPORT)

    def test_falling_not_supported(self):
        with self.assertRaises(NotImplementedError):
            mv.build_movement_info(1, 0, 0, 0, 0, move_flags=uo.MOVEMENTFLAG_FALLING)


class SendSetFacingTest(unittest.TestCase):
    def test_sends_msg_move_set_facing_with_own_guid_and_orientation(self):
        sent = []
        sess = SimpleNamespace(player_guid=0xF130000000000099,
                                _send_packet=lambda opcode, payload=b'': sent.append((opcode, payload)))
        mv.send_set_facing(sess, 10.0, 20.0, 30.0, 1.57)
        self.assertEqual(len(sent), 1)
        opcode, payload = sent[0]
        self.assertEqual(opcode, mv.MSG_MOVE_SET_FACING)
        guid, off = pk.unpack_packed_guid(payload, 0)
        self.assertEqual(guid, 0xF130000000000099)
        x, y, z, o = struct.unpack_from('<4f', payload, off + 4 + 2 + 4)
        self.assertEqual((x, y, z), (10.0, 20.0, 30.0))
        self.assertAlmostEqual(o, 1.57, places=5)


def fake_session(guid=0xF130000000000099, position=(530, 0.0, 0.0, 0.0, 0.0)):
    sent = []
    sess = SimpleNamespace(player_guid=guid, player_position=position)
    sess._send_packet = lambda opcode, payload=b'': sent.append((opcode, payload))
    sess._sent = sent
    return sess


class FakeClock:
    """A controllable clock/sleep pair: sleep() advances the clock instead
    of actually blocking, so _simulate's tick loop runs instantly in tests
    while its elapsed-time-based stuck detection still sees real deltas."""

    def __init__(self):
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    def sleep(self, dt: float):
        self.t += dt


class SimulateArrivalTest(unittest.TestCase):
    def test_arrives_within_stop_distance(self):
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        clock = FakeClock()
        result = mv._simulate(sess, world, lambda: (10.0, 0.0, 0.0), stop_distance=1.0,
                               run_speed=7.0, tick_interval=0.3, stop_event=threading.Event(),
                               clock=clock.clock, sleep=clock.sleep)
        self.assertTrue(result["ok"])
        self.assertLessEqual(result["detail"]["distance"], 1.0)

    def test_final_position_mirrored_onto_session_and_world(self):
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_guid(sess.player_guid)
        world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=sess.player_guid,
                                            object_type=uo.TYPEID_PLAYER,
                                            movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION,
                                                      "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0},
                                            fields={}))
        clock = FakeClock()
        mv._simulate(sess, world, lambda: (10.0, 0.0, 0.0), stop_distance=1.0, run_speed=7.0,
                     tick_interval=0.3, stop_event=threading.Event(), clock=clock.clock, sleep=clock.sleep)
        self.assertAlmostEqual(sess.player_position[1], 10.0, delta=1.0)
        self.assertAlmostEqual(world.get_my_object().position[1], sess.player_position[1])

    def test_heading_points_at_target(self):
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        clock = FakeClock()
        mv._simulate(sess, world, lambda: (0.0, 10.0, 0.0), stop_distance=1.0, run_speed=7.0,
                     tick_interval=0.3, stop_event=threading.Event(), clock=clock.clock, sleep=clock.sleep)
        # due north (+y): orientation pi/2
        self.assertAlmostEqual(sess.player_position[4], math.pi / 2, places=3)

    def test_z_interpolates_toward_destination(self):
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        clock = FakeClock()
        stop_event = threading.Event()

        # A destination 100 yd away takes ~48 ticks to reach at 7 yd/s —
        # stop after 5 ticks to inspect a mid-flight, partially-interpolated z.
        def sleep_then_maybe_stop(dt, calls={"n": 0}):
            clock.sleep(dt)
            calls["n"] += 1
            if calls["n"] >= 5:
                stop_event.set()

        result = mv._simulate(sess, world, lambda: (100.0, 0.0, 50.0), stop_distance=1.0, run_speed=7.0,
                               tick_interval=0.3, stop_event=stop_event, clock=clock.clock,
                               sleep=sleep_then_maybe_stop)
        self.assertEqual(result["error"], "stopped")
        self.assertGreater(result["detail"]["position"][2], 0.0)
        self.assertLess(result["detail"]["position"][2], 50.0)

    def test_no_target_position_fails_immediately(self):
        sess = fake_session()
        world = per.WorldState()
        clock = FakeClock()
        result = mv._simulate(sess, world, lambda: None, stop_distance=1.0, run_speed=7.0,
                               tick_interval=0.3, stop_event=threading.Event(), clock=clock.clock, sleep=clock.sleep)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no target position")
        self.assertEqual(sess._sent, [])  # never even sent START_FORWARD


class SimulateStuckDetectionTest(unittest.TestCase):
    def test_zero_speed_triggers_no_progress_stuck(self):
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        clock = FakeClock()
        result = mv._simulate(sess, world, lambda: (100.0, 0.0, 0.0), stop_distance=1.0, run_speed=0.0,
                               tick_interval=0.3, stop_event=threading.Event(), clock=clock.clock, sleep=clock.sleep)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "stuck")
        self.assertEqual(result["detail"]["reason"], "no_progress")

    def test_stale_pre_move_server_position_is_not_drift(self):
        # Live-verified finding: the server never echoes our own position
        # back via update-object for ordinary client-driven movement, so
        # my_server_position sits at wherever it was at login/spawn for the
        # entire session. Comparing against it unconditionally made every
        # move_to past SERVER_DRIFT_MAX_YD immediately "stuck" — this must
        # NOT happen just because my_server_position predates the move.
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_guid(sess.player_guid)
        world.set_my_map(530)
        world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=sess.player_guid,
                                            object_type=uo.TYPEID_PLAYER,
                                            movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION,
                                                      "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0},
                                            fields={}))
        self.assertEqual(world.my_server_position[1:3], (0.0, 0.0))  # stale, from before the move
        clock = FakeClock()
        result = mv._simulate(sess, world, lambda: (10.0, 0.0, 0.0), stop_distance=1.0, run_speed=7.0,
                               tick_interval=0.3, stop_event=threading.Event(), clock=clock.clock, sleep=clock.sleep)
        self.assertTrue(result["ok"])  # arrives normally — no false "stuck"

    def test_first_ever_server_position_arriving_mid_move_is_not_drift(self):
        # Live-verified race: our own CREATE block can still be in flight on
        # the recv thread when move_to starts (my_server_position is still
        # None), then arrives a tick or two later reporting our *real,
        # unchanged* spawn position. That must not look like "drift from
        # nothing" just because None != a real tuple.
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_guid(sess.player_guid)
        world.set_my_map(530)
        self.assertIsNone(world.my_server_position)  # not arrived yet

        calls = {"n": 0}

        def get_target():
            calls["n"] += 1
            if calls["n"] == 2:
                world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=sess.player_guid,
                                                     object_type=uo.TYPEID_PLAYER,
                                                     movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION,
                                                               "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0},
                                                     fields={}))
            return 5.0, 0.0, 0.0

        clock = FakeClock()
        result = mv._simulate(sess, world, get_target, stop_distance=1.0, run_speed=7.0,
                               tick_interval=0.3, stop_event=threading.Event(), clock=clock.clock, sleep=clock.sleep)
        self.assertTrue(result["ok"])

    def test_server_position_change_mid_move_triggers_stuck(self):
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_guid(sess.player_guid)
        world.set_my_map(530)
        world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=sess.player_guid,
                                            object_type=uo.TYPEID_PLAYER,
                                            movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION,
                                                      "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0},
                                            fields={}))

        # A genuine mid-flight correction (teleport/knockback) arrives after
        # the first tick — get_target() doubles as the per-tick hook here.
        calls = {"n": 0}

        def get_target():
            calls["n"] += 1
            if calls["n"] == 2:
                world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_MOVEMENT, guid=sess.player_guid,
                                                     movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION,
                                                               "x": 50.0, "y": 50.0, "z": 0.0, "o": 0.0}))
            return 10.0, 0.0, 0.0

        clock = FakeClock()
        result = mv._simulate(sess, world, get_target, stop_distance=1.0, run_speed=7.0,
                               tick_interval=0.3, stop_event=threading.Event(), clock=clock.clock, sleep=clock.sleep)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "stuck")
        self.assertEqual(result["detail"]["reason"], "server_drift")

    def test_target_lost_mid_flight(self):
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        calls = {"n": 0}

        def get_target():
            calls["n"] += 1
            return None if calls["n"] > 1 else (10.0, 0.0, 0.0)

        clock = FakeClock()
        result = mv._simulate(sess, world, get_target, stop_distance=1.0, run_speed=7.0,
                               tick_interval=0.3, stop_event=threading.Event(), clock=clock.clock, sleep=clock.sleep)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "target lost")

    def test_stop_event_set_before_loop_stops_immediately(self):
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        stop_event = threading.Event()
        stop_event.set()
        clock = FakeClock()
        result = mv._simulate(sess, world, lambda: (100.0, 0.0, 0.0), stop_distance=1.0, run_speed=7.0,
                               tick_interval=0.3, stop_event=stop_event, clock=clock.clock, sleep=clock.sleep)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "stopped")
        self.assertEqual(clock.t, 0.0)  # never slept — stopped before the first tick


class SimulateReFacingTest(unittest.TestCase):
    def test_refaces_when_target_moves_past_drift_threshold(self):
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        # A target that jumps from due-east to due-north after the first
        # tick — well past the 10 degree re-face threshold.
        calls = {"n": 0}

        def get_target():
            calls["n"] += 1
            return (0.0, 100.0, 0.0) if calls["n"] > 1 else (100.0, 0.0, 0.0)

        clock = FakeClock()
        mv._simulate(sess, world, get_target, stop_distance=1.0, run_speed=7.0,
                     tick_interval=0.3, stop_event=threading.Event(), clock=clock.clock, sleep=clock.sleep)
        opcodes = [op for op, _ in sess._sent]
        self.assertIn(mv.MSG_MOVE_SET_FACING, opcodes)
        self.assertIn(mv.MSG_MOVE_START_FORWARD, opcodes)
        self.assertIn(mv.MSG_MOVE_HEARTBEAT, opcodes)


class MoverTest(unittest.TestCase):
    def test_move_to_sends_stop_on_arrival(self):
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        clock = FakeClock()
        mover = mv.Mover(sess, world, clock=clock.clock, sleep=clock.sleep)
        result = mover.move_to(5.0, 0.0, stop_distance=1.0, run_speed=7.0, tick_interval=0.3)
        self.assertTrue(result["ok"])
        opcodes = [op for op, _ in sess._sent]
        self.assertEqual(opcodes[0], mv.MSG_MOVE_START_FORWARD)
        self.assertEqual(opcodes[-1], mv.MSG_MOVE_STOP)

    def test_stop_returns_false_when_nothing_running(self):
        sess = fake_session()
        world = per.WorldState()
        mover = mv.Mover(sess, world)
        self.assertFalse(mover.stop())

    def test_stop_interrupts_an_in_flight_move(self):
        # Real threading + real time here (no FakeClock): a move_to toward a
        # far target with a slow tick, stopped from the main thread shortly
        # after starting. Bounded and fast (tick_interval is small).
        sess = fake_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        mover = mv.Mover(sess, world)
        results = []

        def run():
            results.append(mover.move_to(1000.0, 0.0, stop_distance=1.0, run_speed=7.0, tick_interval=0.05))

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.15)  # let a couple of ticks happen
        was_moving = mover.stop()
        t.join(timeout=2.0)

        self.assertTrue(was_moving)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"], "stopped")

    def test_get_mover_returns_same_instance_for_a_session(self):
        sess = fake_session()
        world = per.WorldState()
        m1 = mv.get_mover(sess, world)
        m2 = mv.get_mover(sess, world)
        self.assertIs(m1, m2)


class RunSpeedOfTest(unittest.TestCase):
    def test_default_when_no_speeds_known(self):
        self.assertEqual(mv.run_speed_of(None), mv.DEFAULT_RUN_SPEED_YPS)
        self.assertEqual(mv.run_speed_of(per.ObjectInfo(guid=1)), mv.DEFAULT_RUN_SPEED_YPS)

    def test_uses_perceived_run_speed(self):
        obj = per.ObjectInfo(guid=1, speeds=(2.5, 8.0, 4.5, 4.7, 2.5, 3.14, 7.0, 4.5, 3.14))
        self.assertEqual(mv.run_speed_of(obj), 8.0)


if __name__ == '__main__':
    unittest.main()
