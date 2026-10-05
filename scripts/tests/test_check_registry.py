"""Guard (#298): every tests directory must be registered in scripts/check.sh."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class CheckRegistryTest(unittest.TestCase):
    def test_every_tests_dir_is_registered(self):
        script = (ROOT / "scripts" / "check.sh").read_text()
        registered = set(re.findall(r"-s ([^\s\"]+)", script)) | set(re.findall(r"node --test (\S+?)/js/", script))
        found = {
            p.relative_to(ROOT).as_posix()
            for pattern in ("agent/tests", "tools/*/tests", "scripts/*/tests", "scripts/tests")
            for p in ROOT.glob(pattern)
            if p.is_dir()
        }
        self.assertEqual(sorted(found - registered), [], "tests dir not registered in scripts/check.sh SUITES")


if __name__ == "__main__":
    unittest.main()
