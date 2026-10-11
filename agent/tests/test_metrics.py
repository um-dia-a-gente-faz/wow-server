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


class RouterCountersRenderTest(unittest.TestCase):
    def setUp(self):
        from agent import metrics, opcodes
        self.m, self.op = metrics, opcodes
        metrics.ROUTER_COUNTS.clear()
        self.addCleanup(metrics.ROUTER_COUNTS.clear)

    def test_count_router_event_and_render_with_names(self):
        for kind in ("packets", "unhandled", "truncated", "handler_errors"):
            self.m.count_router_event(kind, self.op.SMSG_LOGOUT_COMPLETE)
        self.m.count_router_event("packets", 0xFFFE)   # no name: 0x%03X
        text = self.m.render_router("A")
        for kind in ("packets", "unhandled", "truncated", "handler_errors"):
            self.assertIn(f'wow_agent_router_{kind}_total{{agent="A",opcode="SMSG_LOGOUT_COMPLETE"}} 1', text)
        self.assertIn('wow_agent_router_packets_total{agent="A",opcode="0xFFFE"} 1', text)
        self.assertIn("# TYPE wow_agent_router_handler_errors_total counter", text)

    def test_router_names_are_literals_in_the_producer_source(self):
        # the #280 dashboards-vs-producers test scans agent/metrics.py as text with this regex
        import re
        with open(self.m.__file__, encoding="utf-8") as f:
            found = set(re.findall(r"\b(?:wow|jev)_[a-z0-9_]+\b", f.read()))
        for kind in ("packets", "unhandled", "truncated", "handler_errors"):
            self.assertIn(f"wow_agent_router_{kind}_total", found)

    def test_names_present_even_with_no_events(self):
        text = self.m.render_router("A")
        for kind in ("packets", "unhandled", "truncated", "handler_errors"):
            self.assertIn(f"# TYPE wow_agent_router_{kind}_total counter", text)


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
    """#476: wow_agent_decisions_by_prompt_total{agent,prompt_id}, derived from the audit
    records by the textfile exporter only; 8 newest ids keep a label, older ones are `other`."""

    def render(self, recs):
        text = render_prometheus_text(derive_metrics(recs))
        return [ln for ln in text.splitlines() if ln.startswith("wow_agent_decisions_by_prompt_total{")]

    def test_counts_per_prompt_id(self):
        recs = [{"agent": "A", "ts": 1.0, "prompt_id": "system@1a2b3c4d"},
                {"agent": "A", "ts": 2.0, "prompt_id": "system@1a2b3c4d"},
                {"agent": "A", "ts": 3.0, "prompt_id": "system@5e6f7a8b"}]
        self.assertEqual(self.render(recs), [
            'wow_agent_decisions_by_prompt_total{agent="A",prompt_id="system@1a2b3c4d"} 2',
            'wow_agent_decisions_by_prompt_total{agent="A",prompt_id="system@5e6f7a8b"} 1'])

    def test_ten_ids_over_increasing_timestamps_keep_the_eight_newest(self):
        recs = [{"agent": "A", "ts": float(i), "prompt_id": f"p{i}"} for i in range(10)]
        counts = derive_metrics(recs)["A"].decisions_by_prompt
        self.assertEqual(counts["other"], 2)
        self.assertEqual({k for k in counts if k != "other"}, {f"p{i}" for i in range(2, 10)})
        # shuffled file order: the bound follows the timestamps, not the order of the lines
        counts = derive_metrics(list(reversed(recs)))["A"].decisions_by_prompt
        self.assertEqual({k for k in counts if k != "other"}, {f"p{i}" for i in range(2, 10)})

    def test_older_id_seen_again_recently_stays_labelled(self):
        recs = [{"agent": "A", "ts": float(i), "prompt_id": f"p{i}"} for i in range(9)]
        recs.append({"agent": "A", "ts": 99.0, "prompt_id": "p0"})
        counts = derive_metrics(recs)["A"].decisions_by_prompt
        self.assertEqual(counts["p0"], 2)
        self.assertEqual(counts["other"], 1)      # p1 is now the oldest

    def test_bound_is_per_agent(self):
        recs = [{"agent": a, "ts": float(i), "prompt_id": f"p{i}"} for a in "AB" for i in range(10)]
        for m in derive_metrics(recs).values():
            self.assertEqual(m.decisions_by_prompt["other"], 2)

    def test_missing_empty_and_null_id_is_none_and_not_counted_against_the_eight(self):
        recs = [{"agent": "A", "ts": 0.0}, {"agent": "A", "prompt_id": ""}, {"agent": "A", "prompt_id": None}]
        recs += [{"agent": "A", "ts": float(i), "prompt_id": f"p{i}"} for i in range(8)]
        counts = derive_metrics(recs)["A"].decisions_by_prompt
        self.assertEqual(counts["none"], 3)
        self.assertNotIn("other", counts)

    def test_non_string_ids_render_as_none(self):
        recs = [{"agent": "A", "prompt_id": ["a"]}, {"agent": "A", "prompt_id": {"a": 1}},
                {"agent": "A", "prompt_id": 7}, {"agent": "A", "prompt_id": "system@1"}]
        self.assertEqual(self.render(recs), [
            'wow_agent_decisions_by_prompt_total{agent="A",prompt_id="none"} 3',
            'wow_agent_decisions_by_prompt_total{agent="A",prompt_id="system@1"} 1'])

    def test_quote_backslash_newline_are_escaped_and_samples_stay_on_one_line(self):
        recs = [{"agent": "A", "prompt_id": 'sys"te\\m\n@1'}]
        self.assertEqual(self.render(recs), [
            'wow_agent_decisions_by_prompt_total{agent="A",prompt_id="sys\\"te\\\\m\\n@1"} 1'])


class ReaderSkipsBadLinesTest(unittest.TestCase):
    """#513: iter_records skips non-object JSON and undecodable bytes without raising, and
    reports each skipped line through on_bad."""

    def write(self, data: bytes) -> str:
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        path = os.path.join(self._dir.name, "A.jsonl")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_skips_non_object_truncated_and_non_utf8_lines(self):
        lines = [b'{"agent": "A", "ts": 1.0}', b"[1, 2]", b"7", b'"s"', b"null",
                 b'{"agent": "A", "ts"', b"\xff\xfe\x00", b'{"agent": "A", "prompt_id": "\xff"}',
                 b'{"agent": "A", "ts": 2.0}']
        bad = []
        got = list(iter_records(self.write(b"\n".join(lines) + b"\n"), on_bad=lambda: bad.append(1)))
        self.assertEqual([r["ts"] for r in got], [1.0, 2.0])
        self.assertEqual(len(bad), 7)  # list, number, string, null, truncated, two non-UTF-8 lines

    def test_a_deeply_nested_line_is_skipped(self):
        bad = []
        self.assertEqual(list(iter_records(self.write(b"[" * 100000 + b"\n"), on_bad=lambda: bad.append(1))), [])
        self.assertEqual(len(bad), 1)


class LabelValueTest(unittest.TestCase):
    def test_escapes_backslash_quote_newline_and_replaces_lone_surrogates(self):
        from agent.metrics import _label_value
        self.assertEqual(_label_value('a\\b"c\nd'), 'a\\\\b\\"c\\nd')
        self.assertEqual(_label_value("a\ud800b"), "a?b")

    def test_every_rendered_label_value_goes_through_the_escape(self):
        recs = [{"agent": "A", "ts": 1.0, "tool_call": {"name": 'x"\\\n\ud800', "args": {}},
                 "valid": True, "result": {"ok": True}, "prompt_id": 'p"\\\n\ud800'}]
        text = render_prometheus_text(derive_metrics(recs))
        text.encode("utf-8")  # never raises
        self.assertIn('action="x\\"\\\\\\n?"', text)
        self.assertIn('prompt_id="p\\"\\\\\\n?"', text)


class PromptCollisionTest(unittest.TestCase):
    """#513: a prompt_id literally `other` or `none` is summed with the fold, never overwritten."""

    @staticmethod
    def counts(recs):
        return derive_metrics(recs)["A"].decisions_by_prompt

    def test_literal_none_and_other_ids_keep_their_counts(self):
        recs = [{"agent": "A", "ts": float(i), "prompt_id": "other"} for i in range(3)]
        recs += [{"agent": "A", "ts": 10.0 + i, "prompt_id": "none"} for i in range(2)]
        recs += [{"agent": "A", "ts": 20.0}, {"agent": "A", "ts": 21.0, "prompt_id": "p1"}]
        counts = self.counts(recs)
        self.assertEqual(counts, {"other": 3, "none": 3, "p1": 1})
        self.assertEqual(sum(counts.values()), len(recs))

    def test_literal_other_that_is_kept_is_summed_with_the_fold(self):
        recs = [{"agent": "A", "ts": 100.0 + i, "prompt_id": "other"} for i in range(2)]
        recs += [{"agent": "A", "ts": float(i), "prompt_id": f"p{i}"} for i in range(9)]
        counts = self.counts(recs)
        # 11 ids: p0 and p1 are the two oldest and fold into other, with the two literal "other" records
        self.assertEqual(counts["other"], 2 + 1 + 1)
        self.assertEqual(sum(counts.values()), len(recs))


class OtherSumTest(unittest.TestCase):
    """#513: with more than 8 ids, `other` equals the sum of the counts of the dropped ids,
    even though the lines are out of chronological order."""

    def test_other_is_the_sum_of_the_dropped_ids_counts(self):
        import random
        ids = [f"p{i}" for i in range(12)]
        counts = {pid: i + 1 for i, pid in enumerate(ids)}            # 1..12 records each
        ts = {pid: float((i * 7) % 12) for i, pid in enumerate(ids)}  # distinct, not in id order
        recs = [{"agent": "A", "ts": ts[pid], "prompt_id": pid} for pid in ids for _ in range(counts[pid])]
        random.Random(513).shuffle(recs)                              # out of chronological order
        dropped = sorted(ids, key=lambda pid: ts[pid])[:4]
        got = derive_metrics(recs)["A"].decisions_by_prompt
        self.assertEqual(got["other"], sum(counts[pid] for pid in dropped))
        self.assertEqual({k for k in got if k != "other"}, set(ids) - set(dropped))
        self.assertEqual(sum(got.values()), len(recs))
