"""agent.fleetlimits (ADR 0017, #625): the fleet limits file. `until_ts` is the only
expiry; every bad input fails open (None) and is logged once, not once per cycle."""

import builtins
import json
import logging
import os
import tempfile
import time
import unittest
from datetime import UTC, datetime
from unittest import mock

from agent import fleetlimits as fl

NOW = 1_700_000_000.0


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class FleetLimitsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "limits.json")
        self.clock = Clock()

    def write(self, data, path=None):
        path = path or self.path
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else json.dumps(data).encode())

    def replace(self, data):
        tmp = os.path.join(self.dir, "limits.tmp")
        self.write(data, tmp)
        os.replace(tmp, self.path)

    def limits(self):
        return fl.FleetLimits(self.path, self.clock)

    def doc(self, until=NOW + 30, reason="fleet Jev budget spent", brain="jev", **kw):
        return {"version": 1, "blocks": {brain: {"until_ts": until, "reason": reason}}, **kw}

    def test_exported_names(self):
        self.assertEqual(fl.SCHEMA_VERSION, 1)
        self.assertEqual(fl.MAX_BYTES, 65536)

    def test_active_block_is_reported_and_expires(self):
        self.write(self.doc())
        lim = self.limits()
        iso = datetime.fromtimestamp(NOW + 30, tz=UTC).isoformat(timespec="seconds")
        self.assertEqual(lim.blocked("jev"), f"jev blocked by fleet limits until {iso}: fleet Jev budget spent")
        self.assertIsNone(lim.blocked("llm"))   # a block names one brain
        self.clock.t += 31
        self.assertIsNone(lim.blocked("jev"))

    def test_expiry_is_exactly_until_ts(self):
        self.write(self.doc())
        lim = self.limits()
        self.clock.t = NOW + 29.999
        self.assertIsNotNone(lim.blocked("jev"))
        self.clock.t = NOW + 30   # at until_ts: no longer blocked
        self.assertIsNone(lim.blocked("jev"))

    def test_written_ts_is_never_read(self):
        for extra in ({"written_ts": NOW - 86400}, {"written_ts": NOW + 86400}, {"written_ts": "x"}, {}):
            with self.subTest(extra=extra):
                self.replace(self.doc(**extra))
                self.assertIsNotNone(self.limits().blocked("jev"))

    def test_reason_is_cut_to_200_characters_and_one_line(self):
        self.write(self.doc(reason="a\nb" + "x" * 500))
        msg = self.limits().blocked("jev")
        assert msg is not None
        reason = msg.split(": ", 1)[1]
        self.assertEqual(len(reason), 200)
        self.assertNotIn("\n", msg)

    def test_missing_or_odd_reason_still_blocks(self):
        for entry in ({"until_ts": NOW + 30}, {"until_ts": NOW + 30, "reason": 7}):
            with self.subTest(entry=entry):
                self.replace({"version": 1, "blocks": {"jev": entry}})
                self.assertIn("jev blocked by fleet limits", self.limits().blocked("jev") or "")

    def test_unknown_keys_are_ignored(self):
        self.write({"version": 1, "extra": [1], "blocks": {"other": {"until_ts": NOW + 9},
                    "jev": {"until_ts": NOW + 9, "reason": "r", "x": 1}}})
        self.assertIsNotNone(self.limits().blocked("jev"))

    def test_bad_inputs_fail_open(self):
        blocks = self.doc()["blocks"]
        cases = {
            "missing": None,
            "directory": "dir",
            "70kB": json.dumps(self.doc(reason="x" * 70000)).encode(),
            "invalid json": b'{"version": 1, "blocks": {',
            "not utf8": b"\xff\xfe\x00",
            "empty": b"",
            "top-level list": [1, 2],
            "top-level null": b"null",
            "version 2": {"version": 2, "blocks": blocks},
            "version missing": {"blocks": blocks},
            "version true": {"version": True, "blocks": blocks},
            "version string": {"version": "1", "blocks": blocks},
            "until_ts soon": self.doc(until="soon"),
            "until_ts null": self.doc(until=None),
            "until_ts true": self.doc(until=True),
            "until_ts NaN": b'{"version":1,"blocks":{"jev":{"until_ts":NaN}}}',
            "until_ts Infinity": b'{"version":1,"blocks":{"jev":{"until_ts":Infinity}}}',
            "until_ts huge int": b'{"version":1,"blocks":{"jev":{"until_ts":1' + b"0" * 400 + b"}}}",
            "blocks list": {"version": 1, "blocks": [blocks]},
            "blocks null": {"version": 1, "blocks": None},
            "blocks missing": {"version": 1},
            "entry list": {"version": 1, "blocks": {"jev": [1]}},
            "entry string": {"version": 1, "blocks": {"jev": "now"}},
            "deeply nested": b"[" * 40000 + b"]" * 40000,
            "deeply nested objects": b'{"a":' * 12000 + b"1" + b"}" * 12000,
        }
        for name, content in cases.items():
            with self.subTest(name):
                if os.path.isdir(self.path):
                    os.rmdir(self.path)
                elif os.path.exists(self.path):
                    os.remove(self.path)
                if content == "dir":
                    os.mkdir(self.path)
                elif content is not None:
                    self.write(content)
                lim = self.limits()
                self.assertIsNone(lim.blocked("jev"))
                self.assertIsNone(lim.blocked("llm"))

    def test_unreadable_permissions_fail_open(self):
        self.write(self.doc())
        os.chmod(self.path, 0)
        self.addCleanup(os.chmod, self.path, 0o600)
        if os.access(self.path, os.R_OK):
            self.skipTest("running as a user that ignores file modes")
        self.assertIsNone(self.limits().blocked("jev"))

    def test_a_bad_entry_does_not_hide_a_good_one(self):
        self.write({"version": 1, "blocks": {"jev": {"until_ts": "soon"}, "llm": {"until_ts": NOW + 5, "reason": "r"}}})
        lim = self.limits()
        self.assertIsNone(lim.blocked("jev"))
        self.assertIsNotNone(lim.blocked("llm"))

    def test_unexpected_error_in_the_reader_fails_open(self):
        self.write(self.doc())
        real_stat = os.stat

        def stat(path, *a, **kw):
            if path == self.path:
                raise RuntimeError("boom")
            return real_stat(path, *a, **kw)

        with mock.patch.object(fl.os, "stat", stat), self.assertLogs("agent.fleetlimits", logging.ERROR):
            self.assertIsNone(self.limits().blocked("jev"))

    def test_file_is_read_again_only_when_it_changed(self):
        self.write(self.doc(reason="first"))
        lim = self.limits()
        real_open = builtins.open
        opens = []

        def counting(file, *a, **kw):
            if file == self.path:
                opens.append(file)
            return real_open(file, *a, **kw)

        with mock.patch("builtins.open", counting):
            for _ in range(3):
                self.assertIn("first", lim.blocked("jev") or "")
            self.assertEqual(len(opens), 1)
            self.replace(self.doc(reason="second"))   # rename, not an in-place write
            self.assertIn("second", lim.blocked("jev") or "")
            self.assertEqual(len(opens), 2)

    def test_replacement_with_same_size_and_mtime_is_seen(self):
        self.write(self.doc(reason="aaaa"))
        st = os.stat(self.path)
        lim = self.limits()
        self.assertIn("aaaa", lim.blocked("jev") or "")
        self.replace(self.doc(reason="bbbb"))
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns))   # same size, same mtime, new inode
        self.assertIn("bbbb", lim.blocked("jev") or "")

    def test_one_stat_per_call_and_no_handle_kept(self):
        self.write(self.doc())
        lim = self.limits()
        lim.blocked("jev")
        with mock.patch.object(fl.os, "stat", wraps=os.stat) as st:
            lim.blocked("jev")
        self.assertEqual(st.call_count, 1)
        self.assertFalse([v for v in vars(lim).values() if hasattr(v, "read")])

    def test_block_removed_or_file_deleted_unblocks(self):
        self.write(self.doc())
        lim = self.limits()
        self.assertIsNotNone(lim.blocked("jev"))
        self.replace({"version": 1, "blocks": {}})
        self.assertIsNone(lim.blocked("jev"))
        self.replace(self.doc())
        self.assertIsNotNone(lim.blocked("jev"))
        os.remove(self.path)
        self.assertIsNone(lim.blocked("jev"))

    def test_a_problem_is_logged_once_and_again_after_it_clears(self):
        lim = self.limits()   # file missing
        with self.assertLogs("agent.fleetlimits", logging.WARNING) as cm:
            for _ in range(20):
                lim.blocked("jev")
        self.assertEqual(len(cm.records), 1)
        self.assertNotIn("\n", cm.records[0].getMessage())
        self.write(self.doc())
        lim.blocked("jev")   # ok again
        os.remove(self.path)
        with self.assertLogs("agent.fleetlimits", logging.WARNING) as cm:
            lim.blocked("jev")
        self.assertEqual(len(cm.records), 1)

    def test_a_warning_is_bounded_and_does_not_echo_the_file(self):
        self.write(b'{"secret-looking": "' + b"x" * 5000 + b'", ')
        with self.assertLogs("agent.fleetlimits", logging.WARNING) as cm:
            self.limits().blocked("jev")
        msg = cm.records[0].getMessage()
        self.assertLess(len(msg), 400)
        self.assertNotIn("secret-looking", msg)

    def test_a_healthy_file_logs_nothing(self):
        self.write(self.doc())
        lim = self.limits()
        with self.assertNoLogs("agent.fleetlimits", logging.WARNING):
            for _ in range(5):
                lim.blocked("jev")
            self.clock.t += 100
            lim.blocked("jev")

    def test_default_clock_is_wall_time(self):
        self.write(self.doc(until=time.time() + 60))
        self.assertIsNotNone(fl.FleetLimits(self.path).blocked("jev"))


if __name__ == "__main__":
    unittest.main()
