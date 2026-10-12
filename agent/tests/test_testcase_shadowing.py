"""Fitness test (#697): no unittest.TestCase subclass defines a method that shadows a
TestCase method it does not mean to override.

A `def fail(self, b)` in a TestCase subclass replaces unittest.TestCase.fail, so an
assertion that calls it reports ERROR with an AttributeError instead of FAIL with its
message. The same holds for every `assert*` and for the runner's own methods (run, id,
debug, skipTest, subTest, ...).

Files are read with `ast`, never imported. Scanned: every .py under agent/tests/,
tools/*/tests/, exporters/*/tests/ and scripts/tests/ (test modules and helpers alike).

Covered classes, at any nesting, matched by name within their own module:
- a class with a base whose name (the last part of a dotted name) ends in TestCase;
- a class that inherits, in the same module, from a covered class;
- a class in the same module that is a base of a covered class (a mixin), transitively.
Not covered: a base or mixin imported from another module, unless its name ends in
TestCase. Such cross-module mixins are not followed (no import, no guessing), so a
`def fail` in one of them is not caught here. A class that is not a TestCase in its own
module, and is not a base of one, is not checked.

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
    ROOT / "scripts" / "tests",
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


def _covered_names(classes: list[ast.ClassDef]) -> set[str]:
    names = {c.name for c in classes}
    tests: set[str] = set()
    grow = True
    while grow:   # test classes: TestCase-named bases, then subclasses of those
        grow = False
        for c in classes:
            if c.name not in tests and any(_base(b).endswith("TestCase") or _base(b) in tests
                                           for b in c.bases):
                tests.add(c.name)
                grow = True
    covered = set(tests)
    todo = list(tests)
    while todo:   # mixins: same-module bases of covered classes, transitively
        name = todo.pop()
        for c in classes:
            if c.name == name:
                for b in map(_base, c.bases):
                    if b in names and b not in covered:
                        covered.add(b)
                        todo.append(b)
    return covered


def shadowing(source: str) -> list[tuple[str, str, int]]:
    """(class, method, line) for each def in a covered class whose name is forbidden."""
    classes = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ClassDef)]
    covered = _covered_names(classes)
    return [(c.name, f.name, f.lineno)
            for c in classes if c.name in covered
            for f in c.body
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name in FORBIDDEN]


def scanned_files() -> list[pathlib.Path]:
    return [p for d in SCANNED_DIRS for p in sorted(d.rglob("*.py"))]


class TestCaseShadowingTest(unittest.TestCase):
    """#697: a def named like a TestCase method (fail, run, assertEqual, ...) in a test case."""

    def test_no_testcase_class_shadows_a_testcase_method(self):
        self.assertIn(ROOT / "agent" / "tests" / "test_brain_scenarios.py", scanned_files())
        found = []
        for path in scanned_files():
            for cls, name, line in shadowing(path.read_text(encoding="utf-8")):
                found.append(f"{path.relative_to(ROOT)}:{line} {cls}.{name}")
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


if __name__ == "__main__":
    unittest.main()
