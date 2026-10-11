import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import backups  # noqa: E402


class BackupStatsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)

    def make(self, name, size, mtime):
        p = self.dir / name
        p.write_bytes(b"x" * size)
        os.utime(p, (mtime, mtime))
        return p

    def test_missing_directory_is_zero_backups(self):
        self.assertEqual(backups.backup_stats(self.dir / "nope"), (0, 0.0, 0.0))

    def test_empty_directory_is_zero_backups(self):
        self.assertEqual(backups.backup_stats(self.dir), (0, 0.0, 0.0))

    def test_one_backup(self):
        self.make("auth-20261010T030000.sql.gz", 123, 1000)
        self.assertEqual(backups.backup_stats(self.dir), (1, 1000.0, 123.0))

    def test_newest_by_mtime_not_name_or_size(self):
        self.make("characters-a.sql.gz", 900, 1000)
        self.make("auth-z.sql.gz", 50, 3000)
        self.make("characters-z.sql.gz", 700, 2000)
        self.assertEqual(backups.backup_stats(self.dir), (3, 3000.0, 50.0))

    def test_ignores_part_files_other_files_and_directories(self):
        self.make("auth-a.sql.gz", 10, 1000)
        self.make("auth-b.sql.gz.part", 99, 5000)
        self.make("notes.txt", 99, 6000)
        (self.dir / "dir.sql.gz").mkdir()
        self.assertEqual(backups.backup_stats(self.dir), (1, 1000.0, 10.0))

    def test_unreadable_entry_is_skipped(self):
        self.make("auth-a.sql.gz", 10, 1000)
        (self.dir / "dangling.sql.gz").symlink_to(self.dir / "gone")
        self.assertEqual(backups.backup_stats(self.dir), (1, 1000.0, 10.0))

    def test_invalid_directory_does_not_raise(self):
        self.assertEqual(backups.backup_stats("\0"), (0, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
