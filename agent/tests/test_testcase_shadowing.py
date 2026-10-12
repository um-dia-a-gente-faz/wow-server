"""Fitness test (#697): no unittest.TestCase subclass defines a method that shadows a
TestCase method it does not mean to override.

A `def fail(self, b)` in a TestCase subclass replaces unittest.TestCase.fail, so an
assertion that calls it reports ERROR with an AttributeError instead of FAIL with its
message. The same holds for every `assert*` and for the runner's own methods (run, id,
debug, skipTest, subTest, ...).

Files are read with `ast`, never imported. Scanned: every .py under agent/tests/,
tools/tests/, tools/*/tests/, exporters/*/tests/, scripts/tests/ and
scripts/wow-agents/tests/ (test modules and helpers alike).

Test classes are found across all scanned files together, by name: a class is a test
class if a base's name ends in TestCase, or names a test class (in any scanned file).
So a base imported from another scanned module is followed by its name. Then, per file,
covered classes are the test classes of that file plus the classes of that file that
are bases of a covered class (mixins), transitively.

Not covered:
- a base whose name matches no test class in the scanned trees: a TestCase from outside
  them, a class that is not a test case, or an alias such as
  `from unittest import TestCase as TC` (the name does not end in TestCase);
- a def that is not directly in the class body: one under an `if`, or an assignment
  such as `fail = some_function`;
- names are matched, not resolved: a class that shares a test class's name in another
  file is treated the same way.

Forbidden: every public callable attribute of unittest.TestCase (read from dir() at
test time, so it follows the interpreter), minus the hooks TestCase calls on purpose:
setUp, tearDown, setUpClass, tearDownClass, shortDescription. No class in the repo
overrides run or id today, so neither is allowed; add a row to a table with a reason
if one ever does on purpose.
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCANNED_DIRS = [
    ROOT / "agent" / "tests",
    ROOT / "tools" / "tests",
    ROOT / "scripts" / "tests",
    ROOT / "scripts" / "wow-agents" / "tests",
    *sorted((ROOT / "tools").glob("*/tests")),
    *sorted((ROOT / "exporters").glob("*/tests")),
]
HOOKS = {"setUp", "tearDown", "setUpClass", "tearDownClass", "shortDescription"}
FORBIDDEN = {n for n in dir(unittest.TestCase)
             if not n.startswith("_") and callable(getattr(unittest.TestCase, n))} - HOOKS


def _base(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _classes(tree: ast.AST) -> list[ast.ClassDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]


def _test_names(classes: list[ast.ClassDef]) -> set[str]:
    """Names of the test classes among `classes` (all scanned files at once)."""
    tests: set[str] = set()
    grow = True
    while grow:
        grow = False
        for c in classes:
            if c.name not in tests and any(_base(b).endswith("TestCase") or _base(b) in tests
                                           for b in c.bases):
                tests.add(c.name)
                grow = True
    return tests


def _covered_names(classes: list[ast.ClassDef], tests: set[str]) -> set[str]:
    """Test classes of one file (by the global `tests` names), plus its mixins."""
    names = {c.name for c in classes}
    covered = {c.name for c in classes if c.name in tests}
    todo = list(covered)
    while todo:   # mixins: same-module bases of covered classes, transitively
        name = todo.pop()
        for c in classes:
            if c.name == name:
                for b in map(_base, c.bases):
                    if b in names and b not in covered:
                        covered.add(b)
                        todo.append(b)
    return covered


def shadowing_in(sources: dict[str, str]) -> list[tuple[str, str, str, int]]:
    """(source key, class, method, line) for each def in a covered class whose name is forbidden."""
    per_file = {key: _classes(ast.parse(src)) for key, src in sources.items()}
    tests = _test_names([c for classes in per_file.values() for c in classes])
    return [(key, c.name, f.name, f.lineno)
            for key, classes in per_file.items()
            for covered in [_covered_names(classes, tests)]
            for c in classes if c.name in covered
            for f in c.body
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name in FORBIDDEN]


def shadowing(source: str) -> list[tuple[str, str, int]]:
    """(class, method, line) within one source."""
    return [(c, m, line) for _, c, m, line in shadowing_in({"<src>": source})]


def scanned_files() -> list[pathlib.Path]:
    return [p for d in SCANNED_DIRS for p in sorted(d.rglob("*.py"))]


class TestCaseShadowingTest(unittest.TestCase):
    """#697: a def named like a TestCase method (fail, run, assertEqual, ...) in a test case."""

    def test_no_testcase_class_shadows_a_testcase_method(self):
        files = set(scanned_files())
        for rel in ("agent/tests/test_brain_scenarios.py", "tools/tests/test_architecture.py",
                    "tools/agent-runner/tests/test_characters.py", "scripts/tests/test_deploy.py",
                    "scripts/wow-agents/tests/test_pull_audit.py",
                    "exporters/wow-exporter/tests/test_backups.py"):
            self.assertIn(ROOT / rel, files, f"{rel} is not scanned")
        sources = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8") for p in sorted(files)}
        found = [f"{key}:{line} {cls}.{name}" for key, cls, name, line in shadowing_in(sources)]
        self.assertEqual(found, [])

    def test_probe_a_planted_fail_is_flagged(self):
        src = "import unittest\nclass T(unittest.TestCase):\n    def fail(self, x):\n        pass\n"
        self.assertEqual(shadowing(src), [("T", "fail", 3)])

    def test_probe_a_planted_assert_equal_is_flagged(self):
        src = "import unittest\nclass T(unittest.TestCase):\n    def assertEqual(self, a, b):\n        pass\n"
        self.assertEqual(shadowing(src), [("T", "assertEqual", 3)])

    def test_probe_hooks_are_not_flagged(self):
        src = ("import unittest\nclass T(unittest.TestCase):\n"
               "    def setUp(self): pass\n    def tearDown(self): pass\n"
               "    @classmethod\n    def setUpClass(cls): pass\n"
               "    @classmethod\n    def tearDownClass(cls): pass\n"
               "    def shortDescription(self): return None\n")
        self.assertEqual(shadowing(src), [])

    def test_probe_a_helper_that_is_not_a_test_case_is_not_flagged(self):
        src = "class Helper:\n    def fail(self, x):\n        pass\n    def run(self):\n        pass\n"
        self.assertEqual(shadowing(src), [])

    def test_probe_a_subclass_and_a_nested_class_are_flagged(self):
        src = ("import unittest\nclass Base(unittest.TestCase):\n    pass\n"
               "class Sub(Base):\n    def fail(self, x): pass\n"
               "class Outer:\n    class Inner(unittest.TestCase):\n        def run(self): pass\n")
        self.assertEqual(shadowing(src), [("Sub", "fail", 5), ("Inner", "run", 8)])

    def test_probe_a_same_module_mixin_of_a_test_case_is_flagged(self):
        src = ("import unittest\nclass Mixin:\n    def skipTest(self, why): pass\n"
               "class T(Mixin, unittest.TestCase):\n    pass\n")
        self.assertEqual(shadowing(src), [("Mixin", "skipTest", 3)])

    def test_probe_a_mixin_of_a_non_test_class_is_not_flagged(self):
        src = ("class Mixin:\n    def fail(self, x): pass\n"
               "class Plain(Mixin):\n    pass\n")
        self.assertEqual(shadowing(src), [])

    def test_probe_a_base_defined_as_a_test_case_in_another_source_is_flagged(self):
        base = "import unittest\nclass _ServerCase(unittest.TestCase):\n    pass\n"
        sub = "from base import _ServerCase\nclass ProducerTest(_ServerCase):\n    def fail(self, x): pass\n"
        self.assertEqual(shadowing_in({"base.py": base, "sub.py": sub}),
                         [("sub.py", "ProducerTest", "fail", 3)])

    def test_probe_a_base_that_is_not_a_test_case_in_any_source_is_not_flagged(self):
        plain = "class Plain:\n    pass\n"
        sub = "from plain import Plain\nclass Helper(Plain):\n    def run(self): pass\n"
        self.assertEqual(shadowing_in({"plain.py": plain, "sub.py": sub}), [])


if __name__ == "__main__":
    unittest.main()
