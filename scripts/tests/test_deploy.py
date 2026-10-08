"""#266: path-to-stack mapping, players-online guard and no-op deploys of scripts/deploy.sh.

Runs the real script in a throwaway clone with a fake `docker` on PATH; nothing touches a real stack.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts" / "deploy.sh"
FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
[ "$1" = exec ] && { [ "$FAKE_ONLINE" = fail ] && exit 1; echo "$FAKE_ONLINE"; }
exit 0
"""


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


class DeployTest(unittest.TestCase):
    def setUp(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.tmp = tmp
        origin, self.work = tmp / "origin", tmp / "work"
        (origin / "scripts").mkdir(parents=True)
        shutil.copy(DEPLOY, origin / "scripts" / "deploy.sh")
        git(origin, "init", "-q", "-b", "main")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "base")
        git(tmp, "clone", "-q", str(origin), str(self.work))
        self.origin = origin
        (self.work / ".env").write_text("X=1\n")
        bindir = tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text(FAKE_DOCKER)
        (bindir / "docker").chmod(0o755)
        # git wrapper: FAKE_GIT_DIFF_FAIL=<marker path> fails the first `git diff` only (the game
        # one; the monitoring diff then succeeds); everything else is the real git
        (bindir / "git").write_text('#!/bin/sh\n[ "$1" = diff ] && [ -n "$FAKE_GIT_DIFF_FAIL" ] && '
                                    '[ ! -e "$FAKE_GIT_DIFF_FAIL" ] && { : > "$FAKE_GIT_DIFF_FAIL"; exit 128; }\n'
                                    f'exec {shutil.which("git")} "$@"\n')
        (bindir / "git").chmod(0o755)
        self.log = tmp / "docker.log"
        self.env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
                    "FAKE_ONLINE": "0", "METRICS_DIR": str(tmp / "metrics")}

    def merge(self, path):
        f = self.origin / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("change\n")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-qm", f"change {path}")

    def run_deploy(self, **env):
        return subprocess.run(["bash", str(self.work / "scripts" / "deploy.sh")], cwd=self.work,
                              env={**self.env, **env}, capture_output=True, text=True)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def deploy_log(self):
        f = self.tmp / "metrics" / "deploy.log"
        return f.read_text() if f.exists() else ""

    def stacks_for(self, *paths):
        r = subprocess.run(["bash", str(DEPLOY), "--stacks-for"], input="\0".join(paths) + "\0",
                           capture_output=True, text=True, check=True)
        return r.stdout.split()

    def test_path_to_stack_mapping(self):
        self.assertEqual(self.stacks_for("docs/X.md", "agent/a.py", "tools/wowmap/x.py"), ["monitoring"])
        self.assertEqual(self.stacks_for("docker-compose.yml"), ["game"])
        self.assertEqual(self.stacks_for("tdb/a.sql", "monitoring/docker-compose.yml"), ["game", "monitoring"])
        self.assertEqual(self.stacks_for("docs/X.md", "scripts/check.sh", "agent/tests/t.py"), [])
        self.assertEqual(self.stacks_for("tdb/ação.sql"), ["game"])
        self.assertEqual(self.stacks_for("Dockerfile"), [])  # root Dockerfile is the agent image

    def test_docs_only_merge_touches_no_stack(self):
        self.merge("docs/NOTE.md")
        r = self.run_deploy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stacks: none", r.stdout)
        self.assertNotIn("compose up", self.calls())
        self.assertNotIn("exec", self.calls())  # not even the players query

    def test_wowmap_only_merge_skips_game_stack(self):
        self.merge("tools/wowmap/app.py")
        r = self.run_deploy(FAKE_ONLINE="5")  # players online must not matter
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("monitoring/docker-compose.yml up -d --build", self.calls())
        self.assertNotIn("docker compose up", self.calls())

    def test_game_deploy_defers_while_players_online(self):
        base = git(self.work, "rev-parse", "HEAD")
        self.merge("docker-compose.yml")
        r = self.run_deploy(FAKE_ONLINE="3")
        self.assertEqual(r.returncode, 75)
        self.assertIn("DEFERRED", r.stdout)
        self.assertNotIn("up -d", self.calls())
        self.assertIn("DEFERRED game", self.deploy_log())
        self.assertNotIn("deployed", self.deploy_log())
        self.assertEqual(git(self.work, "rev-parse", "refs/deployed/game"), base)

    def test_failed_players_query_defers(self):
        self.merge("docker-compose.yml")
        r = self.run_deploy(FAKE_ONLINE="fail")
        self.assertEqual(r.returncode, 75)
        self.assertIn("DEFERRED", r.stdout)

    def test_deferred_game_still_deploys_monitoring_and_stays_pending(self):
        self.merge("docker-compose.yml")
        self.merge("tools/wowmap/app.py")
        self.assertEqual(self.run_deploy(FAKE_ONLINE="3").returncode, 75)
        self.assertIn("monitoring/docker-compose.yml up -d --build", self.calls())
        self.assertNotIn("docker compose up", self.calls())
        self.assertIn("deployed", self.deploy_log())
        self.merge("docs/NOTE.md")  # a later merge must not lose the pending game change
        self.assertEqual(self.run_deploy(FAKE_ONLINE="3").returncode, 75)
        r = self.run_deploy(FAKE_ONLINE="0")  # players gone: the game stack finally deploys
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("docker compose up -d --build", self.calls())
        self.assertIn("stacks: none", self.run_deploy().stdout)  # nothing pending any more

    def test_deferred_polls_deploy_monitoring_once(self):
        self.merge("docker-compose.yml")
        self.merge("tools/wowmap/app.py")
        for _ in range(2):  # two cron polls while the game stack stays deferred
            self.assertEqual(self.run_deploy(FAKE_ONLINE="3").returncode, 75)
        self.assertEqual(self.calls().count("monitoring/docker-compose.yml up -d --build"), 1)
        self.assertEqual(self.deploy_log().count("deployed"), 1)

    def test_failing_git_diff_aborts_before_recording_a_deploy(self):
        self.run_deploy()  # creates refs/deployed/* at the base commit
        base = git(self.work, "rev-parse", "HEAD")
        self.merge("docker-compose.yml")
        r = self.run_deploy(FAKE_GIT_DIFF_FAIL=str(self.tmp / "diff-failed"))
        self.assertNotIn(r.returncode, (0, 75), r.stdout)
        self.assertNotIn("up -d", self.calls())
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), base)
        self.assertEqual(git(self.work, "rev-parse", "refs/deployed/game"), base)
        self.assertEqual(git(self.work, "rev-parse", "refs/deployed/monitoring"), base)

    def test_game_deploy_runs_when_empty(self):
        self.merge("docker-compose.yml")
        r = self.run_deploy(FAKE_ONLINE="0")
        self.assertIn("docker compose up -d --build", self.calls(), r.stdout)
        self.assertTrue(self.deploy_log().endswith("stacks: game\n"))

    def test_force_overrides_guard(self):
        self.merge("tdb/a.sql")
        self.assertEqual(self.run_deploy(FAKE_ONLINE="3", FORCE="1").returncode, 0)
        self.assertIn("docker compose up -d --build", self.calls())

    def test_rename_out_of_game_path_counts_as_game(self):
        self.merge("tdb/a.sql")
        self.run_deploy()
        (self.origin / "docs").mkdir()
        git(self.origin, "mv", "tdb/a.sql", "docs/a.sql")
        git(self.origin, "commit", "-qm", "rename")
        self.assertIn("stacks: game", self.run_deploy(DRY_RUN="1").stdout)

    def test_deleted_game_file_counts_as_game(self):
        self.merge("tdb/a.sql")
        self.run_deploy()
        git(self.origin, "rm", "-q", "tdb/a.sql")
        git(self.origin, "commit", "-qm", "delete")
        self.assertIn("stacks: game", self.run_deploy(DRY_RUN="1").stdout)

    def test_non_ascii_game_path(self):
        self.merge("tdb/ação.sql")
        self.assertIn("stacks: game", self.run_deploy(DRY_RUN="1").stdout)

    def test_all_deploys_both_stacks_without_a_diff(self):
        r = self.run_deploy(ALL="1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("docker compose up -d --build", self.calls())
        self.assertIn("monitoring/docker-compose.yml up -d --build", self.calls())

    def test_combined_game_and_monitoring_deferral(self):
        self.merge("docker-compose.yml")
        self.merge("monitoring/docker-compose.yml")
        self.assertEqual(self.run_deploy(FAKE_ONLINE="2").returncode, 75)
        self.assertIn("monitoring/docker-compose.yml up -d --build", self.calls())
        self.assertNotIn("docker compose up", self.calls())
        self.assertIn("stacks: monitoring", self.deploy_log())

    def test_dry_run_changes_nothing(self):
        self.merge("docker-compose.yml")
        r = self.run_deploy(DRY_RUN="1", FAKE_ONLINE="3")
        self.assertIn("stacks: game", r.stdout)
        self.assertNotIn("up -d", self.calls())
        self.assertFalse((self.tmp / "metrics").exists())


if __name__ == "__main__":
    unittest.main()
