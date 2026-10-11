"""Unit tests for agent.metrics: derive_metrics()/render_prometheus_text()
against the synthetic fixture (agent/tests/fixtures/audit/luaprata_sample.jsonl)
and small hand-built record lists. No filesystem/network beyond reading the
fixture file itself."""

import os
import unittest

from agent.metrics import derive_metrics, iter_records, render_prometheus_text, xp_per_hour

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "audit", "luaprata_sample.jsonl")


class DeriveMetricsFixtureTest(unittest.TestCase):
    def setUp(self):
        self.records = list(iter_records(FIXTURE))
        self.by_agent = derive_metrics(self.records)
        self.m = self.by_agent["Luaprata"]

    def test_cycles_total(self):
        self.assertEqual(self.m.cycles_total, 5)

    def test_valid_invalid_tool_call_split(self):
        self.assertEqual(self.m.valid_tool_calls_total, 4)
        self.assertEqual(self.m.invalid_tool_calls_total, 1)

    def test_actions_by_name_and_outcome(self):
        pairs = dict(self.m.actions_total)
        self.assertEqual(pairs[("face", "ok")], 1)
        self.assertEqual(pairs[("move_to", "ok")], 1)
        self.assertEqual(pairs[("nonexistent_action", "invalid")], 1)
        self.assertEqual(pairs[("auto_attack", "failed")], 1)
        self.assertEqual(pairs[("auto_attack", "ok")], 1)

    def test_token_totals(self):
        self.assertEqual(self.m.prompt_tokens_total, 120 + 130 + 140 + 125)
        self.assertEqual(self.m.completion_tokens_total, 12 + 15 + 10 + 11)

    def test_latency_histogram_counts(self):
        # latencies: 350, 420, (null), 6000, 300
        self.assertEqual(self.m.latency_count, 4)
        self.assertEqual(self.m.latency_bucket_counts[500], 3)   # 350, 420, 300
        self.assertEqual(self.m.latency_bucket_counts[10000], 4)  # + 6000

    def test_deaths_and_levelups(self):
        self.assertEqual(self.m.deaths_total, 0)
        self.assertEqual(self.m.levelups_total, 1)

    def test_xp_per_hour(self):
        # xp 100 @ t=1731000005 -> xp 600 @ t=1731003600, ~1 hour apart
        rate = xp_per_hour(self.m)
        self.assertIsNotNone(rate)
        self.assertGreater(rate, 400)
        self.assertLess(rate, 600)


class RenderPrometheusTextTest(unittest.TestCase):
    def test_renders_expected_metric_families(self):
        records = list(iter_records(FIXTURE))
        by_agent = derive_metrics(records)
        text = render_prometheus_text(by_agent)
        for expected in (
            "wow_agent_audit_cycles_total",
            "wow_agent_audit_tool_calls_total",
            "wow_agent_action_total",
            "wow_agent_llm_prompt_tokens_total",
            "wow_agent_llm_completion_tokens_total",
            "wow_agent_llm_latency_ms_bucket",
            "wow_agent_deaths_total",
            "wow_agent_levelups_total",
            "wow_agent_xp_per_hour",
        ):
            self.assertIn(expected, text)
        self.assertIn('agent="Luaprata"', text)

    def test_empty_input_yields_only_headers(self):
        text = render_prometheus_text({})
        self.assertNotIn('agent="', text)


class DeriveMetricsMultiAgentTest(unittest.TestCase):
    def test_groups_by_agent_field(self):
        records = [
            {"agent": "A", "ts": 1, "valid": True, "result": {"ok": True},
             "tool_call": {"name": "face", "args": {}}},
            {"agent": "B", "ts": 1, "valid": False, "result": {"ok": False},
             "tool_call": {"name": None, "args": {}}},
        ]
        by_agent = derive_metrics(records)
        self.assertEqual(set(by_agent.keys()), {"A", "B"})
        self.assertEqual(by_agent["A"].valid_tool_calls_total, 1)
        self.assertEqual(by_agent["B"].invalid_tool_calls_total, 1)

    def test_jev_usage_errors_and_zero_series(self):
        records = [
            {"agent": "Jev1", "ts": __import__("time").time(), "brain": "jev",
             "jev_status": "success", "usage": {"input_tokens": 123, "output_tokens": 4, "cost": 0.01},
             "confidence": 0.8, "latency_ms": 120, "valid": True, "result": {"ok": True}},
            {"agent": "Jev1", "ts": __import__("time").time(), "brain": "llm",
             "jev_status": "http_429", "fallback": "jev failed", "valid": True, "result": {"ok": True}},
        ]
        # Unknown status text is intentionally ignored to keep labels bounded.
        records[-1]["jev_status"] = "http_4xx"
        text = render_prometheus_text(derive_metrics(records))
        self.assertIn('wow_agent_jev_calls_total{agent="Jev1"} 1', text)
        self.assertIn('wow_agent_jev_errors_total{agent="Jev1",status="http_4xx"} 1', text)
        self.assertIn('wow_agent_jev_fallback_total{agent="Jev1"} 1', text)
        self.assertIn('wow_agent_jev_prompt_tokens_total{agent="Jev1"} 123', text)
        self.assertIn('wow_agent_jev_confidence{agent="Jev1"} 0.8', text)

    def test_jev_metric_families_exist_with_no_agents(self):
        text = render_prometheus_text({})
        for name in ("wow_agent_jev_calls_total", "wow_agent_jev_errors_total",
                     "wow_agent_jev_fallback_total", "wow_agent_jev_prompt_tokens_total",
                     "wow_agent_jev_completion_tokens_total", "wow_agent_jev_cost_usd_total",
                     "wow_agent_jev_cost_usd_24h", "wow_agent_jev_confidence",
                     "wow_agent_jev_low_confidence_total", "wow_agent_jev_latency_ms"):
            self.assertIn(f"# TYPE {name}", text)

    def test_non_jev_cycle_renders_zero_jev_series(self):
        rec = {"agent": "IdleJev", "ts": 1, "brain": "llm", "valid": True,
               "result": {"ok": True}, "tool_call": {"name": "face", "args": {}}}
        text = render_prometheus_text(derive_metrics([rec]))
        self.assertIn('wow_agent_jev_calls_total{agent="IdleJev"} 0', text)
        self.assertIn('wow_agent_jev_cost_usd_24h{agent="IdleJev"} 0.0', text)
        self.assertIn('wow_agent_jev_errors_total{agent="IdleJev",status="http_5xx"} 0', text)


if __name__ == "__main__":
    unittest.main()


class SwallowedTest(unittest.TestCase):
    def setUp(self):
        from agent import metrics
        self.m = metrics
        metrics.SWALLOWED.clear()

    def test_counts_logs_with_traceback_and_throttles(self):
        import logging
        log = logging.getLogger("t.swallowed")
        with self.assertLogs("t.swallowed", level="ERROR") as cm:
            for _ in range(3):
                try:
                    raise ValueError("boom")
                except Exception:
                    self.m.swallowed("unit.site", log, "ctx 7")
        self.assertEqual(self.m.SWALLOWED, {"unit.site": 3})
        self.assertEqual(len(cm.records), 1)        # same (where, type): throttled
        self.assertIsNotNone(cm.records[0].exc_info)
        self.assertIn("unit.site (ctx 7): ValueError: boom", cm.output[0])
        text = self.m.render_swallowed("A")
        self.assertIn('wow_agent_swallowed_errors_total{agent="A",where="unit.site"} 3', text)


class DecisionsByPromptTest(unittest.TestCase):
    """#476: wow_agent_decisions_by_prompt_total{prompt_id}, bounded to 8 ids + other."""

    def setUp(self):
        from agent import metrics
        self.m = metrics
        metrics.DECISIONS_BY_PROMPT.clear()
        self.addCleanup(metrics.DECISIONS_BY_PROMPT.clear)

    def test_counts_per_prompt_id_and_renders(self):
        for pid in ("system@1a2b3c4d", "system@1a2b3c4d", "system@5e6f7a8b"):
            self.m.count_decision(pid)
        text = self.m.render_decisions_by_prompt("A")
        self.assertIn("# TYPE wow_agent_decisions_by_prompt_total counter", text)
        self.assertIn('wow_agent_decisions_by_prompt_total{agent="A",prompt_id="system@1a2b3c4d"} 2', text)
        self.assertIn('wow_agent_decisions_by_prompt_total{agent="A",prompt_id="system@5e6f7a8b"} 1', text)

    def test_ten_distinct_ids_make_eight_labels_and_other_is_two(self):
        for i in range(10):
            self.m.count_decision(f"system@{i:08x}")
        counts = dict(self.m.DECISIONS_BY_PROMPT)
        self.assertEqual(len(counts), 9)
        self.assertEqual(counts["other"], 2)
        self.assertEqual(sum(1 for k in counts if k != "other"), 8)
        self.m.count_decision("system@00000000")        # already-known id keeps its own label
        self.assertEqual(self.m.DECISIONS_BY_PROMPT["system@00000000"], 2)
        self.assertEqual(self.m.DECISIONS_BY_PROMPT["other"], 2)

    def test_missing_or_empty_id_is_none(self):
        self.m.count_decision(None)
        self.m.count_decision("")
        self.assertEqual(dict(self.m.DECISIONS_BY_PROMPT), {"none": 2})

    def test_none_does_not_use_up_one_of_the_eight(self):
        self.m.count_decision(None)
        for i in range(8):
            self.m.count_decision(f"p{i}")
        self.assertNotIn("other", self.m.DECISIONS_BY_PROMPT)

    def test_threads_cannot_exceed_the_bound(self):
        import threading
        ts = [threading.Thread(target=lambda i=i: [self.m.count_decision(f"p{i}-{j}") for j in range(20)])
              for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(self.m.DECISIONS_BY_PROMPT), 9)
        self.assertEqual(sum(self.m.DECISIONS_BY_PROMPT.values()), 160)

    def test_textfile_derives_same_series_from_audit_records(self):
        recs = [{"agent": "A", "prompt_id": f"system@{i:08x}"} for i in range(10)]
        recs += [{"agent": "A", "prompt_id": None}, {"agent": "B"}]
        by_agent = self.m.derive_metrics(recs)
        self.assertEqual(by_agent["A"].decisions_by_prompt["other"], 2)
        self.assertEqual(by_agent["A"].decisions_by_prompt["none"], 1)
        self.assertEqual(by_agent["B"].decisions_by_prompt, {"none": 1})
        text = self.m.render_prometheus_text(by_agent)
        self.assertIn('wow_agent_decisions_by_prompt_total{agent="A",prompt_id="other"} 2', text)
        self.assertIn('wow_agent_decisions_by_prompt_total{agent="B",prompt_id="none"} 1', text)
