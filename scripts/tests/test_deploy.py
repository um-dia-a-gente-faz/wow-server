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
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


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

    def stacks_for(self, *paths):
        r = subprocess.run(["bash", str(DEPLOY), "--stacks-for"], input="\n".join(paths),
                           capture_output=True, text=True, check=True)
        return r.stdout.split()

    def test_path_to_stack_mapping(self):
        self.assertEqual(self.stacks_for("docs/X.md", "agent/a.py", "tools/wowmap/x.py"), ["monitoring"])
        self.assertEqual(self.stacks_for("docker-compose.yml"), ["game"])
        self.assertEqual(self.stacks_for("tdb/a.sql", "monitoring/docker-compose.yml"), ["game", "monitoring"])
        self.assertEqual(self.stacks_for("docs/X.md", "scripts/check.sh", "agent/tests/t.py"), [])

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
        calls = self.calls()
        self.assertIn("monitoring/docker-compose.yml up -d --build", calls)
        self.assertNotIn("docker compose up", calls)

    def test_game_deploy_defers_while_players_online(self):
        self.merge("docker-compose.yml")
        before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.work, capture_output=True, text=True).stdout
        r = self.run_deploy(FAKE_ONLINE="3")
        self.assertEqual(r.returncode, 0)
        self.assertIn("DEFERRED", r.stdout)
        self.assertNotIn("up -d", self.calls())
        after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.work, capture_output=True, text=True).stdout
        self.assertEqual(before, after, "checkout must stay put so the next poll retries")

    def test_failed_players_query_defers(self):
        self.merge("docker-compose.yml")
        self.assertIn("DEFERRED", self.run_deploy(FAKE_ONLINE="fail").stdout)

    def test_game_deploy_runs_when_empty_or_forced(self):
        self.merge("docker-compose.yml")
        r = self.run_deploy(FAKE_ONLINE="0")
        self.assertIn("docker compose up -d --build", self.calls(), r.stdout)
        self.assertTrue((self.tmp / "metrics" / "deploy.log").read_text().endswith("stacks: game\n"))

    def test_force_overrides_guard(self):
        self.merge("tdb/a.sql")
        self.run_deploy(FAKE_ONLINE="3", FORCE="1")
        self.assertIn("docker compose up -d --build", self.calls())

    def test_dry_run_changes_nothing(self):
        self.merge("docker-compose.yml")
        r = self.run_deploy(DRY_RUN="1")
        self.assertIn("stacks: game", r.stdout)
        self.assertNotIn("up -d", self.calls())
        self.assertFalse((self.tmp / "metrics").exists())


if __name__ == "__main__":
    unittest.main()
