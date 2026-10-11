"""Alert drift guard (#361): every metric a Prometheus alert rule queries is emitted by a producer.

Self-contained on purpose (test_dashboard_metrics.py guards dashboards). Rules are read as text.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
RULES = ROOT / "monitoring" / "prometheus-agent-alerts.yml"
PRODUCERS = [
    ROOT / "agent" / "metrics.py",
    ROOT / "exporters" / "wow-exporter" / "exporter.py",
    ROOT / "monitoring" / "agent_metrics_textfile.py",
    ROOT / "tools" / "agent-runner" / "runner.py",
]
NAME = re.compile(r"\b(?:wow|jev)_[a-z0-9_]+\b")
SUFFIX = re.compile(r"_(?:bucket|sum|count)$")
SELECTOR = re.compile(r"\{[^{}]*\}")
GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)")


def rule_exprs() -> dict[str, str]:
    """{alert name: expr} for the single-line `expr:` of each rule."""
    out, name = {}, None
    for line in RULES.read_text().splitlines():
        m = re.match(r"\s*-\s*alert:\s*(\S+)", line)
        if m:
            name = m.group(1)
            continue
        m = re.match(r"\s*expr:\s*(.+?)\s*$", line)
        if m and name:
            out[name] = m.group(1)
    return out


def produced() -> set[str]:
    return {n for p in PRODUCERS for n in NAME.findall(p.read_text())}


class AlertRuleMetrics(unittest.TestCase):
    def test_every_metric_in_every_rule_is_produced(self):
        have = produced()
        for alert, expr in rule_exprs().items():
            for name in NAME.findall(GROUPING.sub(" ", SELECTOR.sub(" ", expr))):
                self.assertTrue(name in have or SUFFIX.sub("", name) in have,
                                f"{alert} queries {name}, which no producer emits")

    def test_backup_rule_is_parsed(self):
        self.assertIn("wow_backup_newest_timestamp_seconds", rule_exprs()["BackupStale"])

    def test_backup_stale_threshold_is_26_hours(self):
        self.assertIn("> 26 * 3600", rule_exprs()["BackupStale"])


if __name__ == "__main__":
    unittest.main()
