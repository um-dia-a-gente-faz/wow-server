"""Offline tests for the audit pull (issue #135, ADR 0002 D4).

pull-audit.sh and audit-pull-status.sh run against temp dirs with a fake rsync
on RSYNC_BIN; nothing touches the network or a real host. stdlib only.
"""
import fcntl
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
PULL = SCRIPTS / "pull-audit.sh"
STATUS = SCRIPTS / "audit-pull-status.sh"

# Records its argv, then acts: FAKE_RSYNC_LISTING is printed for --list-only;
# otherwise FAKE_RSYNC_SRC (if set) is copied into the last argument (dest) and
# the process exits FAKE_RSYNC_RC (default 0).
FAKE_RSYNC = r"""#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_RSYNC_LOG"
for a in "$@"; do
  if [ "$a" = "--list-only" ]; then
    printf '%s' "${FAKE_RSYNC_LISTING:-}"
    exit "${FAKE_RSYNC_RC:-0}"
  fi
done
dest="${@: -1}"
if [ -n "${FAKE_RSYNC_SRC:-}" ]; then cp -a "$FAKE_RSYNC_SRC"/. "$dest"; fi
exit "${FAKE_RSYNC_RC:-0}"
"""


def touch(path: Path, age_s: float = 0, text: str = "{}\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    t = time.time() - age_s
    os.utime(path, (t, t))


class ScriptCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.dest = self.tmp / "mirror"
        self.state = self.tmp / "state"
        self.src = self.tmp / "guest"
        self.log = self.tmp / "rsync.log"
        self.fake = self.tmp / "fake-rsync"
        self.fake.write_text(FAKE_RSYNC)
        self.fake.chmod(0o755)
        self.env = {
            "PATH": os.environ["PATH"],
            "AUDIT_SRC_HOST": "guest.invalid",
            "AUDIT_DEST": str(self.dest),
            "AUDIT_STATE_DIR": str(self.state),
            "AUDIT_SSH_KEY": str(self.tmp / "key"),
            "AUDIT_SSH_KNOWN_HOSTS": str(self.tmp / "known_hosts"),
            "RSYNC_BIN": str(self.fake),
            "FAKE_RSYNC_LOG": str(self.log),
        }

    def run_script(self, script, **extra):
        env = dict(self.env, **{k: str(v) for k, v in extra.items()})
        return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=30)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class PullAuditTests(ScriptCase):
    def test_success_pulls_and_writes_heartbeat(self):
        touch(self.src / "AGENT01" / "2026-10-03.jsonl")
        r = self.run_script(PULL, FAKE_RSYNC_SRC=self.src)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.dest / "AGENT01" / "2026-10-03.jsonl").exists())
        epoch, iso = (self.state / "last-success").read_text().split()
        self.assertLess(abs(time.time() - int(epoch)), 30)
        self.assertRegex(iso, r"^\d{4}-\d\d-\d\dT")
        self.assertFalse((self.state / "last-failure").exists())

    def test_rsync_invocation_is_read_only_pull_without_delete(self):
        self.run_script(PULL)
        (call,) = self.calls()
        self.assertIn("-a", call.split())
        self.assertNotIn("--delete", call)
        self.assertIn("audit-pull@guest.invalid:/ ", call)  # rrsync root
        self.assertTrue(call.endswith(str(self.dest) + "/"))
        self.assertIn("StrictHostKeyChecking=yes", call)
        self.assertIn("BatchMode=yes", call)

    def test_script_text_never_deletes_on_the_source_side(self):
        for script in (PULL, STATUS):
            code = re.sub(r"(?m)^\s*#.*$", "", script.read_text())
            self.assertNotIn("--delete", code, script.name)
            self.assertNotRegex(code, r"--remove-source-files", script.name)

    def test_custom_source_path(self):
        self.run_script(PULL, AUDIT_SRC_PATH="/opt/wow-server-metrics/audit/", AUDIT_SRC_USER="u")
        (call,) = self.calls()
        self.assertIn("u@guest.invalid:/opt/wow-server-metrics/audit/ ", call)

    def test_failure_is_visible_and_keeps_old_heartbeat_and_mirror(self):
        touch(self.dest / "AGENT01" / "2026-10-03.jsonl", text="keep\n")
        self.state.mkdir()
        (self.state / "last-success").write_text("1000 old\n")
        r = self.run_script(PULL, FAKE_RSYNC_RC=12)
        self.assertEqual(r.returncode, 12)
        self.assertIn("FAILED", r.stderr)
        self.assertEqual((self.state / "last-success").read_text(), "1000 old\n")
        self.assertIn("rsync_exit=12", (self.state / "last-failure").read_text())
        self.assertEqual((self.dest / "AGENT01" / "2026-10-03.jsonl").read_text(), "keep\n")

    def test_empty_or_rebuilt_source_does_not_wipe_the_mirror(self):
        touch(self.dest / "AGENT01" / "2026-10-02.jsonl", age_s=86400, text="history\n")
        r = self.run_script(PULL)  # fake copies nothing: an empty guest
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.dest / "AGENT01" / "2026-10-02.jsonl").read_text(), "history\n")

    def test_vanished_files_exit_24_counts_as_success(self):
        r = self.run_script(PULL, FAKE_RSYNC_RC=24)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.state / "last-success").exists())

    def test_prunes_mirror_past_retention_only_on_success(self):
        old = self.dest / "AGENT01" / "2026-09-01.jsonl"
        rotated = self.dest / "AGENT01" / "2026-09-01.jsonl.1"
        fresh = self.dest / "AGENT01" / "2026-10-03.jsonl"
        for p, age in ((old, 20 * 86400), (rotated, 20 * 86400), (fresh, 60)):
            touch(p, age_s=age)
        other = self.dest / "AGENT02" / "2026-08-01.jsonl"
        touch(other, age_s=40 * 86400)

        self.assertEqual(self.run_script(PULL, FAKE_RSYNC_RC=12).returncode, 12)
        self.assertTrue(old.exists() and other.exists(), "a failed pull must not prune")

        self.assertEqual(self.run_script(PULL).returncode, 0)
        self.assertFalse(old.exists() or rotated.exists() or other.exists())
        self.assertTrue(fresh.exists())
        self.assertFalse((self.dest / "AGENT02").exists(), "emptied agent dir is removed")
        self.assertTrue(self.dest.exists())

    def test_retention_days_is_configurable_and_validated(self):
        p = self.dest / "AGENT01" / "2026-10-01.jsonl"
        touch(p, age_s=3 * 86400)
        self.assertEqual(self.run_script(PULL, AUDIT_RETENTION_DAYS=2).returncode, 0)
        self.assertFalse(p.exists())
        r = self.run_script(PULL, AUDIT_RETENTION_DAYS="14; rm -rf /")
        self.assertEqual(r.returncode, 64)

    def test_requires_source_host(self):
        env = {k: v for k, v in self.env.items() if k != "AUDIT_SRC_HOST"}
        r = subprocess.run(["bash", str(PULL)], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("AUDIT_SRC_HOST", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_skips_when_another_pull_holds_the_lock(self):
        self.state.mkdir()
        with open(self.state / "lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            r = self.run_script(PULL)
        self.assertEqual(r.returncode, 0)
        self.assertIn("skipping", r.stderr)
        self.assertEqual(self.calls(), [])


class StatusTests(ScriptCase):
    def heartbeat(self, age_s):
        self.state.mkdir(exist_ok=True)
        (self.state / "last-success").write_text(f"{int(time.time() - age_s)} x\n")

    def listing(self, age_s):
        t = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(time.time() - age_s))
        return f"drwxr-xr-x 60 {t} .\n-rw-r--r--  3 {t} AGENT01/2026-10-03.jsonl\n"

    def test_never_pulled_is_stalled(self):
        r = self.run_script(STATUS, SKIP_SOURCE=1)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("PULL STALLED", r.stdout)

    def test_old_heartbeat_is_stalled_even_if_mirror_is_fresh(self):
        touch(self.dest / "AGENT01" / "d.jsonl", age_s=5)
        self.heartbeat(600)
        r = self.run_script(STATUS, SKIP_SOURCE=1)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("PULL STALLED", r.stdout)

    def test_fresh_pull_and_fresh_mirror_is_ok(self):
        touch(self.dest / "AGENT01" / "d.jsonl", age_s=5)
        self.heartbeat(10)
        r = self.run_script(STATUS, SKIP_SOURCE=1)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("VERDICT: OK", r.stdout)

    def test_healthy_pull_and_old_mirror_is_quiet_agents(self):
        touch(self.dest / "AGENT01" / "d.jsonl", age_s=3600)
        self.heartbeat(10)
        r = self.run_script(STATUS, AUDIT_SRC_HOST="guest.invalid", FAKE_RSYNC_LISTING=self.listing(3600))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("VERDICT: QUIET", r.stdout)

    def test_source_newer_than_mirror_is_pull_behind(self):
        touch(self.dest / "AGENT01" / "d.jsonl", age_s=3600)
        self.heartbeat(10)
        r = self.run_script(STATUS, AUDIT_SRC_HOST="guest.invalid", FAKE_RSYNC_LISTING=self.listing(5))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("PULL BEHIND", r.stdout)

    def test_unreachable_source_falls_back_to_heartbeat_verdict(self):
        touch(self.dest / "AGENT01" / "d.jsonl", age_s=3600)
        self.heartbeat(10)
        r = self.run_script(STATUS, AUDIT_SRC_HOST="guest.invalid", FAKE_RSYNC_RC=12)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("source:        unavailable", r.stdout)
        self.assertIn("VERDICT: QUIET", r.stdout)


class UnitFileTests(unittest.TestCase):
    def read(self, name):
        return (SCRIPTS / name).read_text()

    def test_service_fails_visibly(self):
        svc = self.read("wow-audit-pull.service")
        self.assertIn("OnFailure=wow-audit-pull-failed.service", svc)
        self.assertIn("Type=oneshot", svc)
        self.assertIn("scripts/wow-agents/pull-audit.sh", svc)
        self.assertIn("logger", self.read("wow-audit-pull-failed.service"))

    def test_timer_is_30s_and_never_overlaps(self):
        timer = self.read("wow-audit-pull.timer")
        self.assertIn("OnUnitActiveSec=30s", timer)
        self.assertIn("WantedBy=timers.target", timer)


if __name__ == "__main__":
    unittest.main()
