"""backup-db.sh / restore-db.sh against a fake `docker` on PATH (no MySQL needed)."""
import gzip
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
# Fake docker: `exec [-i] C sh -c CMD sh DB`. Dump prints a SQL line; mysql appends stdin to $FAKE_LOG.stdin.
FAKE = r"""#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
[ "$2" = -i ] && cat >> "$FAKE_LOG.stdin"
case "$*" in *mysqldump*) echo "INSERT INTO t VALUES (1);";; esac
exit 0
"""


class BackupRestore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "bin").mkdir()
        d = self.tmp / "bin" / "docker"
        d.write_text(FAKE)
        d.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}",
                    "FAKE_LOG": str(self.tmp / "log"), "BACKUP_DIR": str(self.tmp / "b")}

    def run_sh(self, name, *args, **env):
        return subprocess.run(["bash", str(SCRIPTS / name), *args], env={**self.env, **env},
                              capture_output=True, text=True)

    def test_backup_roundtrip_and_prune(self):
        b = self.tmp / "b"
        b.mkdir()
        old = b / "auth-19700101T000000.sql.gz"
        old.write_bytes(b"x")
        os.utime(old, (1, 1))
        r = self.run_sh("backup-db.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        names = sorted(p.name.split("-")[0] for p in b.glob("*.sql.gz"))
        self.assertEqual(names, ["auth", "characters"])  # the old one was pruned
        f = next(b.glob("characters-*.sql.gz"))
        self.assertIn("INSERT INTO t", gzip.open(f, "rt").read())
        self.assertFalse(list(b.glob("*.part")))

        r = self.run_sh("restore-db.sh", "--dry-run", str(f))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("database 'characters'", r.stdout)
        r = self.run_sh("restore-db.sh", str(f), DB_CONTAINER="scratch")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("INSERT INTO t", (self.tmp / "log.stdin").read_text())

    def test_restore_refuses_live_without_confirm(self):
        f = self.tmp / "auth-1.sql.gz"
        f.write_bytes(gzip.compress(b"select 1;"))
        r = self.run_sh("restore-db.sh", str(f))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("CONFIRM_LIVE", r.stderr)

    def test_backup_failure_leaves_no_file(self):
        (self.tmp / "bin" / "docker").write_text("#!/usr/bin/env bash\nexit 1\n")
        r = self.run_sh("backup-db.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(list((self.tmp / "b").glob("*.sql.gz")))


if __name__ == "__main__":
    unittest.main()
