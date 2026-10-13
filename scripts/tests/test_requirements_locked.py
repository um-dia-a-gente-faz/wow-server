"""Guard (#737): requirements-dev.txt is hash-locked and CI installs it with --require-hashes.

Stdlib only. The file and the workflow are read as text. pip itself proves the hashes in the
lint and coverage jobs (Python 3.12): a package missing from the file or a wrong hash fails there.
This test catches the cheap regressions: an unpinned line, a line with no hash, a hash that is
not sha256, and a CI install that drops the flag.

RELEASE_FILES holds the per-pin release-file counts. A pin bump must update the table in the same
change as requirements-dev.txt (docs/DEPLOYMENT.md, Bumping pins); a count that disagrees is a
stale table, not a bad hash, so update the table with the new counts from the index.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
REQ = (ROOT / "requirements-dev.txt").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

PIN = re.compile(r"^[A-Za-z0-9._-]+==[0-9][^\s;]*(\s*;\s*[^\s].*)?$")

# Release files of each pin on PyPI (urls[] of /pypi/<name>/<version>/json when the lock was made).
# A hash removed from a line makes its count short; a new transitive package makes the set differ.
# Bumping a pin means updating this table with the same counts (docs/DEPLOYMENT.md, Bumping pins).
RELEASE_FILES = {
    "ruff==0.8.6": 18,
    "mypy==1.14.1": 38,
    "coverage==7.16.2": 121,
    "typing_extensions==4.16.0": 2,
    "mypy_extensions==1.1.0": 2,
    "tomli==2.5.0": 65,
}


def requirements(text=REQ):
    """Each requirement as one string: its pin line plus its --hash options (continuations folded)."""
    for line in text.replace("\\\n", " ").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            yield line


def ci_requirement_installs(text=CI):
    return [l.strip() for l in text.splitlines() if "pip install" in l and "requirements-dev.txt" in l]


class RequirementsDevLockTest(unittest.TestCase):
    def test_every_requirement_is_pinned_with_sha256_hashes(self):
        reqs = list(requirements())
        self.assertEqual({r.partition(" --hash")[0].split(";")[0].strip() for r in reqs}, set(RELEASE_FILES))
        for req in reqs:
            pin = req.partition(" --hash")[0].split(";")[0].strip()
            self.assertRegex(pin, PIN, req[:60])
            hashes = re.findall(r"--hash=(\S+)", req)
            for h in hashes:
                self.assertRegex(h, r"^sha256:[0-9a-f]{64}$", pin)
            self.assertEqual(len(hashes), len(set(hashes)), f"duplicate hash on {pin}")
            self.assertEqual(len(hashes), RELEASE_FILES[pin], f"hash count on {pin}")

    def test_the_three_gate_tools_are_still_pinned(self):
        names = {re.split(r"[=; ]", r, maxsplit=1)[0] for r in requirements()}
        self.assertLessEqual({"ruff", "mypy", "coverage"}, names)

    def test_every_ci_install_of_the_file_requires_hashes(self):
        installs = ci_requirement_installs()
        self.assertEqual(len(installs), 2, installs)   # lint + types job and the coverage job
        for line in installs:
            self.assertIn("pip install --require-hashes -r requirements-dev.txt", line)


if __name__ == "__main__":
    unittest.main()
