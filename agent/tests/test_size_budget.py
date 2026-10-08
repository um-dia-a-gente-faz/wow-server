"""Size budget fitness test (#383): no function over FUNC_MAX lines and no module over
MODULE_MAX lines in the non-test code of `agent/` and `tools/`.

BASELINE lists the offenders that existed when the gate landed, with their size then.
It is a ratchet (CONTRIBUTING.md, "Size budget"): an entry is a ceiling that may only
go down, and new code never gets an entry. Growth past the budget or past an entry
fails. An entry that is now too high, or whose function or file is gone, is reported as
stale but does not fail, so a PR that splits or shrinks an offender cannot turn `main`
red; lower or delete the entry in that PR.

Sizes are read with `ast` (a function is `def` line to last line, decorators excluded;
a module is its line count). Keys are `path` for a module and `path::Class.func` for a
function.
"""
import ast
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TREES = ("agent", "tools")
FUNC_MAX = 80
MODULE_MAX = 600

BASELINE = {
    # modules
    "agent/perception.py": 1151,
    "agent/tools/probe.py": 769,
    "tools/agent-runner/runner.py": 711,
    "tools/wowmap/activity.py": 639,
    # functions
    "agent/__main__.py::main": 93,
    "agent/auth.py::auth_logon": 85,
    "agent/candidates.py::generate": 176,
    "agent/handlers/chat.py::handle_messagechat": 84,
    "agent/loot.py::parse_item_query_response": 131,
    "agent/metrics.py::derive_metrics": 101,
    "agent/metrics.py::render_prometheus_text": 87,
    "agent/movement.py::_simulate": 125,
    "agent/perception.py::WorldState.snapshot": 91,
    "agent/think.py::think_and_act": 166,
    "agent/tools/ab.py::run": 101,
    "agent/update_object.py::parse_monster_move": 88,
    "tools/dbc/names.py::GameNames.__init__": 147,
    "tools/dbc/spelltext.py::render": 82,
    "tools/wowmap/activity.py::diff_snapshots": 87,
    "tools/wowmap/item_tooltip.py::tooltip": 111,
}


def _functions(node, prefix=""):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield prefix + child.name, (child.end_lineno or child.lineno) - child.lineno + 1
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _functions(child, prefix + child.name + ".")
        else:
            yield from _functions(child, prefix)


def sizes(root: pathlib.Path) -> dict[str, int]:
    """key -> lines, for every non-test module and function under TREES of `root`."""
    out: dict[str, int] = {}
    for tree in TREES:
        for path in sorted((root / tree).rglob("*.py")):
            rel = path.relative_to(root)
            if "tests" in rel.parts or "__pycache__" in rel.parts:
                continue
            src = path.read_text()
            out[rel.as_posix()] = len(src.splitlines())
            for name, lines in _functions(ast.parse(src, str(path))):
                key = f"{rel.as_posix()}::{name}"
                out[key] = max(lines, out.get(key, 0))   # same name defined twice: the longer
    return out


def _limit(key: str) -> int:
    return FUNC_MAX if "::" in key else MODULE_MAX


def check(found: dict[str, int], baseline: dict[str, int]) -> tuple[list[str], list[str]]:
    """(over, stale): what broke the budget, and baseline entries that must go down."""
    over = [f"{key} is {n} lines (allowed {baseline.get(key, _limit(key))})"
            for key, n in sorted(found.items()) if n > baseline.get(key, _limit(key))]
    stale = []
    for key, was in sorted(baseline.items()):
        now = found.get(key)
        if now is None:
            stale.append(f"{key} no longer exists: delete its BASELINE entry")
        elif now <= _limit(key):
            stale.append(f"{key} is within budget ({now} lines): delete its BASELINE entry")
        elif now < was:
            stale.append(f"{key} shrank from {was} to {now} lines: lower its BASELINE entry")
    return over, stale


class SizeBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.over, cls.stale = check(sizes(ROOT), BASELINE)

    def test_nothing_grew_past_the_budget(self):
        self.assertEqual(self.over, [], f"over the size budget ({FUNC_MAX} lines per function, "
                         f"{MODULE_MAX} per module); split it, do not add a BASELINE entry")

    def test_baseline_has_no_stale_entries(self):
        # Reported, not failed: see the module docstring. #407 turns this into a failure
        # once the in-flight splits have merged.
        if self.stale:
            self.skipTest("stale BASELINE entries in agent/tests/test_size_budget.py: "
                          + "; ".join(self.stale))


class CheckerTests(unittest.TestCase):
    """The checker itself, on a synthetic tree."""

    def _sizes(self, files: dict[str, str]) -> dict[str, int]:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name, src in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(src)
            return sizes(root)

    @staticmethod
    def _func(lines: int, name: str = "f", indent: str = "") -> str:
        return f"{indent}def {name}():\n" + f"{indent}    pass\n" * (lines - 1)

    def test_a_function_at_the_budget_passes_and_one_line_more_fails(self):
        found = self._sizes({"agent/ok.py": self._func(FUNC_MAX),
                             "tools/x/new.py": self._func(FUNC_MAX + 1)})
        over, stale = check(found, {})
        self.assertEqual(over, [f"tools/x/new.py::f is {FUNC_MAX + 1} lines (allowed {FUNC_MAX})"])
        self.assertEqual(stale, [])

    def test_methods_and_nested_functions_are_measured(self):
        src = "class C:\n" + self._func(FUNC_MAX + 1, "m", "    ") \
            + "def outer():\n" + self._func(FUNC_MAX + 1, "inner", "    ")
        over, _ = check(self._sizes({"agent/a.py": src}), {})
        self.assertEqual([o.split(" ")[0] for o in over],
                         ["agent/a.py::C.m", "agent/a.py::outer", "agent/a.py::outer.inner"])

    def test_a_module_over_the_budget_fails(self):
        found = self._sizes({"agent/ok.py": "x = 1\n" * MODULE_MAX,
                             "agent/big.py": "x = 1\n" * (MODULE_MAX + 1)})
        self.assertEqual(check(found, {})[0],
                         [f"agent/big.py is {MODULE_MAX + 1} lines (allowed {MODULE_MAX})"])

    def test_tests_are_not_measured(self):
        found = self._sizes({"agent/tests/test_a.py": self._func(FUNC_MAX + 1),
                             "tools/x/tests/test_b.py": "x = 1\n" * (MODULE_MAX + 1)})
        self.assertEqual(found, {})

    def test_a_baseline_entry_is_a_ceiling(self):
        found = self._sizes({"agent/a.py": self._func(100)})
        self.assertEqual(check(found, {"agent/a.py::f": 100}), ([], []))
        over, _ = check(found, {"agent/a.py::f": 99})
        self.assertEqual(over, ["agent/a.py::f is 100 lines (allowed 99)"])

    def test_stale_baseline_entries_are_reported(self):
        found = self._sizes({"agent/a.py": self._func(90, "shrank") + self._func(10, "fixed")})
        over, stale = check(found, {"agent/a.py::shrank": 120, "agent/a.py::fixed": 95,
                                    "agent/a.py::gone": 95, "agent/gone.py": 900})
        self.assertEqual(over, [])
        self.assertEqual(stale, [
            "agent/a.py::fixed is within budget (10 lines): delete its BASELINE entry",
            "agent/a.py::gone no longer exists: delete its BASELINE entry",
            "agent/a.py::shrank shrank from 120 to 90 lines: lower its BASELINE entry",
            "agent/gone.py no longer exists: delete its BASELINE entry",
        ])


if __name__ == "__main__":
    unittest.main()
