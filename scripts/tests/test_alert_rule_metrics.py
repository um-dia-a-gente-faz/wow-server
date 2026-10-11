"""Alert drift guard (#361): every metric a Prometheus alert rule queries is emitted by a producer.
Rule structure (#393): every rule has its fields, its runbook anchor has a section, names are unique.

Self-contained on purpose (test_dashboard_metrics.py guards dashboards). Rules are read as text.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
RULES = ROOT / "monitoring" / "prometheus-agent-alerts.yml"
RUNBOOK = ROOT / "docs" / "RUNBOOK-ALERTS.md"
RUNBOOK_PATH = "docs/RUNBOOK-ALERTS.md"  # the path a rule's runbook annotation must name
REQUIRED = ("expr", "for", "severity", "summary", "description", "runbook")
SEVERITIES = ("warning", "critical")
CAMEL = re.compile(r"[A-Z][a-z0-9]*(?:[A-Z][a-z0-9]*)*")
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


def unquote(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def parse_rules(text: str) -> list[dict]:
    """Each rule as {"alert", "expr", "for", "severity", "summary", "description", "runbook"}, read
    line by line like rule_exprs(). A field absent from the text is absent from the dict."""
    rules: list[dict] = []
    for line in text.splitlines():
        m = re.match(r"\s*-\s*alert:\s*(\S+)", line)
        if m:
            rules.append({"alert": m.group(1)})
            continue
        if not rules:
            continue
        m = re.match(r"\s*(expr|for|summary|description|runbook):\s*(.+?)\s*$", line)
        if m:
            rules[-1][m.group(1)] = unquote(m.group(2))
        m = re.match(r"\s*labels:.*severity:\s*(\w+)", line)
        if m:
            rules[-1]["severity"] = m.group(1)
    return rules


def rule_problems(text: str) -> list[str]:
    """Each rule missing a required field, or with a severity that is not warning or critical."""
    out = []
    for rule in parse_rules(text):
        for field in REQUIRED:
            if not rule.get(field):
                out.append(f"{rule['alert']}: no {field}")
        if rule.get("severity") and rule["severity"] not in SEVERITIES:
            out.append(f"{rule['alert']}: severity {rule['severity']} is not warning or critical")
    return out


def name_problems(text: str) -> list[str]:
    names = [rule["alert"] for rule in parse_rules(text)]
    out = [f"{n}: not CamelCase" for n in names if not CAMEL.fullmatch(n)]
    out += [f"{n}: name used by more than one rule" for n in sorted(set(names)) if names.count(n) > 1]
    return out


def github_anchor(heading: str) -> str:
    """GitHub's anchor for a heading: lowercased, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def runbook_problems(rules_text: str, runbook_text: str) -> list[str]:
    """Each rule's runbook annotation must name RUNBOOK_PATH#<lowercased name> and that anchor must
    be a `## ` section; every `## ` section must name a rule (no stale section)."""
    rules = parse_rules(rules_text)
    names = {rule["alert"] for rule in rules}
    sections = re.findall(r"^## (.+?)\s*$", runbook_text, re.MULTILINE)
    anchors = {github_anchor(s) for s in sections}
    out = []
    for rule in rules:
        ref = rule.get("runbook")
        if not ref:
            continue  # rule_problems reports the missing field
        path, _, anchor = ref.partition("#")
        if path != RUNBOOK_PATH:
            out.append(f"{rule['alert']}: runbook {ref} is not in {RUNBOOK_PATH}")
        elif anchor != rule["alert"].lower():
            out.append(f"{rule['alert']}: runbook anchor #{anchor} is not the lowercased alert name")
        elif anchor not in anchors:
            out.append(f"{rule['alert']}: runbook anchor #{anchor} has no ## section in {RUNBOOK_PATH}")
    out += [f"runbook section ## {s} names no rule" for s in sections if s not in names]
    return out


BLOCK_INDICATOR = re.compile(r"[|>][+-]?\d*[+-]?")  # a block scalar header, as the whole value


def yaml_problems(text: str) -> list[str]:
    """The subset the rules file uses, checked by line (no YAML library in CI): no tabs, indents in
    steps of two, quoted scalars and flow maps closed, and each expr: holds its whole query on its
    line (no empty value, no block scalar header, no continuation line indented under it)."""
    out = []
    lines = text.splitlines()
    content = [(n, line) for n, line in enumerate(lines, 1) if line.strip() and not line.lstrip().startswith("#")]
    for i, (n, line) in enumerate(content):
        if "\t" in line:
            out.append(f"line {n}: tab")
        if (len(line) - len(line.lstrip(" "))) % 2:
            out.append(f"line {n}: indent is not a multiple of two")
        if line.count('"') % 2:
            out.append(f"line {n}: unclosed quote")
        if line.count("{") != line.count("}"):
            out.append(f"line {n}: unbalanced braces")
        m = re.match(r"(\s*)expr:\s*(.*?)\s*$", line)
        if not m:
            continue
        value = m.group(2)
        if not value:
            out.append(f"line {n}: expr is empty")
        elif BLOCK_INDICATOR.fullmatch(value):
            out.append(f"line {n}: expr is a block scalar")
        if i + 1 < len(content):
            next_line = content[i + 1][1]
            if len(next_line) - len(next_line.lstrip(" ")) > len(m.group(1)):
                out.append(f"line {n}: expr continues on the next line")
    return out


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


class AlertRuleStructure(unittest.TestCase):
    def test_every_rule_has_required_fields_and_a_valid_severity(self):
        self.assertEqual(rule_problems(RULES.read_text()), [])

    def test_every_runbook_anchor_has_a_section_and_every_section_names_a_rule(self):
        self.assertEqual(runbook_problems(RULES.read_text(), RUNBOOK.read_text()), [])

    def test_alert_names_are_unique_and_camelcase(self):
        self.assertEqual(name_problems(RULES.read_text()), [])

    def test_rules_file_parses_as_the_line_subset(self):
        self.assertEqual(yaml_problems(RULES.read_text()), [])


RULE_TEMPLATE = """groups:
  - name: synthetic
    rules:
      - alert: {name}
        expr: {expr}
        for: 5m
        labels: {{severity: warning}}
        annotations:
          summary: "s"
          description: "d"
          runbook: "{runbook}"
"""


def synthetic(name="AgentX", runbook="docs/RUNBOOK-ALERTS.md#agentx", without=None, expr="up == 0") -> str:
    lines = RULE_TEMPLATE.format(name=name, runbook=runbook, expr=expr).splitlines()
    return "\n".join(line for line in lines if not without or not line.strip().startswith(f"{without}:")) + "\n"


class AlertRuleCheckerSelfTest(unittest.TestCase):
    def test_rule_without_for_is_reported(self):
        self.assertEqual(rule_problems(synthetic(without="for")), ["AgentX: no for"])

    def test_rule_without_runbook_is_reported(self):
        self.assertEqual(rule_problems(synthetic(without="runbook")), ["AgentX: no runbook"])

    def test_runbook_anchor_without_a_section_is_reported(self):
        self.assertEqual(runbook_problems(synthetic(), "# Runbook\n\n## AgentY\n"), [
            f"AgentX: runbook anchor #agentx has no ## section in {RUNBOOK_PATH}",
            "runbook section ## AgentY names no rule",
        ])

    def test_duplicate_name_is_reported(self):
        self.assertEqual(name_problems(synthetic() + synthetic()), ["AgentX: name used by more than one rule"])

    def test_folded_block_scalar_expr_is_reported(self):
        # reviewer mutation 1: `expr: >-` with the query on the next line
        problems = yaml_problems(synthetic(expr=">-\n          up == 0"))
        self.assertIn("line 5: expr is a block scalar", problems)

    def test_plain_continuation_line_is_reported(self):
        # reviewer mutation 2: a plain multi-line scalar
        problems = yaml_problems(synthetic(expr="up == 0\n          or up == 1"))
        self.assertIn("line 5: expr continues on the next line", problems)

    def test_continuation_hiding_a_misspelled_metric_is_reported(self):
        # reviewer mutation 3: the drift guard reads only the first line, so this name escapes it
        expr = "sum by (agent) (rate(wow_agent_router_truncated_total[10m])) > 0\n          or wow_agent_router_truncatedd_total > 0"
        problems = yaml_problems(synthetic(expr=expr))
        self.assertIn("line 5: expr continues on the next line", problems)

    def test_one_line_expr_with_a_comparison_is_not_reported(self):
        self.assertEqual(yaml_problems(synthetic(expr="up > 0")), [])


if __name__ == "__main__":
    unittest.main()
