"""#589: scripts/auto-deploy.sh poll paths, run in a throwaway clone with a stub scripts/deploy.sh.

The lock file comes from AUTO_DEPLOY_LOCK (a temp file here), so nothing touches /tmp/wow-auto-deploy.lock
or a real stack. The stub deploy.sh exits with STUB_RC and appends a line to STUB_RAN when it runs.
"""
import fcntl
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
AUTO_DEPLOY = ROOT / "scripts" / "auto-deploy.sh"
STUB_DEPLOY = """#!/usr/bin/env bash
echo ran >> "$STUB_RAN"
exit "$STUB_RC"
"""
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


@unittest.skipUnless(shutil.which("flock"), "flock not on PATH")
class AutoDeployTest(unittest.TestCase):
    def setUp(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.lock = tmp / "auto-deploy.lock"
        self.ran = tmp / "deploy-ran.log"
        origin, clone, pusher = tmp / "origin.git", tmp / "clone", tmp / "pusher"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        git(tmp, "clone", "-q", str(origin), str(clone))
        (clone / "scripts").mkdir()
        shutil.copy(AUTO_DEPLOY, clone / "scripts" / "auto-deploy.sh")
        (clone / "scripts" / "deploy.sh").write_text(STUB_DEPLOY)
        os.chmod(clone / "scripts" / "deploy.sh", 0o755)
        git(clone, "add", "-A")
        git(clone, "commit", "-qm", "base")
        git(clone, "push", "-q", "origin", "HEAD:main")
        self.clone = clone
        self.pusher = pusher
        self.origin = origin
        self.git_dir = tmp

    def move_origin(self):
        """Push a new commit to origin/main from a second clone, so the test clone falls behind."""
        git(self.git_dir, "clone", "-q", str(self.origin), str(self.pusher))
        (self.pusher / "change.txt").write_text("change\n")
        git(self.pusher, "add", "-A")
        git(self.pusher, "commit", "-qm", "change")
        git(self.pusher, "push", "-q", "origin", "HEAD:main")

    def run_auto_deploy(self, rc=0, lock_held=False):
        """Run the script once. lock_held: the test holds flock on the lock file meanwhile."""
        env = {**os.environ, "AUTO_DEPLOY_LOCK": str(self.lock), "STUB_RC": str(rc), "STUB_RAN": str(self.ran)}
        fd = None
        if lock_held:
            fd = os.open(self.lock, os.O_CREAT | os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            return subprocess.run(["bash", str(self.clone / "scripts" / "auto-deploy.sh")],
                                  capture_output=True, text=True, env=env, cwd=self.clone)
        finally:
            if fd is not None:
                os.close(fd)

    def deploy_ran(self):
        return self.ran.exists()

    def test_up_to_date_exits_zero_and_does_not_deploy(self):
        r = self.run_auto_deploy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("up to date", r.stdout)
        self.assertFalse(self.deploy_ran())

    def test_origin_moved_runs_deploy_and_reports_done(self):
        self.move_origin()
        r = self.run_auto_deploy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.deploy_ran())
        self.assertIn("deploy done", r.stdout)

    def test_deferred_game_stack_exit_75_is_success_and_says_deferred(self):
        self.move_origin()
        r = self.run_auto_deploy(rc=75)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.deploy_ran())
        self.assertIn("deferred", r.stdout)
        self.assertNotIn("deploy done", r.stdout)

    def test_other_deploy_failure_is_passed_through(self):
        self.move_origin()
        r = self.run_auto_deploy(rc=3)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertTrue(self.deploy_ran())

    def test_held_lock_skips_the_run(self):
        self.move_origin()
        r = self.run_auto_deploy(lock_held=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("another auto-deploy run in progress", r.stdout)
        self.assertFalse(self.deploy_ran())


if __name__ == "__main__":
    unittest.main()
