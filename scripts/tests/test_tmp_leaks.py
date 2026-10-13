"""#725: `scripts/check.sh tmp-leaks` lists what a suite left in its private TMPDIR.

The functions are read from scripts/check.sh as text and run in bash with a fake SUITES array,
so no real suite runs here. Every directory the test uses is a temporary one it creates.
"""
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = (ROOT / "scripts" / "check.sh").read_text()
HELPER = re.search(r"^leftovers_in\(\) \{.*\}$", CHECK, re.M).group(0)
# suite() through tmp_leaks(): the real functions, not the dispatch at the bottom of the script.
FUNCTIONS = re.search(r"^suite\(\) \{.*?^tmp_leaks\(\) \(.*?^\)$", CHECK, re.M | re.S).group(0)


def leftovers(directory: str) -> list[str]:
    run = subprocess.run(["bash", "-c", HELPER + '\nleftovers_in "$1"', "x", directory],
                         capture_output=True, text=True, check=True)
    return run.stdout.splitlines()


def run_tmp_leaks(suites: str, outer: str, functions: str = FUNCTIONS) -> subprocess.CompletedProcess:
    """The real tmp_leaks() against fake `SUITES=(...)`, with TMPDIR = `outer`."""
    script = f"set -euo pipefail\nSUITES=({suites})\n{functions}\ntmp_leaks\n"
    env = {k: v for k, v in os.environ.items() if k != "CHECK_STALL_DUMP_S"}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={**env, "TMPDIR": outer})


class TmpLeaksHelperTest(unittest.TestCase):
    def test_lists_what_is_left_and_nothing_when_clean(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(leftovers(d), [])
            (pathlib.Path(d) / "wow-agent-test-names-1.json").write_text("{}")
            (pathlib.Path(d) / "tmpabc").mkdir()
            (pathlib.Path(d) / ".hidden").write_text("")  # ls -A lists dotfiles; plain ls would not
            self.assertEqual(sorted(leftovers(d)), [".hidden", "tmpabc", "wow-agent-test-names-1.json"])


class TmpLeaksTest(unittest.TestCase):
    """The real tmp_leaks(), run against fake suites with a private outer TMPDIR (#725)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.outer = tmp.name

    def assertOuterEmpty(self):
        self.assertEqual(os.listdir(self.outer), [], "tmp_leaks must leave the outer TMPDIR empty")

    def test_suite_that_leaves_a_file_fails_naming_it(self):
        run = run_tmp_leaks('"leaky|touch \\"\\$TMPDIR/left.json\\"" "clean|true"', self.outer)
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertIn("leaky", run.stderr)
        self.assertIn("left.json", run.stderr)
        self.assertNotIn("clean", run.stderr)
        self.assertOuterEmpty()

    def test_clean_suites_pass(self):
        run = run_tmp_leaks('"clean|true"', self.outer)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertOuterEmpty()

    def test_failing_suite_fails_naming_it(self):
        run = run_tmp_leaks('"broken|false"', self.outer)
        self.assertEqual(run.returncode, 1)
        self.assertIn("suite broken failed", run.stderr)
        self.assertOuterEmpty()


if __name__ == "__main__":
    unittest.main()
