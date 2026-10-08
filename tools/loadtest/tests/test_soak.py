import unittest

from tools.loadtest import soak


class SoakTest(unittest.TestCase):
    def test_refuses_non_loopback_hosts(self):
        for host in ("192.168.1.64", "10.0.0.1", "wow.example.com", "0.0.0.0"):
            with self.subTest(host=host), self.assertRaises(SystemExit):
                soak.check_host(host)
        for host in ("127.0.0.1", "localhost", "::1", "127.0.0.2"):
            soak.check_host(host)
        soak.check_host("192.168.1.64", allow_non_loopback=True)   # explicit override only

    def test_run_soak_refuses_before_starting_anything(self):
        with self.assertRaises(SystemExit):
            soak.run_soak(1, 1, host="192.168.1.64")

    def test_compare_flags_only_real_regressions(self):
        base = {"rss_per_agent_kb": 1000, "threads_per_agent": 3}
        self.assertEqual(soak.compare({"rss_per_agent_kb": 1100, "threads_per_agent": 3}, base), [])
        bad = soak.compare({"rss_per_agent_kb": 1300, "threads_per_agent": 3}, base)
        self.assertEqual(len(bad), 1)
        self.assertIn("rss_per_agent_kb", bad[0])

    def test_compare_refuses_a_run_that_does_not_match_the_baseline(self):
        shape = {"agents": 25, "duration_s": 60.0, "think_interval_ms": 100.0}
        base = {**shape, "rss_per_agent_kb": 100, "threads_per_agent": 3}
        run = {**shape, "rss_per_agent_kb": 100, "threads_per_agent": 3}
        self.assertEqual(soak.compare(run, base), [])
        for key, other in (("agents", 5), ("duration_s", 30.0), ("think_interval_ms", 1000.0)):
            with self.subTest(key=key), self.assertRaises(SystemExit) as cm:
                soak.compare({**run, key: other}, base)
            self.assertIn(key, str(cm.exception))
        for key in ("rss_per_agent_kb", "threads_per_agent"):
            with self.subTest(missing=key), self.assertRaises(SystemExit) as cm:
                soak.compare(run, {k: v for k, v in base.items() if k != key})
            self.assertIn(key, str(cm.exception))

    def test_tick_recorder_is_fixed_size_and_percentiles_work(self):
        t = soak.TickRecorder()
        before = len(t.hist)
        t.hist[1000] += 90
        t.hist[1100] += 10
        t.count, t.max = 100, 0.11
        self.assertEqual(len(t.hist), before)
        self.assertAlmostEqual(t.percentile(.5), 0.1001)
        self.assertAlmostEqual(t.percentile(.99), 0.1101)

    def test_cpu_figures_total_and_steady(self):
        s = [{"t": 1.0, "cpu_s": 0.5}, {"t": 5.0, "cpu_s": 0.6}, {"t": 10.0, "cpu_s": 0.7}]
        self.assertEqual(soak.cpu_figures(s), {"cpu_pct_total": 7.0, "cpu_pct_steady": 2.0})

    def test_smoke_three_agents_with_a_drop_and_a_reset(self):
        # The first two login attempts are faulted so recovery is exercised.
        rep = soak.run_soak(3, 5, faults="drop_after=8;drop_after=8,reset;;;", think_interval=0.05,
                            sample_every=0.5)
        self.assertEqual(rep["agents"], 3)
        self.assertGreaterEqual(rep["tick_period_ms"]["count"], 20)
        self.assertGreaterEqual(rep["reconnects"], 2)            # both faults hit a login or a session
        self.assertGreaterEqual(rep["recovery_s"]["count"], 1)
        self.assertLessEqual(rep["threads_end"], rep["threads_before"])    # nothing left behind
        self.assertGreater(rep["rss_peak_kb"], 0)
        self.assertEqual(rep["dropped_packets"], 0)


if __name__ == "__main__":
    unittest.main()
