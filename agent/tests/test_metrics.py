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


if __name__ == "__main__":
    unittest.main()
