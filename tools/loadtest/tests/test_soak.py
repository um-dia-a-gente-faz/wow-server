import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tools.loadtest import soak
from tools.loadtest.__main__ import main


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

    def test_main_checks_the_baseline_before_soaking(self):
        shape = {"agents": 25, "duration_s": 60.0, "think_interval_ms": 100.0}
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            json.dump({**shape, "rss_per_agent_kb": 1, "threads_per_agent": 1}, f)
            f.flush()
            with mock.patch.object(soak, "run_soak", side_effect=AssertionError("soaked")), \
                    self.assertRaises(SystemExit) as cm:
                main(["--agents", "5", "--duration", "60", "--compare", f.name])
        self.assertIn("agents", str(cm.exception))

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

    def test_container_items_counts_the_nested_caches(self):
        from agent import handles, items, names, npc, quests
        ws = SimpleNamespace(names=names.NameCache(), items=items.ItemCache(), quest_texts=quests.QuestCache(),
                             npc_texts=npc.NpcTextCache(), handles=handles.HandleMap(), other=SimpleNamespace(x=[1]))
        before = soak.container_items(ws)
        ws.names.players[1] = "a"
        ws.items.items[1] = {}
        ws.quest_texts.quests[1] = {}
        ws.npc_texts.texts[1] = {}
        ws.handles.handle_for(1)       # one entry in each of _by_guid and _by_handle
        self.assertEqual(soak.container_items(ws) - before, 6)

    def test_packet_rate_is_packets_over_seconds(self):
        self.assertEqual(soak.packet_rate(500, 10.0), 50.0)
        self.assertIsNone(soak.packet_rate(5, 0))

    def test_stream_run_reports_the_recv_packet_rate(self):
        rep = soak.run_soak(2, 6, think_interval=0.05, sample_every=0.5, stream_hz=20)
        self.assertEqual(rep["stream_hz"], 20)
        # 3 packets per tick at 20 Hz = 60/s per agent; a login gap costs a few percent. A half-rate
        # (10 Hz = 30/s) or double-rate stream falls outside +-15 %.
        for rate in rep["recv_packets_per_agent"].values():
            self.assertTrue(51 <= rate <= 69, rate)
        self.assertEqual(rep["dropped_packets"], 0)
        self.assertGreater(rep["recv_packets_total"], 0)

    def test_report_carries_the_stream_pool(self):
        """#486: the pool is echoed in the report; the default is the mock's STREAM_POOL, 0 = unbounded."""
        rep = soak.run_soak(1, 1, think_interval=0.05, sample_every=0.5, stream_hz=5)
        self.assertEqual(rep["stream_pool"], 50)
        rep = soak.run_soak(1, 1, think_interval=0.05, sample_every=0.5, stream_hz=5, stream_pool=0)
        self.assertEqual(rep["stream_pool"], 0)

    def test_cli_passes_the_stream_pool_to_the_soak(self):
        with mock.patch.object(soak, "run_soak", return_value={"agents": 1}) as run:
            self.assertEqual(main(["--agents", "1", "--duration", "1", "--stream-pool", "0"]), 0)
            self.assertEqual(run.call_args.kwargs["stream_pool"], 0)
            main(["--agents", "1", "--duration", "1"])
            self.assertEqual(run.call_args.kwargs["stream_pool"], 50)

    def test_run_soak_hands_the_pool_to_the_mock(self):
        with mock.patch.object(soak.server, "WorldMock", wraps=soak.server.WorldMock) as spy:
            soak.run_soak(1, 1, think_interval=0.05, sample_every=0.5, stream_hz=5, stream_pool=0)
        self.assertEqual(spy.call_args.kwargs["stream_pool"], 0)

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
