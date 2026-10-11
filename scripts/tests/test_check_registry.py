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


def _array(*entries: str) -> str:
    return "SUITES=(\n" + "".join(f"  {e}\n" for e in entries) + ")\n"


DEFAULT_SCRIPT = _array(
    '"dbc|python3 -m unittest discover -s tools/dbc/tests"',
    '"wowmap-js|node --test tools/wowmap/tests/js/*.test.js"',
)


SUITE_ENTRY = re.compile(r'^\s*"[^"|]+\|([^"]*)"')


def _suite_span(lines: list[str]) -> tuple[int, int]:
    # One SUITES=( block, opened on its own line. Between it and the first bare ")" only blank
    # lines, comments and quoted entries may appear. Anything else is a reformat: ValueError.
    opens = [i for i, line in enumerate(lines) if "SUITES=(" in line]
    if len(opens) != 1 or lines[opens[0]] != "SUITES=(":
        raise ValueError("expected exactly one SUITES=( line, on its own")
    for end in range(opens[0] + 1, len(lines)):
        if lines[end] == ")":
            return opens[0] + 1, end
        body = lines[end].strip()
        if body and not body.startswith(("#", '"')):
            raise ValueError(f"SUITES array has an unexpected line: {lines[end]!r}")
    raise ValueError("SUITES array is not closed by a bare )")


def registered_suites(script_text: str) -> set[tuple[str, str]]:
    # Only the command inside each quoted SUITES entry registers a suite. Comments, trailing
    # text after the closing quote and anything outside the array do not.
    lines = script_text.splitlines()
    first, last = _suite_span(lines)
    commands = [m.group(1) for m in map(SUITE_ENTRY.match, lines[first:last]) if m]
    py = {("py", d) for c in commands for d in re.findall(r"-s ([^\s\"]+)", c)}
    js = {("js", d) for c in commands for d in re.findall(r"node --test (\S+?)/js/", c)}
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
    first, last = _suite_span(lines)
    return [i for i in range(first, last) if lines[i].lstrip().startswith('"')]


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
        self.assertEqual(registered_suites(script), found, "SUITES registers a suite not on disk")

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
            # The pair the line itself registers, read from its own text alone.
            expected = registered_suites(_array(lines[i].strip()))
            self.assertEqual(len(expected), 1)
            with self.subTest(line=lines[i]):
                text = "\n".join(lines[:i] + lines[i + 1:])
                self.assertEqual(found - registered_suites(text), expected)
            with self.subTest(commented=lines[i]):
                commented = lines[:i] + ["# " + lines[i].lstrip()] + lines[i + 1:]
                self.assertEqual(found - registered_suites("\n".join(commented)), expected)

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
            self._unregistered_in(rel, _array('"y|python3 -m unittest discover -s tools/y/tests"')),
            {("js", "tools/y/tests")},
        )
        self.assertEqual(
            self._unregistered_in(rel, _array('"y|node --test tools/y/tests/js/*.test.js"')), set()
        )

    def test_comment_mentioning_a_suite_is_not_a_registration(self):
        self.assertEqual(
            self._unregistered_in(
                ["tools/y/tests/js/a.test.js"], _array("# node --test tools/y/tests/js/")
            ),
            {("js", "tools/y/tests")},
        )

    def test_path_in_trailing_comment_does_not_register_another_suite(self):
        script = _array(
            '"a|python3 -m unittest discover -s tools/y/tests"  # see also -s tools/x/tests'
        )
        self.assertEqual(
            self._unregistered_in(["tools/x/tests/test_x.py"], script),
            {("py", "tools/x/tests")},
        )

    def test_s_outside_suites_array_does_not_register(self):
        script = (
            'OLD="python3 -m unittest discover -s tools/x/tests"\n'
            'echo "try -s tools/x/tests"\n'
            + _array('"a|python3 -m unittest discover -s tools/y/tests"')
        )
        self.assertEqual(
            self._unregistered_in(["tools/x/tests/test_x.py"], script),
            {("py", "tools/x/tests")},
        )

    def test_script_without_suites_array_is_an_error(self):
        with self.assertRaises(ValueError):
            registered_suites('"a|python3 -m unittest discover -s tools/x/tests"\n')

    def test_entry_shaped_line_after_the_array_does_not_register(self):
        script = (
            _array('"a|python3 -m unittest discover -s tools/y/tests"')
            + 'OTHER=(\n"x|python3 -m unittest discover -s tools/x/tests"\n)\n'
        )
        self.assertEqual(
            self._unregistered_in(["tools/x/tests/test_x.py"], script),
            {("py", "tools/x/tests")},
        )

    def test_array_closed_by_indented_or_commented_paren_is_an_error(self):
        for close in ("  )", ")  # end"):
            with self.subTest(close=close):
                script = (
                    'SUITES=(\n  "a|python3 -m unittest discover -s tools/y/tests"\n'
                    + close + '\n  "x|python3 -m unittest discover -s tools/x/tests"\n)\n'
                )
                with self.assertRaises(ValueError):
                    registered_suites(script)

    def test_two_suites_arrays_are_an_error(self):
        script = _array('"a|python3 -m unittest discover -s tools/y/tests"') * 2
        with self.assertRaises(ValueError):
            registered_suites(script)

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
