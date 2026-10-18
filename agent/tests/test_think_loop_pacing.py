import unittest
from types import SimpleNamespace
from unittest import mock

from agent import runloop as m
from agent.http_api import AgentObserver


class RemainingSleepTests(unittest.TestCase):
    def test_sleeps_the_remainder(self):
        self.assertAlmostEqual(m.remaining_sleep(10.0, 5.0, 10.25), 4.75)

    def test_overrun_never_negative(self):
        self.assertEqual(m.remaining_sleep(10.0, 3.0, 20.0), 0.0)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


class ThinkLoopPacingTests(unittest.TestCase):
    def _run(self, cycle_costs, interval=3.0):
        clock = FakeClock()
        costs = iter(cycle_costs)
        starts = []

        def objects():
            starts.append(clock.now)
            clock.now += next(costs)  # the cycle takes this long
            return {}

        sess = SimpleNamespace(unexpected_disconnect=False,
                               world_state=SimpleNamespace(get_objects=objects))
        cfg = SimpleNamespace(think_interval=interval)
        with mock.patch.object(m.time, "monotonic", clock.monotonic), \
             mock.patch.object(m.time, "sleep", clock.sleep):
            m.run_think_loop(sess, cfg, duration=interval * len(cycle_costs) - 0.5, start=0.0)
        return starts, clock.sleeps

    def test_period_is_interval_not_interval_plus_cycle(self):
        starts, sleeps = self._run([0.25, 1.0, 0.0])
        self.assertEqual(starts, [0.0, 3.0, 6.0])
        self.assertTrue(all(s >= 0 for s in sleeps))

    def test_overrun_does_not_sleep_negative_or_start_early(self):
        starts, sleeps = self._run([5.0, 0.5], interval=3.0)
        self.assertEqual(sleeps[0], 0.0)
        self.assertEqual(starts[1], 5.0)  # right after the overrun, not before
        self.assertTrue(all(s >= 0 for s in sleeps))


class _Stop:
    """A threading.Event stand-in for the reflex and think loops: runs `cycles` iterations, then stops."""

    def __init__(self, cycles):
        self.left = cycles
        self.waits = 0

    def is_set(self):
        return self.left <= 0

    def wait(self, timeout=None):
        self.left -= 1
        self.waits += 1


class _Reflex:
    def __init__(self):
        self.ticks = 0

    def tick(self, sess, world):
        self.ticks += 1


class ControlHoldTests(unittest.TestCase):
    """#330: a paused brain skips think_and_act but keeps the cycle's sleep; a held reflex is skipped the same way."""

    def _think(self, observer, cycles, interval=3.0):
        clock = FakeClock()
        sess = SimpleNamespace(unexpected_disconnect=False, player_position=None,
                               world_state=SimpleNamespace(get_objects=lambda: {}))
        cfg = SimpleNamespace(think_interval=interval, persona=None)
        think = mock.Mock(return_value=SimpleNamespace(ok=True, error=None))
        with mock.patch.object(m.time, "monotonic", clock.monotonic), \
             mock.patch.object(m.time, "sleep", clock.sleep), \
             mock.patch.object(m, "think_and_act", think):
            m.run_think_loop(sess, cfg, duration=interval * cycles - 0.5, start=0.0,
                             brain=object(), observer=observer)
        return think.call_count, clock.sleeps

    def test_paused_brain_decides_nothing_but_still_sleeps_and_resumes(self):
        observer = AgentObserver("Hold")
        observer.control_state.pause()
        calls, sleeps = self._think(observer, cycles=4)
        self.assertEqual((calls, len(sleeps)), (0, 4))
        observer.control_state.resume()
        calls, sleeps = self._think(observer, cycles=3)
        self.assertEqual((calls, len(sleeps)), (3, 3))

    def test_override_holds_the_brain_too(self):
        observer = AgentObserver("Hold")
        observer.control_state.override()
        self.assertEqual(self._think(observer, cycles=2)[0], 0)
        observer.control_state.release()
        self.assertEqual(self._think(observer, cycles=2)[0], 2)

    def test_held_reflexes_are_skipped_and_ticked_again_after_release(self):
        held = {"on": True}
        for loop, getter in ((m.run_follow_reflex_loop, "get_follow_reflex"),
                             (m.run_rest_reflex_loop, "get_rest_reflex")):
            with self.subTest(loop=loop.__name__):
                reflex = _Reflex()
                sess = SimpleNamespace(world_state=None)
                with mock.patch.object(m, getter, return_value=reflex):
                    stop = _Stop(3)
                    loop(sess, stop, tick_interval=0.0, suspended=lambda: held["on"])
                    self.assertEqual((reflex.ticks, stop.waits), (0, 3))
                    held["on"] = False
                    loop(sess, _Stop(2), tick_interval=0.0, suspended=lambda: held["on"])
                    self.assertEqual(reflex.ticks, 2)
                held["on"] = True

    def test_run_loop_hands_the_override_to_both_reflex_loops(self):
        observer = AgentObserver("Hold")
        sess = SimpleNamespace()
        cfg = SimpleNamespace(think_interval=1.0)
        with mock.patch.object(m, "run_follow_reflex_loop") as follow, \
             mock.patch.object(m, "run_rest_reflex_loop") as rest, \
             mock.patch.object(m, "run_think_loop", return_value=False):
            m.run_loop(sess, cfg, duration=None, observer=observer)
        for loop in (follow, rest):
            suspended = loop.call_args.kwargs["suspended"]
            self.assertFalse(suspended())
            observer.control_state.override()
            self.assertTrue(suspended())
            observer.control_state.release()


if __name__ == "__main__":
    unittest.main()
