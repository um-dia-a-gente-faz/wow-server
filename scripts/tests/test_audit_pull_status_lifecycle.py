"""#622: audit-pull-status.sh freshness counts the decision day files only.

<audit_dir>/<agent>/lifecycle/<YYYY-MM-DD>.jsonl (PR #617) is not think-loop activity, so a
fresh lifecycle file must not make a stale agent read OK. PULL BEHIND still counts the whole
tree on both sides (lifecycle lag is real pull lag).

Runs the real script against a temp audit tree; RSYNC_BIN is a fake, no ssh, no real directory.
"""
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATUS = ROOT / "scripts" / "wow-agents" / "audit-pull-status.sh"

# Prints FAKE_RSYNC_LISTING for --list-only (the source listing), nothing else.
FAKE_RSYNC = """#!/bin/bash
for a in "$@"; do
  if [ "$a" = "--list-only" ]; then printf '%s' "$FAKE_RSYNC_LISTING"; exit 0; fi
done
exit 1
"""


def touch(path: pathlib.Path, age_s: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    t = time.time() - age_s
    os.utime(path, (t, t))


class AuditPullStatusLifecycleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.dest = self.tmp / "mirror"
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.now = int(time.time())
        # fresh heartbeat: the pull is healthy
        (self.state / "last-success").write_text(f"{self.now} iso\n")
        self.fake = self.tmp / "fake-rsync"
        self.fake.write_text(FAKE_RSYNC)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.agent = self.dest / "AGENT01"

    def run_status(self, **extra):
        env = {"PATH": os.environ["PATH"], "AUDIT_DEST": str(self.dest),
               "AUDIT_STATE_DIR": str(self.state), "SKIP_SOURCE": "1", "QUIET_AFTER_S": "300",
               **{k: str(v) for k, v in extra.items()}}
        return subprocess.run(["bash", str(STATUS)], env=env, capture_output=True, text=True, timeout=30)

    def assertVerdict(self, r, verdict, code):
        self.assertIn(f"VERDICT: {verdict}", r.stdout, r.stdout + r.stderr)
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)

    def test_old_day_file_and_fresh_lifecycle_reads_quiet(self):
        touch(self.agent / "2026-10-10.jsonl", age_s=3600)
        touch(self.agent / "lifecycle" / "2026-10-12.jsonl", age_s=5)
        self.assertVerdict(self.run_status(), "QUIET", 0)

    def test_fresh_day_file_and_old_lifecycle_reads_ok(self):
        touch(self.agent / "2026-10-12.jsonl", age_s=5)
        touch(self.agent / "lifecycle" / "2026-10-10.jsonl", age_s=3600)
        self.assertVerdict(self.run_status(), "OK", 0)

    def test_fresh_rotated_day_file_counts(self):
        touch(self.agent / "2026-10-12.jsonl", age_s=3600)
        touch(self.agent / "2026-10-12.jsonl.1", age_s=5)
        touch(self.agent / "lifecycle" / "2026-10-10.jsonl", age_s=3600)
        self.assertVerdict(self.run_status(), "OK", 0)

    def test_old_rotated_day_file_with_fresh_lifecycle_reads_quiet(self):
        touch(self.agent / "2026-10-12.jsonl.1", age_s=3600)
        touch(self.agent / "lifecycle" / "2026-10-12.jsonl", age_s=5)
        self.assertVerdict(self.run_status(), "QUIET", 0)

    def listing_line(self, name: str, age_s: float) -> str:
        ts = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(time.time() - age_s))
        return f"-rw-r--r--        120 {ts} {name}\n"

    def test_stuck_loop_with_source_check_enabled_reads_quiet_and_labels_ages(self):
        # The #622 scenario with the source check on, as the timer runs it: the stuck agent
        # reconnects, so its lifecycle file is fresh on both sides. The source and the mirror
        # agree (5 s), the decision file is 3600 s old, and the verdict is QUIET, exit 0.
        touch(self.agent / "2026-10-10.jsonl", age_s=3600)
        touch(self.agent / "lifecycle" / "2026-10-12.jsonl", age_s=5)
        listing = (self.listing_line("AGENT01/2026-10-10.jsonl", 3600)
                   + self.listing_line("AGENT01/lifecycle/2026-10-12.jsonl", 5))
        r = self.run_status(SKIP_SOURCE="0", AUDIT_SRC_HOST="guest.invalid",
                            RSYNC_BIN=self.fake, FAKE_RSYNC_LISTING=listing)
        self.assertVerdict(r, "QUIET", 0)
        mirror = next(x for x in r.stdout.splitlines() if x.startswith("mirror:"))
        m = re.search(r"newest audit file (\d+) s old, newest decision file (\d+) s old", mirror)
        self.assertIsNotNone(m, mirror)
        self.assertLessEqual(int(m.group(1)), 10, mirror)
        self.assertGreaterEqual(int(m.group(2)), 3600, mirror)
        self.assertLessEqual(int(m.group(2)), 3610, mirror)

    def test_pull_behind_still_counts_lifecycle_on_source(self):
        # mirror: day file 1000 s old. source: a lifecycle file 10 s old that the mirror
        # has not pulled, i.e. the pull is behind. Must stay PULL BEHIND (exit 1).
        touch(self.agent / "2026-10-12.jsonl", age_s=1000)
        r = self.run_status(SKIP_SOURCE="0", AUDIT_SRC_HOST="guest.invalid",
                            RSYNC_BIN=self.fake,
                            FAKE_RSYNC_LISTING=self.listing_line("AGENT01/lifecycle/2026-10-12.jsonl", 10))
        self.assertVerdict(r, "PULL BEHIND", 1)


if __name__ == "__main__":
    unittest.main()
