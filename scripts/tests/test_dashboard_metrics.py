"""Dashboard drift guard (#280): every metric a Grafana panel queries is emitted by a producer.

Producers are read as text, never imported: the names they emit are literals in their source.
A renamed metric otherwise fails as an empty panel, never in CI.
"""
import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PRODUCERS = [
    ROOT / "agent" / "metrics.py",
    ROOT / "exporters" / "wow-exporter" / "exporter.py",
    ROOT / "monitoring" / "agent_metrics_textfile.py",
    ROOT / "tools" / "agent-runner" / "runner.py",
]
DASHBOARDS = sorted((ROOT / "monitoring").glob("*.json")) + sorted(
    (ROOT / "grafana" / "provisioning").glob("**/*.json")
)
NAME = re.compile(r"\b(?:wow|jev)_[a-z0-9_]+\b")
SUFFIX = re.compile(r"_(?:bucket|sum|count)$")  # histogram and summary series
SELECTOR = re.compile(r"\{[^{}]*\}")  # {job="wow_exporter"}: label names and values
GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)")
# Metric names a dashboard queries that no producer emits yet: {name: why}. A known gap, not a
# waiver; test_known_unproduced_entries_are_still_needed fails once the entry goes stale.
KNOWN_UNPRODUCED: dict[str, str] = {}

Ref = tuple[str, str, str]  # (metric name, dashboard file, panel title)


def producer_names(paths) -> set[str]:
    return {name for path in paths for name in NAME.findall(pathlib.Path(path).read_text())}


def referenced_names(expr: str) -> list[str]:
    """Metric names in a PromQL expression, ignoring label matchers and grouping labels."""
    return NAME.findall(GROUPING.sub(" ", SELECTOR.sub(" ", expr)))


def resolves(name: str, produced: set[str]) -> bool:
    if name in produced:
        return True
    stem = SUFFIX.sub("", name)
    return stem != name and stem in produced


def _target_exprs(node, title):
    """(panel title, expr) for every target under node; a panel title covers its nested targets."""
    if isinstance(node, list):
        for item in node:
            yield from _target_exprs(item, title)
    elif isinstance(node, dict):
        title = node.get("title", title)
        targets = node.get("targets")
        if isinstance(targets, list):
            for target in targets:
                if isinstance(target, dict) and isinstance(target.get("expr"), str):
                    yield title, target["expr"]
        for key, value in node.items():
            if key != "targets":
                yield from _target_exprs(value, title)


def _template_exprs(doc):
    for var in doc.get("templating", {}).get("list", []):
        title = f"template variable ${var.get('name')}"
        query = var.get("query")
        if isinstance(query, dict):
            query = query.get("query")
        for text in (query, var.get("definition")):
            if isinstance(text, str):
                yield title, text


def dashboard_exprs(doc) -> list[tuple[str, str]]:
    return [*_target_exprs(doc, "(untitled)"), *_template_exprs(doc)]


def dashboard_refs(file: str, doc) -> list[Ref]:
    return [(name, file, title) for title, expr in dashboard_exprs(doc) for name in referenced_names(expr)]


def unknown_refs(refs, produced: set[str], known=None) -> list[Ref]:
    known = KNOWN_UNPRODUCED if known is None else known
    return [ref for ref in refs if ref[0] not in known and not resolves(ref[0], produced)]


class DashboardMetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.produced = producer_names(PRODUCERS)
        cls.refs = []
        cls.expr_counts = {}  # dashboard file -> expressions read from it
        for path in DASHBOARDS:
            file, doc = path.relative_to(ROOT).as_posix(), json.loads(path.read_text())
            cls.refs += dashboard_refs(file, doc)
            cls.expr_counts[file] = len(dashboard_exprs(doc))

    def test_every_dashboard_metric_is_produced(self):
        bad = unknown_refs(self.refs, self.produced)
        self.assertEqual(
            [], [f"{file}: panel {title!r} queries {name}" for name, file, title in bad],
            "dashboard queries a metric no producer emits (rename drift, or add a KNOWN_UNPRODUCED entry)",
        )

    def test_known_unproduced_entries_are_still_needed(self):
        referenced = {name for name, _, _ in self.refs}
        for name in KNOWN_UNPRODUCED:
            self.assertIn(name, referenced, f"{name}: no dashboard references it any more, drop the entry")
            self.assertFalse(resolves(name, self.produced), f"{name}: now produced, drop the entry")

    def test_scan_is_not_vacuous(self):
        self.assertGreaterEqual(len(DASHBOARDS), 5, "too few dashboards read")
        # server-host queries node_*/container_* (not in this test's namespace), so count expressions.
        empty = [file for file, count in self.expr_counts.items() if count == 0]
        self.assertEqual(empty, [], "dashboard with no targets read: the walk missed its panels")
        self.assertGreaterEqual(len({name for name, _, _ in self.refs}), 20, "too few dashboard names found")
        self.assertIn("wow_players_online", self.produced, "producer extraction found nothing")


class ExtractionTest(unittest.TestCase):
    PRODUCED = {"wow_players_online", "wow_agent_jev_latency_ms"}

    @staticmethod
    def panel(title, expr):
        return {"title": title, "targets": [{"refId": "A", "expr": expr}]}

    def test_renamed_metric_is_reported_with_file_and_panel(self):
        doc = {"panels": [self.panel("Players online over time", "wow_players_onlinee")]}
        bad = unknown_refs(dashboard_refs("grafana-dashboard-x.json", doc), self.PRODUCED, known={})
        self.assertEqual(bad, [("wow_players_onlinee", "grafana-dashboard-x.json", "Players online over time")])

    def test_histogram_bucket_suffix_resolves_to_producer_name(self):
        expr = "histogram_quantile(0.95, sum by (le) (rate(wow_agent_jev_latency_ms_bucket[5m])))"
        doc = {"panels": [self.panel("Jev latency", expr)]}
        self.assertEqual(unknown_refs(dashboard_refs("x.json", doc), self.PRODUCED, known={}), [])

    def test_label_matcher_and_grouping_label_are_not_metrics(self):
        exprs = ['wow_players_online{job="wow_exporter"}', "sum by (wow_zone) (wow_players_online)"]
        doc = {"panels": [self.panel("Players", e) for e in exprs]}
        self.assertEqual(unknown_refs(dashboard_refs("x.json", doc), self.PRODUCED, known={}), [])

    def test_template_variable_query_is_scanned(self):
        doc = {"templating": {"list": [{"name": "character", "type": "query",
                                        "query": {"query": "label_values(wow_player_positionx, character)"}}]}}
        bad = unknown_refs(dashboard_refs("x.json", doc), self.PRODUCED, known={})
        self.assertEqual([name for name, _, _ in bad], ["wow_player_positionx"])

    def test_known_unproduced_name_is_subtracted(self):
        refs = [("wow_gone", "x.json", "Old panel")]
        self.assertEqual(unknown_refs(refs, self.PRODUCED, known={"wow_gone": "tracked elsewhere"}), [])


if __name__ == "__main__":
    unittest.main()
