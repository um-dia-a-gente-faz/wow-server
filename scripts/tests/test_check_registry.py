"""Guard (#298, #542, #560): every tests suite must be registered in scripts/check.sh.

A suite is a (kind, dir) pair: ("py", dir) for test_*.py, ("js", dir) for js/*.test.js.
"""
import os
import pathlib
import re
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOP_DIRS = ("agent", "tools", "exporters", "scripts")
SKIP_NAMES = {"node_modules", "__pycache__", ".claude"}
DEFAULT_SCRIPT = (
    'dbc|python3 -m unittest discover -s tools/dbc/tests\n'
    'wowmap-js|node --test tools/wowmap/tests/js/*.test.js\n'
)


def registered_suites(script_text: str) -> set[tuple[str, str]]:
    # Comment lines are not registrations, even when they name a path.
    code = "\n".join(l for l in script_text.splitlines() if not l.lstrip().startswith("#"))
    py = {("py", d) for d in re.findall(r"-s ([^\s\"]+)", code)}
    js = {("js", d) for d in re.findall(r"node --test (\S+?)/js/", code)}
    return py | js


def find_suites(root: pathlib.Path) -> set[tuple[str, str]]:
    found = set()
    for top in TOP_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES]
            d = pathlib.Path(dirpath)
            if d.name != "tests":
                continue
            rel = d.relative_to(root).as_posix()
            js = d / "js"
            if any(d.glob("test_*.py")):
                found.add(("py", rel))
            if js.is_dir() and any(js.glob("*.test.js")):
                found.add(("js", rel))
    return found


def _write(root: pathlib.Path, rel_paths: list[str]) -> None:
    for rel in rel_paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")


def _suite_lines(script_text: str) -> list[int]:
    lines = script_text.splitlines()
    start = lines.index("SUITES=(")
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == ")")
    return [i for i in range(start + 1, end) if lines[i].lstrip().startswith('"')]


class CheckRegistryTest(unittest.TestCase):
    def test_every_tests_dir_is_registered(self):
        script = (ROOT / "scripts" / "check.sh").read_text()
        found = find_suites(ROOT)
        known = {
            ("py", "agent/tests"),
            ("py", "tools/tests"),
            ("py", "exporters/wow-exporter/tests"),
            ("py", "scripts/tests"),
            ("js", "tools/wowmap/tests"),
            ("py", "tools/wowmap/tests"),
        }
        self.assertEqual(sorted(known - found), [], "the walk no longer finds a known suite")
        missing = found - registered_suites(script)
        self.assertEqual(sorted(missing), [], "suite not registered in scripts/check.sh SUITES")

    def test_wowmap_python_line_removed_reports_python_suite(self):
        script = "\n".join(
            l for l in (ROOT / "scripts" / "check.sh").read_text().splitlines()
            if not l.lstrip().startswith('"wowmap|')
        )
        self.assertEqual(
            find_suites(ROOT) - registered_suites(script), {("py", "tools/wowmap/tests")}
        )

    def test_wowmap_js_line_removed_reports_js_suite(self):
        script = "\n".join(
            l for l in (ROOT / "scripts" / "check.sh").read_text().splitlines()
            if not l.lstrip().startswith('"wowmap-js|')
        )
        self.assertEqual(
            find_suites(ROOT) - registered_suites(script), {("js", "tools/wowmap/tests")}
        )

    def test_every_suite_line_is_needed(self):
        lines = (ROOT / "scripts" / "check.sh").read_text().splitlines()
        suite_idx = _suite_lines("\n".join(lines))
        self.assertTrue(suite_idx)
        found = find_suites(ROOT)
        for i in suite_idx:
            with self.subTest(line=lines[i]):
                text = "\n".join(lines[:i] + lines[i + 1:])
                self.assertEqual(len(found - registered_suites(text)), 1)

    def _unregistered_in(self, rel_paths: list[str], script: str = DEFAULT_SCRIPT) -> set:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write(root, rel_paths)
            return find_suites(root) - registered_suites(script)

    def test_unregistered_tools_tests_is_reported(self):
        self.assertEqual(
            self._unregistered_in(["tools/tests/test_x.py"]), {("py", "tools/tests")}
        )

    def test_tests_dir_at_depth_three_is_reported(self):
        self.assertEqual(
            self._unregistered_in(["tools/a/b/tests/test_x.py"]), {("py", "tools/a/b/tests")}
        )

    def test_exporters_tests_dir_is_reported(self):
        self.assertEqual(
            self._unregistered_in(["exporters/x/tests/test_x.py"]),
            {("py", "exporters/x/tests")},
        )

    def test_node_modules_copy_is_not_reported(self):
        self.assertEqual(self._unregistered_in(["tools/x/node_modules/y/tests/test_x.py"]), set())

    def test_dot_claude_copy_is_not_reported(self):
        self.assertEqual(
            self._unregistered_in(["tools/x/.claude/worktrees/w/tests/test_x.py"]), set()
        )

    def test_js_only_tests_dir_needs_node_test_line(self):
        rel = ["tools/y/tests/js/a.test.js"]
        self.assertEqual(
            self._unregistered_in(rel, "y|python3 -m unittest discover -s tools/y/tests\n"),
            {("js", "tools/y/tests")},
        )
        self.assertEqual(
            self._unregistered_in(rel, "y|node --test tools/y/tests/js/*.test.js\n"), set()
        )

    def test_comment_mentioning_a_suite_is_not_a_registration(self):
        self.assertEqual(
            self._unregistered_in(
                ["tools/y/tests/js/a.test.js"], "# node --test tools/y/tests/js/\n"
            ),
            {("js", "tools/y/tests")},
        )

    def test_tests_dir_without_test_module_is_not_reported(self):
        self.assertEqual(self._unregistered_in(["tools/x/tests/helper.py"]), set())

    def test_js_subdir_does_not_count_as_separate_tests_dir(self):
        rel = ["tools/wowmap/tests/test_a.py", "tools/wowmap/tests/js/x.test.js"]
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write(root, rel)
            self.assertEqual(
                find_suites(root),
                {("py", "tools/wowmap/tests"), ("js", "tools/wowmap/tests")},
            )


if __name__ == "__main__":
    unittest.main()
