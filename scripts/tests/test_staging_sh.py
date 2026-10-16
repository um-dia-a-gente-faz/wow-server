"""#400: scripts/staging.sh refuses the game VM (a container named trinitycore-wowserver) and a
missing .env.staging, and drives compose in the right order. Runs the real script from a throwaway
copy with a stub `docker` on PATH, so nothing touches a real container.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
STAGING_SH = ROOT / "scripts" / "staging.sh"
STUB_DOCKER = """#!/bin/sh
if [ "$1" = ps ]; then printf '%s\\n' "$FAKE_CONTAINERS"; exit 0; fi
echo "docker $*" >> "$FAKE_LOG"
exit 0
"""


class StagingShTest(unittest.TestCase):
    def setUp(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        repo = tmp / "repo"
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(STAGING_SH, repo / "scripts" / "staging.sh")
        self.script = repo / "scripts" / "staging.sh"
        self.env_file = repo / ".env.staging"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(STUB_DOCKER)
        docker.chmod(0o755)
        self.log = tmp / "docker.log"
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_LOG": str(self.log), "FAKE_CONTAINERS": "other-container"}

    def run_staging(self, *args, containers="other-container"):
        self.env["FAKE_CONTAINERS"] = containers
        return subprocess.run(["bash", str(self.script), *args], env=self.env,
                              capture_output=True, text=True)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_refuses_on_the_game_vm(self):
        self.env_file.write_text("STAGING_PUBLIC_IP=192.0.2.10\n")
        r = self.run_staging("up", containers="trinitycore-wowserver\nother")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("refusing", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_refusal_also_covers_config(self):
        r = self.run_staging("config", containers="trinitycore-wowserver")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_similar_name_is_not_the_game_vm(self):
        self.env_file.write_text("STAGING_PUBLIC_IP=192.0.2.10\n")
        r = self.run_staging("up", containers="trinitycore-wowserver-old\nstaging-worldserver")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_missing_env_file_refuses_up_down_reset(self):
        for cmd in ("up", "down", "reset"):
            with self.subTest(cmd=cmd):
                r = self.run_staging(cmd)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn(".env.staging", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_config_works_without_env_file(self):
        r = self.run_staging("config")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(" config" in c for c in self.calls()), self.calls())

    def test_reset_is_down_volumes_then_up(self):
        self.env_file.write_text("STAGING_PUBLIC_IP=192.0.2.10\n")
        r = self.run_staging("reset")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        down = next(i for i, c in enumerate(calls) if " down -v" in c)
        up = next(i for i, c in enumerate(calls) if c.endswith(" up -d"))
        self.assertLess(down, up, calls)

    def test_unknown_command_is_a_usage_error(self):
        r = self.run_staging("deploy")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stderr)


if __name__ == "__main__":
    unittest.main()
