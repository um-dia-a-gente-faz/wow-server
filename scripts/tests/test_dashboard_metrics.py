"""Dashboard drift guard (#280): every metric a Grafana panel queries is emitted by a producer.

Producers are read as text, never imported: the names they emit are literals in their source.
A renamed metric otherwise fails as an empty panel, never in CI.
"""
import ast
import json
import pathlib
import re
import tempfile
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

Ref = tuple[str, str, str]  # (metric name, dashboard file, where: "panel 'T'", "annotation 'N'", ...)
_SCOPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _is_docstring(node) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _fstring_names(values) -> list[str]:
    """Whole names in the literal pieces of an f-string. A match touching an interpolation may
    continue into it (f'wow_agent_{kind}_total' is 'wow_agent_' plus a value), so it is not counted."""
    names = []
    for i, part in enumerate(values):
        if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
            continue
        text = part.value
        left = i > 0 and isinstance(values[i - 1], ast.FormattedValue)
        right = i + 1 < len(values) and isinstance(values[i + 1], ast.FormattedValue)
        for m in NAME.finditer(text):
            if (left and m.start() == 0) or (right and m.end() == len(text)):
                continue
            names.append(m.group())
    return names


def source_names(source: str) -> set[str]:
    """Metric names a producer source emits. Only string literals count: docstrings are skipped and
    comments never reach the AST, so a name mentioned there is not produced."""
    tree = ast.parse(source)
    docstrings = {id(scope.body[0].value) for scope in ast.walk(tree)
                  if isinstance(scope, _SCOPES) and scope.body and _is_docstring(scope.body[0])}
    fstring_parts = {id(value) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
                     for value in node.values}
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            names.update(_fstring_names(node.values))
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings and id(node) not in fstring_parts):
            names.update(NAME.findall(node.value))
    return names


def producer_names(paths) -> set[str]:
    names: set[str] = set()
    for path in paths:
        names |= source_names(pathlib.Path(path).read_text())
    return names


def referenced_names(expr: str) -> list[str]:
    """Metric names in a PromQL expression, ignoring label matchers and grouping labels."""
    return NAME.findall(GROUPING.sub(" ", SELECTOR.sub(" ", expr)))


def resolves(name: str, produced: set[str]) -> bool:
    if name in produced:
        return True
    stem = SUFFIX.sub("", name)
    return stem != name and stem in produced


def _target_exprs(node, title):
    """(where, expr) for every target under node; a panel title covers its nested targets."""
    if isinstance(node, list):
        for item in node:
            yield from _target_exprs(item, title)
    elif isinstance(node, dict):
        title = node.get("title", title)
        targets = node.get("targets")
        if isinstance(targets, list):
            for target in targets:
                if isinstance(target, dict) and isinstance(target.get("expr"), str):
                    yield f"panel {title!r}", target["expr"]
        for key, value in node.items():
            if key != "targets":
                yield from _target_exprs(value, title)


def _template_exprs(doc):
    for var in doc.get("templating", {}).get("list", []):
        where = f"template variable ${var.get('name')}"
        query = var.get("query")
        if isinstance(query, dict):
            query = query.get("query")
        for text in (query, var.get("definition")):
            if isinstance(text, str):
                yield where, text


def _annotation_exprs(doc):
    for ann in doc.get("annotations", {}).get("list", []):
        if isinstance(ann.get("expr"), str):
            yield f"annotation {ann.get('name')!r}", ann["expr"]


def dashboard_exprs(doc) -> list[tuple[str, str]]:
    return [*_target_exprs(doc, "(untitled)"), *_template_exprs(doc), *_annotation_exprs(doc)]


def dashboard_refs(file: str, doc) -> list[Ref]:
    return [(name, file, where) for where, expr in dashboard_exprs(doc) for name in referenced_names(expr)]


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
            [], [f"{file}: {where} queries {name}" for name, file, where in bad],
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


JEV_DASHBOARD = ROOT / "monitoring" / "grafana-dashboard-jev-decision-usage.json"


class JevBudgetSkipPanels(unittest.TestCase):
    """#689: a spent hourly budget is a skip. The error panel excludes it; a panel shows it apart."""

    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads(JEV_DASHBOARD.read_text())
        cls.panels = {p.get("title"): p for p in cls.doc["panels"]}

    def test_jev_errors_panel_excludes_budget_exhausted(self):
        exprs = [t["expr"] for t in self.panels["Jev errors / sec"]["targets"]]
        self.assertTrue(exprs)
        for expr in exprs:
            self.assertIn('wow_agent_jev_errors_total{status!="budget_exhausted"}', expr)

    def test_skip_panel_queries_the_errors_counter_for_budget_exhausted(self):
        exprs = [t["expr"] for t in self.panels["Jev cycles skipped: hourly budget spent"]["targets"]]
        self.assertTrue(any('wow_agent_jev_errors_total{status="budget_exhausted"}' in e for e in exprs), exprs)

    def test_budget_skip_panel_is_titled_as_a_skip(self):
        self.assertIn("Jev cycles skipped: hourly budget spent", self.panels)


class SourceNamesTest(unittest.TestCase):
    def test_name_only_in_docstring_is_not_produced(self):
        src = ('"""Module mentions wow_doc_module_total."""\n'
               "class C:\n"
               '    """Class mentions wow_doc_class_total."""\n'
               "def f():\n"
               '    """Counts wow_doc_fn_total."""\n'
               "    return 1\n")
        self.assertEqual(source_names(src), set())

    def test_name_only_in_comment_is_not_produced(self):
        self.assertEqual(source_names("# emitted as wow_comment_only_total\nx = 1\n"), set())

    def test_name_in_fstring_literal_part_is_produced(self):
        src = 'value = 1\nline = f"wow_fstring_metric_total {value}"\n'
        self.assertEqual(source_names(src), {"wow_fstring_metric_total"})

    def test_name_before_label_block_in_fstring_is_produced(self):
        src = "line = f'wow_labelled_total{{agent=\"{a}\"}} {n}'\n"
        self.assertEqual(source_names(src), {"wow_labelled_total"})

    def test_name_split_by_interpolation_is_not_produced(self):
        src = "line = f'wow_agent_{kind}_total'\n"
        self.assertEqual(source_names(src), set())

    def test_plain_string_literal_in_nested_table_is_produced(self):
        src = 'ROUTER = {"packets": ("wow_plain_literal_total", "help text")}\n'
        self.assertEqual(source_names(src), {"wow_plain_literal_total"})

    def test_name_continued_by_a_leading_interpolation_is_not_produced(self):
        self.assertEqual(source_names("line = f'{prefix}wow_tail_total'\n"), set())

    def test_name_only_in_async_function_docstring_is_not_produced(self):
        src = 'async def f():\n    """Counts wow_doc_async_total."""\n    return 1\n'
        self.assertEqual(source_names(src), set())

    def test_producer_files_are_read_as_literals_not_as_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "producer.py"
            path.write_text('"""Mentions wow_doc_only_total."""\n# wow_comment_only_total\nNAME = "wow_real_total"\n')
            self.assertEqual(producer_names([path]), {"wow_real_total"})


class ExtractionTest(unittest.TestCase):
    PRODUCED = {"wow_players_online", "wow_agent_jev_latency_ms"}

    @staticmethod
    def panel(title, expr):
        return {"title": title, "targets": [{"refId": "A", "expr": expr}]}

    def test_renamed_metric_is_reported_with_file_and_panel(self):
        doc = {"panels": [self.panel("Players online over time", "wow_players_onlinee")]}
        bad = unknown_refs(dashboard_refs("grafana-dashboard-x.json", doc), self.PRODUCED, known={})
        self.assertEqual(bad, [("wow_players_onlinee", "grafana-dashboard-x.json", "panel 'Players online over time'")])

    def test_nested_row_panel_is_scanned_with_its_own_title(self):
        row = {"type": "row", "title": "Row", "panels": [self.panel("Nested panel", "wow_nested_onlinee")]}
        bad = unknown_refs(dashboard_refs("x.json", {"panels": [row]}), self.PRODUCED, known={})
        self.assertEqual(bad, [("wow_nested_onlinee", "x.json", "panel 'Nested panel'")])

    def test_histogram_bucket_suffix_resolves_to_producer_name(self):
        expr = "histogram_quantile(0.95, sum by (le) (rate(wow_agent_jev_latency_ms_bucket[5m])))"
        doc = {"panels": [self.panel("Jev latency", expr)]}
        self.assertEqual(unknown_refs(dashboard_refs("x.json", doc), self.PRODUCED, known={}), [])

    def test_sum_and_count_suffixes_resolve_to_producer_name(self):
        exprs = ["rate(wow_agent_jev_latency_ms_sum[5m])", "rate(wow_agent_jev_latency_ms_count[5m])"]
        doc = {"panels": [self.panel("Jev latency", e) for e in exprs]}
        self.assertEqual(unknown_refs(dashboard_refs("x.json", doc), self.PRODUCED, known={}), [])

    def test_sum_suffix_does_not_excuse_an_unknown_base_name(self):
        doc = {"panels": [self.panel("Totals", "rate(wow_gone_sum[5m])")]}
        bad = unknown_refs(dashboard_refs("x.json", doc), self.PRODUCED, known={})
        self.assertEqual([name for name, _, _ in bad], ["wow_gone_sum"])

    def test_label_matcher_and_grouping_label_are_not_metrics(self):
        exprs = ['wow_players_online{job="wow_exporter"}', "sum by (wow_zone) (wow_players_online)"]
        doc = {"panels": [self.panel("Players", e) for e in exprs]}
        self.assertEqual(unknown_refs(dashboard_refs("x.json", doc), self.PRODUCED, known={}), [])

    def test_template_variable_query_is_scanned(self):
        doc = {"templating": {"list": [{"name": "character", "type": "query",
                                        "query": {"query": "label_values(wow_player_positionx, character)"}}]}}
        bad = unknown_refs(dashboard_refs("x.json", doc), self.PRODUCED, known={})
        self.assertEqual([name for name, _, _ in bad], ["wow_player_positionx"])

    def test_template_variable_definition_is_scanned(self):
        doc = {"templating": {"list": [{"name": "zone", "type": "query",
                                        "definition": "label_values(wow_zone_definitionx, zone)"}]}}
        bad = unknown_refs(dashboard_refs("x.json", doc), self.PRODUCED, known={})
        self.assertEqual(bad, [("wow_zone_definitionx", "x.json", "template variable $zone")])

    def test_plain_string_template_query_is_scanned(self):
        doc = {"templating": {"list": [{"name": "zone", "type": "query",
                                        "query": "label_values(wow_zone_plainx, zone)"}]}}
        bad = unknown_refs(dashboard_refs("x.json", doc), self.PRODUCED, known={})
        self.assertEqual([name for name, _, _ in bad], ["wow_zone_plainx"])

    def test_annotation_query_with_unknown_metric_is_reported(self):
        doc = {"annotations": {"list": [{"name": "Deploys", "enable": True,
                                         "expr": "changes(wow_deploy_markerx_total[5m])"}]}}
        bad = unknown_refs(dashboard_refs("grafana-dashboard-x.json", doc), self.PRODUCED, known={})
        self.assertEqual(bad, [("wow_deploy_markerx_total", "grafana-dashboard-x.json", "annotation 'Deploys'")])

    def test_known_unproduced_name_is_subtracted(self):
        refs = [("wow_gone", "x.json", "Old panel")]
        self.assertEqual(unknown_refs(refs, self.PRODUCED, known={"wow_gone": "tracked elsewhere"}), [])


if __name__ == "__main__":
    unittest.main()
