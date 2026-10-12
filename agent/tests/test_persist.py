"""agent/persist.py (ADR 0018, slice 1): atomic write, versioned read, set_aside."""
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from agent import persist


def _write(d, content, name="f.json"):
    path = os.path.join(d, name)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode) as f:
        f.write(content)
    return path


class WriteAtomicTests(unittest.TestCase):
    def test_creates_directory_and_writes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a", "b", "f.json")
            persist.write_atomic(path, "hello")
            with open(path) as f:
                self.assertEqual(f.read(), "hello")
            self.assertEqual(os.listdir(os.path.dirname(path)), ["f.json"])

    def test_fsync_runs_before_replace(self):
        calls = []
        real_fsync, real_replace = os.fsync, os.replace

        def spy_fsync(fd):
            calls.append("fsync")
            real_fsync(fd)

        def spy_replace(a, b):
            calls.append("replace")
            real_replace(a, b)

        with (tempfile.TemporaryDirectory() as d,
              mock.patch("os.fsync", spy_fsync), mock.patch("os.replace", spy_replace)):
            persist.write_atomic(os.path.join(d, "f.json"), "x")
        self.assertEqual(calls, ["fsync", "replace"])

    def test_failure_keeps_old_content_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.json")
            persist.write_atomic(path, "old")
            for target in ("os.fsync", "os.replace"):
                with self.subTest(target):
                    with mock.patch(target, side_effect=OSError("boom")), self.assertRaises(OSError):
                        persist.write_atomic(path, "new")
                    with open(path) as f:
                        self.assertEqual(f.read(), "old")
                    self.assertEqual(os.listdir(d), ["f.json"])

    def test_temp_name_differs_per_thread(self):
        names = []
        real_replace = os.replace

        def spy(src, dst):
            names.append(src)
            real_replace(src, dst)

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.json")
            with mock.patch("os.replace", spy):
                persist.write_atomic(path, "x")
                t = threading.Thread(target=persist.write_atomic, args=(path, "y"))
                t.start()
                t.join()
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1])


class ReadVersionedTests(unittest.TestCase):
    def test_missing(self):
        with tempfile.TemporaryDirectory() as d:
            r = persist.read_versioned(os.path.join(d, "nope.json"), 1)
        self.assertEqual((r.status, r.data), (persist.MISSING, {}))

    def test_ok_without_schema_is_version_1(self):
        with tempfile.TemporaryDirectory() as d:
            r = persist.read_versioned(_write(d, '{"a": 1}'), 1)
        self.assertEqual((r.status, r.version, r.data), (persist.OK, 1, {"a": 1}))

    def test_ok_with_schema_strips_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            r = persist.read_versioned(_write(d, '{"schema": 2, "a": 1}'), 2)
        self.assertEqual((r.status, r.version, r.data), (persist.OK, 2, {"a": 1}))

    def test_older(self):
        with tempfile.TemporaryDirectory() as d:
            r = persist.read_versioned(_write(d, '{"schema": 1, "a": 1}'), 2)
            r2 = persist.read_versioned(_write(d, '{"a": 1}', "g.json"), 2)
        self.assertEqual((r.status, r.version, r.data), (persist.OLDER, 1, {"a": 1}))
        self.assertEqual(r2.status, persist.OLDER)

    def test_newer_has_no_data(self):
        with tempfile.TemporaryDirectory() as d:
            r = persist.read_versioned(_write(d, '{"schema": 3, "a": 1}'), 2)
        self.assertEqual((r.status, r.version, r.data), (persist.NEWER, 3, {}))

    def test_unreadable_kinds(self):
        cases = {
            "empty": "",
            "truncated": '{"a": {"b',
            "invalid json": "not json",
            "list": "[]",
            "string": '"x"',
            "string schema": '{"schema": "x"}',
            "float schema": '{"schema": 1.0}',
            "bool schema": '{"schema": true}',
            "null schema": '{"schema": null}',
            "zero schema": '{"schema": 0}',
            "negative schema": '{"schema": -1}',
            "deep": "[" * 100000 + "]" * 100000,
            "bad utf8": b'{"a": "\xff\xfe"}',
        }
        with tempfile.TemporaryDirectory() as d:
            for label, content in cases.items():
                with self.subTest(label):
                    r = persist.read_versioned(_write(d, content), 1)
                    self.assertEqual((r.status, r.data), (persist.UNREADABLE, {}))

    def test_io_error_other_than_missing_is_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(persist.read_versioned(d, 1).status, persist.UNREADABLE)  # a directory
            under_file = os.path.join(_write(d, "x"), "sub")  # NotADirectoryError
            self.assertEqual(persist.read_versioned(under_file, 1).status, persist.UNREADABLE)


class WriteVersionedTests(unittest.TestCase):
    def test_schema_sits_at_top_level_next_to_the_payload(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.json")
            persist.write_versioned(path, 3, {"a": {"1": 2}})
            with open(path) as f:
                self.assertEqual(json.load(f), {"schema": 3, "a": {"1": 2}})
            self.assertEqual(persist.read_versioned(path, 3).status, persist.OK)

    def test_body_with_schema_key_raises_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.json")
            with self.assertRaises(ValueError):
                persist.write_versioned(path, 1, {"schema": 9})
            self.assertEqual(os.listdir(d), [])
            persist.write_atomic(path, "old")
            with self.assertRaises(ValueError):
                persist.write_versioned(path, 1, {"schema": 9})
            with open(path) as f:
                self.assertEqual(f.read(), "old")


class SetAsideTests(unittest.TestCase):
    def test_moves_and_replaces_older_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, "one")
            persist.set_aside(path)
            self.assertFalse(os.path.exists(path))
            _write(d, "two")
            persist.set_aside(path)
            with open(path + ".corrupt") as f:
                self.assertEqual(f.read(), "two")

    def test_swallows_oserror(self):
        with tempfile.TemporaryDirectory() as d:
            persist.set_aside(os.path.join(d, "missing"))  # nothing to move
            path = _write(d, "x")
            os.mkdir(path + ".corrupt")
            persist.set_aside(path)  # replacing a directory with a file fails
            self.assertTrue(os.path.exists(path))

    def test_swallows_a_path_the_os_rejects(self):
        persist.set_aside("/tmp/a\x00b")   # os.replace raises ValueError here, not OSError


if __name__ == "__main__":
    unittest.main()
