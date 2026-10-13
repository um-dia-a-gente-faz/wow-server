"""#725: `scripts/check.sh tmp-leaks` lists what a suite left in its private TMPDIR.

The helper is read from scripts/check.sh as text and run alone in bash, so no suite runs here.
"""
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HELPER = re.search(r"^leftovers_in\(\) \{.*\}$", (ROOT / "scripts" / "check.sh").read_text(), re.M).group(0)


def leftovers(directory: str) -> list[str]:
    run = subprocess.run(["bash", "-c", HELPER + '\nleftovers_in "$1"', "x", directory],
                         capture_output=True, text=True, check=True)
    return run.stdout.splitlines()


class TmpLeaksHelperTest(unittest.TestCase):
    def test_lists_what_is_left_and_nothing_when_clean(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(leftovers(d), [])
            (pathlib.Path(d) / "wow-agent-test-names-1.json").write_text("{}")
            (pathlib.Path(d) / "tmpabc").mkdir()
            self.assertEqual(leftovers(d), ["tmpabc", "wow-agent-test-names-1.json"])


if __name__ == "__main__":
    unittest.main()
