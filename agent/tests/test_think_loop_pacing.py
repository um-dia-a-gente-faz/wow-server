import unittest
from types import SimpleNamespace
from unittest import mock

from agent import __main__ as m


class RemainingSleepTests(unittest.TestCase):
    def test_sleeps_the_remainder(self):
        self.assertAlmostEqual(m._remaining_sleep(10.0, 5.0, 10.25), 4.75)

    def test_overrun_never_negative(self):
        self.assertEqual(m._remaining_sleep(10.0, 3.0, 20.0), 0.0)


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
            m._run_think_loop(sess, cfg, duration=interval * len(cycle_costs) - 0.5, start=0.0)
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


if __name__ == "__main__":
    unittest.main()
