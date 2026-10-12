"""#631: audit-pull-status.sh judges each agent directory, not the whole mirror at once.

Each directory directly under <audit_dir> is an agent; its age is the newest decision day file
directly in it (lifecycle/ does not count, #622). One healthy agent must not mask a dead one:
any quiet agent makes the verdict QUIET and the line names it. PULL BEHIND still compares the
whole tree on both sides.

Runs the real script against a temp audit tree; RSYNC_BIN is a fake, no ssh, no real directory.
"""
import os
import pathlib
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

QUIET_TEXT = "VERDICT: QUIET - the pull is healthy and the source has nothing newer: no agent activity for over 300s."


def touch(path: pathlib.Path, age_s: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    t = time.time() - age_s
    os.utime(path, (t, t))


class AuditPullStatusPerAgentTest(unittest.TestCase):
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

    def run_status(self, **extra):
        env = {"PATH": os.environ["PATH"], "AUDIT_DEST": str(self.dest),
               "AUDIT_STATE_DIR": str(self.state), "SKIP_SOURCE": "1", "QUIET_AFTER_S": "300",
               **{k: str(v) for k, v in extra.items()}}
        return subprocess.run(["bash", str(STATUS)], env=env, capture_output=True, text=True, timeout=30)

    def assertVerdict(self, r, verdict, code):
        self.assertIn(f"VERDICT: {verdict}", r.stdout, r.stdout + r.stderr)
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)

    def verdict_line(self, r) -> str:
        return next(x for x in r.stdout.splitlines() if x.startswith("VERDICT:"))

    def listing_line(self, name: str, age_s: float) -> str:
        ts = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(time.time() - age_s))
        return f"-rw-r--r--        120 {ts} {name}\n"

    def test_one_fresh_and_one_dead_agent_reads_quiet_and_names_the_dead_one(self):
        touch(self.dest / "AGENT01" / "2026-10-12.jsonl", age_s=5)
        touch(self.dest / "AGENT02" / "2026-10-10.jsonl", age_s=9000)
        r = self.run_status()
        self.assertVerdict(r, "QUIET", 0)
        self.assertIn("from: AGENT02.", self.verdict_line(r))
        self.assertNotIn("AGENT01", self.verdict_line(r))
        self.assertIn("agent AGENT01: newest decision file", r.stdout)
        self.assertIn("agent AGENT02: newest decision file", r.stdout)

    def test_all_fresh_agents_read_ok(self):
        touch(self.dest / "AGENT01" / "2026-10-12.jsonl", age_s=5)
        touch(self.dest / "AGENT02" / "2026-10-12.jsonl", age_s=60)
        r = self.run_status()
        self.assertVerdict(r, "OK", 0)

    def test_agent_with_only_lifecycle_file_is_quiet_and_named(self):
        touch(self.dest / "AGENT01" / "2026-10-12.jsonl", age_s=5)
        touch(self.dest / "AGENT02" / "lifecycle" / "2026-10-12.jsonl", age_s=5)
        r = self.run_status()
        self.assertVerdict(r, "QUIET", 0)
        self.assertIn("from: AGENT02.", self.verdict_line(r))
        self.assertIn("agent AGENT02: no decision files", r.stdout)

    def test_quiet_agents_are_named_sorted_by_name(self):
        # created in the order c, a, b: the names must come out a, b, c
        for name in ("c", "a", "b"):
            touch(self.dest / name / "2026-10-10.jsonl", age_s=9000)
        r = self.run_status()
        self.assertVerdict(r, "QUIET", 0)
        self.assertIn("from: a, b, c.", self.verdict_line(r))

    def test_pull_behind_wins_over_quiet_and_agent_lines_are_still_printed(self):
        touch(self.dest / "AGENT01" / "2026-10-12.jsonl", age_s=1000)
        touch(self.dest / "AGENT02" / "2026-10-10.jsonl", age_s=9000)
        r = self.run_status(SKIP_SOURCE="0", AUDIT_SRC_HOST="guest.invalid",
                            RSYNC_BIN=self.fake,
                            FAKE_RSYNC_LISTING=self.listing_line("AGENT01/2026-10-12.jsonl", 10))
        self.assertVerdict(r, "PULL BEHIND", 1)
        self.assertIn("agent AGENT01: newest decision file", r.stdout)
        self.assertIn("agent AGENT02: newest decision file", r.stdout)

    def test_pull_stalled_wins_over_quiet_and_agent_lines_are_still_printed(self):
        (self.state / "last-success").write_text(f"{self.now - 1000} iso\n")
        touch(self.dest / "AGENT01" / "2026-10-12.jsonl", age_s=5)
        touch(self.dest / "AGENT02" / "2026-10-10.jsonl", age_s=9000)
        r = self.run_status()
        self.assertVerdict(r, "PULL STALLED", 2)
        self.assertIn("agent AGENT02: newest decision file", r.stdout)

    def test_directory_name_with_a_space_is_one_agent(self):
        touch(self.dest / "AGENT 01" / "2026-10-12.jsonl", age_s=5)
        touch(self.dest / "BOB 2" / "2026-10-10.jsonl", age_s=9000)
        r = self.run_status()
        self.assertVerdict(r, "QUIET", 0)
        self.assertIn("from: BOB 2.", self.verdict_line(r))
        self.assertIn("agent BOB 2: newest decision file", r.stdout)
        self.assertIn("agent AGENT 01: newest decision file", r.stdout)

    def test_no_agent_directory_keeps_todays_quiet_text(self):
        # the mirror dir does not exist yet: no agent directory at all
        r = self.run_status()
        self.assertEqual(self.verdict_line(r), QUIET_TEXT, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse([x for x in r.stdout.splitlines() if x.startswith("agent ")], r.stdout)


if __name__ == "__main__":
    unittest.main()
