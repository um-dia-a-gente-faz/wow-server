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
# `docker ps` lists FAKE_CONTAINERS only with -a, so a stopped container is seen only by -a.
# Every other call is logged twice: as a line (FAKE_LOG) and as its argv, tab-separated (FAKE_ARGV).
# The realm row (#795): a `SELECT port` prints FAKE_REALM_PORT (default 8086) once the row exists. The
# row is missing for the first FAKE_REALM_MISSING SELECTs (an empty answer), or always when that is
# "never". FAKE_REALM_COUNT counts the SELECTs so far.
STUB_DOCKER = """#!/bin/sh
if [ "$1" = ps ]; then
  if [ "$2" = -a ]; then printf '%s\\n' "$FAKE_CONTAINERS"; fi
  exit 0
fi
printf '%s\\t' "$@" >> "$FAKE_ARGV"; echo >> "$FAKE_ARGV"
echo "docker $*" >> "$FAKE_LOG"
case "$*" in
  *"SELECT port"*)
    [ "$FAKE_REALM_MISSING" = never ] && exit 0
    n=$(cat "$FAKE_REALM_COUNT" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$FAKE_REALM_COUNT"
    [ "$n" -lt "${FAKE_REALM_MISSING:-0}" ] && exit 0
    echo "${FAKE_REALM_PORT:-8086}"
    ;;
esac
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
        self.argv = tmp / "docker.argv"
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_LOG": str(self.log), "FAKE_ARGV": str(self.argv),
                    "FAKE_CONTAINERS": "other-container",
                    "FAKE_REALM_COUNT": str(tmp / "realm.count"),
                    "STAGING_REALMPORT_SLEEP": "0"}   # no waiting between tries in tests

    def run_staging(self, *args, containers="other-container", **env):
        self.env["FAKE_CONTAINERS"] = containers
        self.env.update(env)
        return subprocess.run(["bash", str(self.script), *args], env=self.env,
                              capture_output=True, text=True)

    def realm_selects(self):
        return [a for a in self.argv_calls() if a[:2] == ["exec", "staging-db"] and any("SELECT port" in x for x in a)]

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def argv_calls(self):
        if not self.argv.exists():
            return []
        return [line.rstrip("\t").split("\t") for line in self.argv.read_text().splitlines()]

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

    def test_realmport_sets_the_advertised_port_inside_the_db_container(self):
        self.env_file.write_text("STAGING_PUBLIC_IP=192.0.2.10\nSTAGING_MYSQL_ROOT_PASSWORD=pw-400\n")
        r = self.run_staging("realmport")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(["exec", "staging-db", "sh", "-c",
                       'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e '
                       '"UPDATE auth.realmlist SET port=8086 WHERE id=1"'],
                      self.argv_calls())

    def test_realmport_present_at_once_takes_one_try(self):
        self.env_file.write_text("STAGING_PUBLIC_IP=192.0.2.10\nSTAGING_MYSQL_ROOT_PASSWORD=pw-795\n")
        r = self.run_staging("realmport", FAKE_REALM_MISSING="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.realm_selects()), 1)

    def test_realmport_waits_while_the_row_is_missing(self):
        self.env_file.write_text("STAGING_PUBLIC_IP=192.0.2.10\nSTAGING_MYSQL_ROOT_PASSWORD=pw-795\n")
        r = self.run_staging("realmport", FAKE_REALM_MISSING="3", STAGING_REALMPORT_TRIES="5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.realm_selects()), 4)
        updates = [a for a in self.argv_calls() if any("UPDATE auth.realmlist" in x for x in a)]
        self.assertEqual(len(updates), 4)   # the UPDATE is retried with the read: it lands once the row exists

    def test_realmport_exits_3_when_the_row_never_appears(self):
        self.env_file.write_text("STAGING_PUBLIC_IP=192.0.2.10\nSTAGING_MYSQL_ROOT_PASSWORD=pw-795\n")
        r = self.run_staging("realmport", FAKE_REALM_MISSING="never", STAGING_REALMPORT_TRIES="3")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("never read port 8086 after 3 tries", r.stderr)
        self.assertEqual(len(self.realm_selects()), 3)

    def test_realmport_exits_3_on_the_wrong_port(self):
        self.env_file.write_text("STAGING_PUBLIC_IP=192.0.2.10\nSTAGING_MYSQL_ROOT_PASSWORD=pw-795\n")
        r = self.run_staging("realmport", FAKE_REALM_PORT="8085", STAGING_REALMPORT_TRIES="2")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("8086", r.stderr)

    def test_realmport_defaults_to_30_tries(self):
        self.env_file.write_text("STAGING_PUBLIC_IP=192.0.2.10\nSTAGING_MYSQL_ROOT_PASSWORD=pw-795\n")
        r = self.run_staging("realmport", FAKE_REALM_MISSING="never")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(len(self.realm_selects()), 30)

    def test_up_fails_when_the_realm_never_reads_8086(self):
        self.env_file.write_text("STAGING_PUBLIC_IP=192.0.2.10\nSTAGING_MYSQL_ROOT_PASSWORD=pw-795\n")
        r = self.run_staging("up", FAKE_REALM_MISSING="never", STAGING_REALMPORT_TRIES="2")
        self.assertEqual(r.returncode, 3, r.stderr)

    def test_up_sets_the_realm_port_after_compose_up(self):
        self.env_file.write_text("STAGING_PUBLIC_IP=192.0.2.10\nSTAGING_MYSQL_ROOT_PASSWORD=pw-400\n")
        r = self.run_staging("up")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.argv_calls()
        up = next(i for i, a in enumerate(argv) if a[-2:] == ["up", "-d"])
        port = next(i for i, a in enumerate(argv) if a[:2] == ["exec", "staging-db"])
        self.assertLess(up, port, argv)

    def test_reset_sets_the_realm_port_after_its_up(self):
        self.env_file.write_text("STAGING_PUBLIC_IP=192.0.2.10\nSTAGING_MYSQL_ROOT_PASSWORD=pw-400\n")
        r = self.run_staging("reset")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.argv_calls()
        up = next(i for i, a in enumerate(argv) if a[-2:] == ["up", "-d"])
        port = next(i for i, a in enumerate(argv) if a[:2] == ["exec", "staging-db"])
        self.assertLess(up, port, argv)

    def test_the_root_password_never_reaches_stdout_or_stderr(self):
        password = "pw-400-sentinel"
        self.env_file.write_text(f"STAGING_PUBLIC_IP=192.0.2.10\nSTAGING_MYSQL_ROOT_PASSWORD={password}\n")
        for cmd in ("realmport", "up"):
            with self.subTest(cmd=cmd):
                r = self.run_staging(cmd)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn(password, r.stdout)
                self.assertNotIn(password, r.stderr)
                self.assertNotIn(password, self.argv.read_text())

    def test_unknown_command_is_a_usage_error(self):
        r = self.run_staging("deploy")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stderr)


if __name__ == "__main__":
    unittest.main()
