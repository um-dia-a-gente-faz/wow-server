"""GH #194: no tracked file may name the old repo path.

The repo moved from Cividati/wow-server to um-dia-a-gente-faz/wow-server. GitHub
redirects the old path, so stale references work by accident and drift unnoticed.
`Cividati/<repo>` is stale; bare `Cividati` is the GitHub account and is correct.
"""
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OLD = "Cividati/wow-server"

# Deliberate exceptions (path -> why).
ALLOWED = {
    ".hermes/plans/2026-09-13_wow-server-initial.md": "dated historical plan, kept as written (already marked historical)",
    "agent/tests/test_repo_name.py": "defines the pattern",
}


class RepoNameTest(unittest.TestCase):
    def test_no_old_repo_path(self):
        files = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
                               check=True).stdout.decode().split("\0")
        bad = []
        for f in filter(None, files):
            if f in ALLOWED or not (REPO / f).is_file():
                continue
            try:
                text = (REPO / f).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            bad += [f"{f}:{n}" for n, line in enumerate(text.splitlines(), 1) if OLD in line]
        self.assertEqual(bad, [], f"use um-dia-a-gente-faz/wow-server, not {OLD}")


if __name__ == "__main__":
    unittest.main()
