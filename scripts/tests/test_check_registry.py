"""Guard (#298, #542): every tests directory must be registered in scripts/check.sh."""
import os
import pathlib
import re
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOP_DIRS = ("agent", "tools", "exporters", "scripts")
SKIP_NAMES = {"node_modules", "__pycache__", ".claude"}


def registered_dirs(script_text: str) -> set[str]:
    return set(re.findall(r"-s ([^\s\"]+)", script_text)) | set(
        re.findall(r"node --test (\S+?)/js/", script_text)
    )


def find_tests_dirs(root: pathlib.Path) -> set[str]:
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
            js = d / "js"
            if any(d.glob("test_*.py")) or (js.is_dir() and any(js.glob("*.test.js"))):
                found.add(d.relative_to(root).as_posix())
    return found


def _write(root: pathlib.Path, rel_paths: list[str]) -> None:
    for rel in rel_paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")


class CheckRegistryTest(unittest.TestCase):
    def test_every_tests_dir_is_registered(self):
        script = (ROOT / "scripts" / "check.sh").read_text()
        found = find_tests_dirs(ROOT)
        known = {"agent/tests", "tools/tests", "exporters/wow-exporter/tests", "scripts/tests"}
        self.assertEqual(sorted(known - found), [], "the walk no longer finds a known tests dir")
        missing = found - registered_dirs(script)
        self.assertEqual(sorted(missing), [], "tests dir not registered in scripts/check.sh SUITES")

    def _unregistered_in(self, rel_paths: list[str]) -> set[str]:
        script = 'dbc|python3 -m unittest discover -s tools/dbc/tests\n'
        script += 'wowmap-js|node --test tools/wowmap/tests/js/*.test.js\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write(root, rel_paths)
            return find_tests_dirs(root) - registered_dirs(script)

    def test_unregistered_tools_tests_is_reported(self):
        self.assertEqual(self._unregistered_in(["tools/tests/test_x.py"]), {"tools/tests"})

    def test_tests_dir_at_depth_three_is_reported(self):
        self.assertEqual(self._unregistered_in(["tools/a/b/tests/test_x.py"]), {"tools/a/b/tests"})

    def test_exporters_tests_dir_is_reported(self):
        self.assertEqual(
            self._unregistered_in(["exporters/x/tests/test_x.py"]), {"exporters/x/tests"}
        )

    def test_node_modules_copy_is_not_reported(self):
        self.assertEqual(self._unregistered_in(["tools/x/node_modules/y/tests/test_x.py"]), set())

    def test_dot_claude_copy_is_not_reported(self):
        self.assertEqual(
            self._unregistered_in(["tools/x/.claude/worktrees/w/tests/test_x.py"]), set()
        )

    def test_js_only_tests_dir_is_reported(self):
        self.assertEqual(self._unregistered_in(["tools/y/tests/js/a.test.js"]), {"tools/y/tests"})

    def test_tests_dir_without_test_module_is_not_reported(self):
        self.assertEqual(self._unregistered_in(["tools/x/tests/helper.py"]), set())

    def test_js_subdir_does_not_count_as_separate_tests_dir(self):
        rel = ["tools/wowmap/tests/test_a.py", "tools/wowmap/tests/js/x.test.js"]
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write(root, rel)
            self.assertEqual(find_tests_dirs(root), {"tools/wowmap/tests"})


if __name__ == "__main__":
    unittest.main()
