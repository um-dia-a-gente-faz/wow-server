"""#685: save_roster is atomic. Every test writes a temporary copy, never agents/roster.json."""
import contextlib
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import create_agent_roster  # noqa: E402

class AtomicWriteChecks:
    """#685: the writer is atomic, keeps the file mode and writes the same bytes as before.
    Needs: self.target (a path), self.save(n) (n = 1, 2: different content), self.expected(n)."""

    def listing(self):
        return sorted(os.listdir(os.path.dirname(self.target)))

    def test_bytes_are_the_documented_bytes(self):
        self.save(1)
        with open(self.target, "rb") as f:
            self.assertEqual(f.read(), self.expected(1))

    def _assert_failure_leaves_the_old_file(self, patcher):
        self.save(1)
        with open(self.target, "rb") as f:
            before = f.read()
        names = self.listing()
        with patcher, self.assertRaises(OSError):
            self.save(2)
        with open(self.target, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.listing(), names)

    def test_a_failed_replace_leaves_the_old_file_and_no_temporary(self):
        self._assert_failure_leaves_the_old_file(mock.patch("os.replace", side_effect=OSError("boom")))

    def test_a_failed_fsync_leaves_the_old_file_and_no_temporary(self):
        self._assert_failure_leaves_the_old_file(mock.patch("os.fsync", side_effect=OSError("boom")))

    def test_a_failed_write_leaves_the_old_file_and_no_temporary(self):
        real = os.fdopen

        def fdopen(*a, **k):
            f = real(*a, **k)
            f.write = mock.Mock(side_effect=OSError("disk full"))
            return f
        self._assert_failure_leaves_the_old_file(mock.patch("os.fdopen", fdopen))

    def test_a_failed_fdopen_closes_the_descriptor(self):
        self.save(1)
        closed = []
        real_close = os.close
        with mock.patch("os.fdopen", side_effect=OSError("boom")), \
                mock.patch("os.close", lambda fd: (closed.append(fd), real_close(fd))), \
                self.assertRaises(OSError):
            self.save(2)
        self.assertEqual(len(closed), 1)
        self.assertEqual(self.listing(), [os.path.basename(self.target)])

    def test_a_failed_fdopen_and_a_failing_close_leave_no_temporary(self):
        before = self.listing() if os.path.isdir(os.path.dirname(self.target)) else []
        fds, real_open, real_close = [], os.open, os.close

        def spy(*a, **k):
            fds.append(real_open(*a, **k))
            return fds[-1]

        def close_leaked():
            for fd in fds:
                with contextlib.suppress(OSError):
                    real_close(fd)
        self.addCleanup(close_leaked)
        with mock.patch("os.open", spy), mock.patch("os.fdopen", side_effect=OSError("boom")), \
                mock.patch("os.close", side_effect=OSError("close boom")), self.assertRaises(OSError):
            self.save(1)
        self.assertEqual(self.listing(), before)

    def test_a_planted_file_at_the_temporary_path_is_refused_and_left_alone(self):
        self.save(1)
        with open(self.target, "rb") as f:
            before = f.read()
        directory, name = os.path.split(self.target)
        planted = os.path.join(directory, f".{name}.fixed.tmp")
        with open(planted, "wb") as f:
            f.write(b"planted")
        with mock.patch("secrets.token_hex", return_value="fixed"), self.assertRaises(FileExistsError):
            self.save(2)
        with open(planted, "rb") as f:
            self.assertEqual(f.read(), b"planted")
        with open(self.target, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_the_temporary_of_an_existing_target_is_never_wider_than_it(self):
        self.save(1)
        os.chmod(self.target, 0o600)
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        modes, real_open = [], os.open

        def spy(*a, **k):
            fd = real_open(*a, **k)
            modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
            return fd
        with mock.patch("os.open", spy):
            self.save(2)
        self.assertTrue(modes)
        self.assertEqual([m & ~0o600 for m in modes], [0] * len(modes))
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o600)

    def test_two_concurrent_saves_never_share_a_temporary_name(self):
        self.save(1)
        sources, real = [], os.replace
        barrier = threading.Barrier(2)

        def replace(src, dst):
            sources.append(src)
            real(src, dst)

        def run(n):
            barrier.wait()
            self.save(n)
        with mock.patch("os.replace", replace):
            threads = [threading.Thread(target=run, args=(n,)) for n in (1, 2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(sources), 2)
        self.assertEqual(len(set(sources)), 2)
        with open(self.target, "rb") as f:
            self.assertIn(f.read(), (self.expected(1), self.expected(2)))
        self.assertEqual(self.listing(), [os.path.basename(self.target)])

    def test_an_existing_file_keeps_its_mode(self):
        self.save(1)
        for mode in (0o644, 0o664, 0o600):
            os.chmod(self.target, mode)
            self.save(2)
            self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), mode)

    def test_a_new_file_gets_the_mode_open_would_give_it(self):
        old = os.umask(0o027)
        self.addCleanup(os.umask, old)
        for umask in (0o022, 0o002, 0o077, 0o027, 0o000):
            os.umask(umask)
            if os.path.exists(self.target):
                os.unlink(self.target)
            self.save(1)
            self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o666 & ~umask)
            self.assertEqual(self.listing(), [os.path.basename(self.target)])


def agents(n):
    return [{"account": "AGENT06", "character": f"Name{n}", "race": 2, "class": 1}]


class SaveRosterTest(AtomicWriteChecks, unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = os.path.join(tmp.name, "agents", "roster.json")

    def save(self, n):
        create_agent_roster.save_roster(agents(n), Path(self.target))

    def expected(self, n):
        return (json.dumps({"agents": agents(n)}, indent=2) + "\n").encode()

    def test_the_committed_roster_is_not_the_default_target_of_these_tests(self):
        self.assertNotEqual(Path(self.target), create_agent_roster.ROSTER_PATH)


if __name__ == "__main__":
    unittest.main()
