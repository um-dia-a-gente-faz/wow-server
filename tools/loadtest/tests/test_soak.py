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
