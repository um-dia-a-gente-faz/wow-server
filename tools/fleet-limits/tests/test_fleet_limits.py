"""tools/fleet-limits/fleet_limits.py (#745): the pure core, the shell and the contract with the
reader, agent/fleetlimits.py. Files are written only under temporary directories."""
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
TOOL = ROOT / "tools" / "fleet-limits"
_saved_path = list(sys.path)
sys.path[:0] = [str(TOOL), str(ROOT)]
try:
    import fleet_limits as fl
    from agent.fleetlimits import FleetLimits   # the TEST may import the reader; the tool may not
finally:
    sys.path[:] = _saved_path   # leave sys.path as it was

NOW = 1_700_000_000.0


def jev(ts, i=100, o=50):
    return {"ts": ts, "brain": "jev", "usage": {"input_tokens": i, "output_tokens": o},
            "prompt_tokens": i, "completion_tokens": o}


def llm(ts, p=70, c=30):
    return {"ts": ts, "brain": "llm", "prompt_tokens": p, "completion_tokens": c, "usage": {}}


def compute(records, jev_cap=0, llm_cap=0, owner=None, now=NOW, warn=None):
    return fl.compute(records, now, jev_cap, llm_cap, owner, *([warn] if warn else []))


class Sums(unittest.TestCase):
    def test_jev_sums_usage_and_llm_only_counts_llm_records(self):
        recs = [jev(NOW - 5), jev(NOW - 6, 1, 2), llm(NOW - 7), llm(NOW - 8, 5, 5)]
        # the Jev record also carries prompt/completion_tokens: they must not count as LLM tokens
        self.assertEqual(fl.hour_sums(recs, NOW), (150 + 3, 100 + 10))

    def test_llm_tokens_are_not_counted_for_jev_records(self):
        self.assertEqual(fl.hour_sums([llm(NOW - 1)], NOW), (0, 100))
        self.assertEqual(fl.hour_sums([jev(NOW - 1)], NOW), (150, 0))

    def test_window_edges(self):
        self.assertEqual(fl.hour_sums([jev(NOW - 3600)], NOW)[0], 0, "exactly 3600 s old is out")
        self.assertEqual(fl.hour_sums([jev(NOW - 3599.5)], NOW)[0], 150)
        self.assertEqual(fl.hour_sums([jev(NOW)], NOW)[0], 150, "ts == now is in")
        self.assertEqual(fl.hour_sums([jev(NOW + 0.5)], NOW)[0], 0, "future is out")

    def test_bad_records_are_skipped_never_an_error(self):
        good = jev(NOW - 1)
        bad = [
            "a line", 7, None, [1], {},
            {"usage": {"input_tokens": 9, "output_tokens": 9}},                      # no ts
            {"ts": "1700000000", "usage": {"input_tokens": 9}},                      # string ts
            {"ts": float("nan"), "usage": {"input_tokens": 9}},
            {"ts": float("inf"), "usage": {"input_tokens": 9}},
            {"ts": True, "usage": {"input_tokens": 9}},
            {"ts": NOW - 1, "usage": {"input_tokens": "99", "output_tokens": 1}},   # string tokens
            {"ts": NOW - 1, "usage": {"input_tokens": float("nan"), "output_tokens": 1}},
            {"ts": NOW - 1, "usage": {"input_tokens": -5, "output_tokens": 1}},
            {"ts": NOW - 1, "usage": "x"},
            {"ts": NOW - 1, "brain": "llm", "prompt_tokens": "7", "completion_tokens": 1},
            {"ts": NOW - 1, "brain": "llm", "prompt_tokens": None, "completion_tokens": None},
        ]
        self.assertEqual(fl.hour_sums(bad + [good], NOW), (150, 0))

    def test_a_missing_token_field_counts_as_zero(self):
        self.assertEqual(fl.hour_sums([{"ts": NOW, "usage": {"input_tokens": 40}}], NOW), (40, 0))


class Blocks(unittest.TestCase):
    def test_cap_zero_is_off(self):
        self.assertEqual(compute([jev(NOW - 1, 10**9, 10**9), llm(NOW - 1, 10**9, 10**9)])["blocks"], {})

    def test_at_the_cap_blocked_one_below_not(self):
        recs = [jev(NOW - 1)]   # 150 Jev tokens
        self.assertIn("jev", compute(recs, jev_cap=150)["blocks"])
        self.assertNotIn("jev", compute(recs, jev_cap=151)["blocks"])
        recs = [llm(NOW - 1)]   # 100 LLM tokens
        self.assertIn("llm", compute(recs, llm_cap=100)["blocks"])
        self.assertNotIn("llm", compute(recs, llm_cap=101)["blocks"])

    def test_shape_until_ts_and_reason(self):
        doc = compute([jev(NOW - 1), llm(NOW - 1)], jev_cap=100, llm_cap=50)
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["written_ts"], NOW)
        self.assertEqual(doc["blocks"]["jev"], {
            "until_ts": NOW + 30, "reason": "fleet jev budget spent: 150 of 100 tokens in the last hour"})
        self.assertEqual(doc["blocks"]["llm"]["until_ts"], NOW + 30)
        self.assertEqual(doc["blocks"]["llm"]["reason"], "fleet llm budget spent: 100 of 50 tokens in the last hour")

    def test_each_brain_has_its_own_cap(self):
        doc = compute([jev(NOW - 1), llm(NOW - 1)], jev_cap=100)
        self.assertEqual(sorted(doc["blocks"]), ["jev"])


class OwnerStop(unittest.TestCase):
    def test_owner_wins_over_a_computed_block_and_keeps_its_until_ts(self):
        owner = {"jev": {"until_ts": NOW + 86400, "reason": "maintenance"}}
        doc = compute([jev(NOW - 1)], jev_cap=100, owner=owner)
        self.assertEqual(doc["blocks"]["jev"], {"until_ts": NOW + 86400, "reason": "maintenance"})

    def test_owner_block_alone_and_alongside_a_computed_one(self):
        doc = compute([llm(NOW - 1)], llm_cap=10, owner={"jev": {"until_ts": NOW + 500, "reason": "r"}})
        self.assertEqual(sorted(doc["blocks"]), ["jev", "llm"])
        self.assertEqual(doc["blocks"]["llm"]["until_ts"], NOW + 30)

    def test_invalid_entries_are_dropped_with_a_warning(self):
        warned = []
        owner = {
            "jev": {"until_ts": "soon", "reason": "x"},
            "llm": {"until_ts": float("inf")},
            "other": {"until_ts": NOW + 100, "reason": "x"},
            "bad": "stop",
        }
        doc = compute([], owner=owner, warn=lambda key, msg, exc_info=False: warned.append(key))
        self.assertEqual(doc["blocks"], {})
        self.assertEqual(sorted(warned), ["owner-entry-bad", "owner-entry-jev", "owner-entry-llm", "owner-entry-other"])

    def test_bool_nan_and_missing_until_ts_are_invalid(self):
        for until in (True, float("nan"), None):
            owner = {"jev": {"until_ts": until}}
            self.assertEqual(compute([], owner=owner)["blocks"], {}, until)
        self.assertEqual(compute([], owner={"jev": {}})["blocks"], {})

    def test_an_expired_owner_entry_does_not_shadow_a_computed_block(self):
        owner = {"jev": {"until_ts": NOW - 1, "reason": "old"}}
        doc = compute([jev(NOW - 1)], jev_cap=100, owner=owner)
        self.assertEqual(doc["blocks"]["jev"]["until_ts"], NOW + 30)

    def test_owner_reason_is_cut_and_squeezed(self):
        doc = compute([], owner={"jev": {"until_ts": NOW + 9, "reason": "a\n  b" + "x" * 500}})
        reason = doc["blocks"]["jev"]["reason"]
        self.assertEqual(len(reason), fl.REASON_MAX_CHARS)
        self.assertTrue(reason.startswith("a b"))

    def test_owner_without_a_reason_gets_a_default(self):
        doc = compute([], owner={"jev": {"until_ts": NOW + 9}})
        self.assertTrue(doc["blocks"]["jev"]["reason"])

    def test_owner_stop_that_is_not_an_object_is_ignored(self):
        self.assertEqual(compute([], owner=["jev"])["blocks"], {})

    def test_output_stays_under_the_readers_limit(self):
        owner = {"jev": {"until_ts": NOW + 9, "reason": "x" * 10**6}, "llm": {"until_ts": NOW + 9, "reason": "y" * 10**6}}
        self.assertLess(len(json.dumps(compute([], owner=owner))), 1000)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.audit = self.tmp / "audit"
        self.audit.mkdir()
        self.fleet = self.tmp / "fleet"
        self.fleet.mkdir()
        self.limits = self.fleet / "limits.json"
        self.owner = self.fleet / "owner-stop.json"

    def env(self, **extra):
        env = {"FLEET_AUDIT_DIR": str(self.audit), "FLEET_LIMITS_FILE": str(self.limits)}
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def audit_file(self, agent, name, lines, mtime=None, raw=False):
        d = self.audit / agent
        d.mkdir(exist_ok=True)
        p = d / name
        p.write_bytes(lines if raw else "".join(json.dumps(x) + "\n" for x in lines).encode())
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def writer(self, **extra):
        return fl.Writer(fl.Settings(self.env(**extra)))

    def written(self):
        return json.loads(self.limits.read_text())


class ReadAudit(TmpCase):
    def sums(self, **kw):
        w = self.writer(**kw)
        return w.cycle(NOW)[1]

    def test_reads_every_agent_and_rotated_files(self):
        self.audit_file("alpha", "2023-11-14.jsonl", [jev(NOW - 5)], NOW)
        self.audit_file("alpha", "2023-11-14.jsonl.1", [jev(NOW - 6)], NOW)
        self.audit_file("bravo", "2023-11-14.jsonl", [llm(NOW - 7)], NOW)
        self.assertEqual(self.sums(), (300, 100))

    def test_the_lifecycle_subdirectory_is_not_audit(self):
        self.audit_file("alpha", "2023-11-14.jsonl", [jev(NOW - 5)], NOW)
        self.audit_file("alpha/lifecycle", "2023-11-14.jsonl", [jev(NOW - 5, 10**6, 10**6)], NOW)
        self.assertEqual(self.sums(), (150, 0))

    def test_only_jsonl_files_and_regular_ones(self):
        self.audit_file("alpha", "notes.txt", [jev(NOW - 5)], NOW)
        self.audit_file("alpha", ".hidden.jsonl", [jev(NOW - 5)], NOW)
        (self.audit / "alpha" / "dir.jsonl").mkdir()
        (self.audit / "stray.jsonl").write_text(json.dumps(jev(NOW - 5)) + "\n")   # not under an agent dir
        self.assertEqual(self.sums(), (0, 0))

    def test_a_file_last_written_before_the_window_is_not_read(self):
        self.audit_file("alpha", "old.jsonl", [jev(NOW - 5)], mtime=NOW - 3600)
        self.audit_file("alpha", "new.jsonl", [jev(NOW - 5)], mtime=NOW - 3599)
        self.assertEqual(self.sums(), (150, 0))

    def test_old_records_in_a_fresh_file_do_not_count(self):
        self.audit_file("alpha", "day.jsonl", [jev(NOW - 7200), jev(NOW - 10)], NOW)
        self.assertEqual(self.sums(), (150, 0))

    def test_truncated_last_line_blank_lines_garbage_and_bad_utf8(self):
        good = (json.dumps(jev(NOW - 1)) + "\n").encode()
        raw = (b"\n" + good + b"not json\n[1,2]\n" + b'{"ts": 1\xff, "usage": {"x": 1}}\n'
               + good + b'{"ts": 17000000')   # last line cut mid-write
        self.audit_file("alpha", "day.jsonl", raw, NOW, raw=True)
        self.assertEqual(self.sums(), (300, 0))

    def test_invalid_utf8_inside_a_string_keeps_the_record(self):
        line = b'{"ts": %d, "goal": "caf\xe9", "usage": {"input_tokens": 5, "output_tokens": 6}}\n' % int(NOW - 1)
        self.audit_file("alpha", "day.jsonl", line, NOW, raw=True)
        self.assertEqual(self.sums(), (11, 0))

    def test_a_file_that_vanishes_between_listing_and_opening_is_skipped_quietly(self):
        self.audit_file("alpha", "a.jsonl", [jev(NOW - 5)], NOW)
        self.audit_file("alpha", "b.jsonl", [jev(NOW - 5)], NOW)
        real_open = open

        def flaky(path, *a, **kw):
            if str(path).endswith("a.jsonl"):
                raise FileNotFoundError(path)
            return real_open(path, *a, **kw)

        with mock.patch("builtins.open", flaky), self.assertNoLogs("fleet_limits", "WARNING"):
            self.assertEqual(self.sums(), (150, 0))

    def test_an_unreadable_file_warns_once_and_counts_as_no_usage(self):
        self.audit_file("alpha", "a.jsonl", [jev(NOW - 5)], NOW)
        self.audit_file("alpha", "b.jsonl", [jev(NOW - 5)], NOW)
        real_open = open

        def denied(path, *a, **kw):
            if str(path).endswith("a.jsonl"):
                raise PermissionError(13, "denied", path)
            return real_open(path, *a, **kw)

        w = self.writer()
        with mock.patch("builtins.open", denied), self.assertLogs("fleet_limits", "WARNING") as logs:
            self.assertEqual(w.cycle(NOW)[1], (150, 0))
            self.assertEqual(w.cycle(NOW + 15)[1], (150, 0))
        self.assertEqual(len(logs.records), 1)


class Shell(TmpCase):
    def test_unreadable_audit_directory_still_writes_the_owner_stop_and_warns_once(self):
        self.owner.write_text(json.dumps({"jev": {"until_ts": NOW + 900, "reason": "owner"}}))
        w = fl.Writer(fl.Settings({"FLEET_AUDIT_DIR": str(self.tmp / "nope"), "FLEET_LIMITS_FILE": str(self.limits),
                                   "FLEET_JEV_TOKENS_PER_HOUR": "1"}))
        with self.assertLogs("fleet_limits", "WARNING") as logs:
            _, sums, ok = w.cycle(NOW)
            w.cycle(NOW + 15)
        self.assertTrue(ok)
        self.assertEqual(sums, (0, 0))
        self.assertEqual(self.written()["blocks"], {"jev": {"until_ts": NOW + 900, "reason": "owner"}})
        self.assertEqual(len(logs.records), 1)

    def test_audit_path_that_is_a_file_does_not_raise(self):
        (self.tmp / "afile").write_text("x")
        w = fl.Writer(fl.Settings({"FLEET_AUDIT_DIR": str(self.tmp / "afile"), "FLEET_LIMITS_FILE": str(self.limits)}))
        with self.assertLogs("fleet_limits", "WARNING"):
            self.assertTrue(w.cycle(NOW)[2])
        self.assertEqual(self.written()["blocks"], {})

    def test_owner_stop_file_default_sits_next_to_the_limits_file(self):
        self.assertEqual(fl.Settings(self.env()).owner_file, str(self.fleet / "owner-stop.json"))
        self.assertEqual(fl.Settings(self.env(FLEET_OWNER_STOP_FILE="/x/y.json")).owner_file, "/x/y.json")

    def test_owner_stop_read_from_the_file_and_removed_with_it(self):
        w = self.writer()
        self.owner.write_text(json.dumps({"llm": {"until_ts": NOW + 900, "reason": "r"}}))
        w.cycle(NOW)
        self.assertIn("llm", self.written()["blocks"])
        self.owner.unlink()
        w.cycle(NOW + 15)
        self.assertEqual(self.written()["blocks"], {})

    def test_an_invalid_owner_file_after_a_valid_one_keeps_the_valid_one(self):
        w = self.writer()
        self.owner.write_text(json.dumps({"jev": {"until_ts": NOW + 900, "reason": "keep me"}}))
        w.cycle(NOW)
        for i, bad in enumerate((b'{"jev": {"until_', b"[1]", b"\xff\xfe", b"x" * (fl.MAX_BYTES + 1))):
            self.owner.write_bytes(bad)
            if i == 0:
                with self.assertLogs("fleet_limits", "WARNING"):
                    w.cycle(NOW + 15)
            else:
                w.cycle(NOW + 15)   # the same problem as last cycle is not logged again
            self.assertEqual(self.written()["blocks"]["jev"]["reason"], "keep me", bad[:20])

    def test_an_invalid_owner_file_at_start_means_no_owner_stop(self):
        self.owner.write_text("{oops")
        with self.assertLogs("fleet_limits", "WARNING") as logs:
            self.writer().cycle(NOW)
            self.writer().cycle(NOW)
        self.assertEqual(self.written()["blocks"], {})
        self.assertEqual(len(logs.records), 2)   # one per process: two writers here

    def test_an_unreadable_owner_file_keeps_the_previous_one(self):
        w = self.writer()
        self.owner.write_text(json.dumps({"jev": {"until_ts": NOW + 900, "reason": "keep me"}}))
        w.cycle(NOW)
        self.owner.unlink()
        self.owner.mkdir()   # reading a directory fails with something other than FileNotFoundError
        with self.assertLogs("fleet_limits", "WARNING"):
            w.cycle(NOW + 15)
        self.assertIn("jev", self.written()["blocks"])

    def test_the_hours_sums_and_the_caps_end_to_end(self):
        self.audit_file("alpha", "d.jsonl", [jev(NOW - 5, 600, 400), llm(NOW - 5, 30, 20)], NOW)
        w = self.writer(FLEET_JEV_TOKENS_PER_HOUR=1000, FLEET_LLM_TOKENS_PER_HOUR=51)
        doc, sums, ok = w.cycle(NOW)
        self.assertEqual((sums, ok), ((1000, 50), True))
        self.assertEqual(sorted(doc["blocks"]), ["jev"])
        self.assertEqual(self.written(), doc)

    def test_settings(self):
        s = fl.Settings(self.env())
        self.assertEqual((s.jev_cap, s.llm_cap, s.period_s), (0, 0, 15.0))
        for key in ("FLEET_AUDIT_DIR", "FLEET_LIMITS_FILE"):
            env = self.env()
            del env[key]
            with self.assertRaises(ValueError):
                fl.Settings(env)
        for name, value in (("FLEET_JEV_TOKENS_PER_HOUR", "-1"), ("FLEET_LLM_TOKENS_PER_HOUR", "x"),
                            ("FLEET_LIMITS_PERIOD_S", "0"), ("FLEET_LIMITS_PERIOD_S", "nan"),
                            ("FLEET_LIMITS_PERIOD_S", "inf")):
            with self.assertRaises(ValueError, msg=(name, value)):
                fl.Settings(self.env(**{name: value}))

    def test_bad_settings_exit_2_without_writing(self):
        self.assertEqual(fl.main(["--once"], env={"FLEET_AUDIT_DIR": str(self.audit)}), 2)
        self.assertFalse(self.limits.exists())


class AtomicWrite(TmpCase):
    def test_replaced_by_rename_and_a_held_reader_keeps_the_old_content(self):
        fl._write_atomic(self.limits, '{"old": true}\n')
        inode = os.stat(self.limits).st_ino
        with open(self.limits) as held:
            fl._write_atomic(self.limits, '{"new": true, "pad": "' + "x" * 5000 + '"}\n')
            self.assertEqual(held.read(), '{"old": true}\n')
        self.assertNotEqual(os.stat(self.limits).st_ino, inode)
        self.assertTrue(json.loads(self.limits.read_text())["new"])

    def test_a_failed_replace_leaves_the_old_file_and_no_temporary(self):
        fl._write_atomic(self.limits, "old\n")
        with mock.patch("os.replace", side_effect=OSError(28, "No space left on device")), \
                self.assertRaises(OSError):
            fl._write_atomic(self.limits, "new\n")
        self.assertEqual(self.limits.read_text(), "old\n")
        self.assertEqual([p.name for p in self.fleet.iterdir()], ["limits.json"])

    def test_a_failure_midway_never_leaves_a_half_file(self):
        fl._write_atomic(self.limits, "old\n")
        with mock.patch("os.fsync", side_effect=OSError(5, "I/O error")), self.assertRaises(OSError):
            fl._write_atomic(self.limits, "new\n" * 1000)
        self.assertEqual(self.limits.read_text(), "old\n")
        self.assertEqual([p.name for p in self.fleet.iterdir()], ["limits.json"])

    def test_mode_is_0644_whatever_the_umask(self):
        old = os.umask(0o077)
        try:
            fl._write_atomic(self.limits, "x\n")
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(self.limits).st_mode), 0o644)

    def test_a_failed_write_in_a_cycle_warns_once_returns_false_and_keeps_the_old_file(self):
        w = self.writer()
        w.cycle(NOW)
        before = self.limits.read_text()
        with mock.patch("os.replace", side_effect=OSError(28, "No space left on device")), \
                self.assertLogs("fleet_limits", "WARNING") as logs:
            self.assertFalse(w.cycle(NOW + 15)[2])
            self.assertFalse(w.cycle(NOW + 30)[2])
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(self.limits.read_text(), before)
        self.assertTrue(w.cycle(NOW + 45)[2])   # retried next period


class Once(TmpCase):
    def test_once_in_process_logs_the_sums_and_writes(self):
        self.audit_file("alpha", "d.jsonl", [jev(NOW - 5)], NOW)
        env = self.env(FLEET_JEV_TOKENS_PER_HOUR=100)
        with self.assertLogs("fleet_limits", "INFO") as logs:
            self.assertEqual(fl.main(["--once"], env=env, clock=lambda: NOW), 0)
        self.assertIn("jev 150 of 100", logs.output[0])
        self.assertIn("llm 0 of off", logs.output[0])
        self.assertIn("blocked: jev", logs.output[0])
        self.assertEqual(self.written()["blocks"]["jev"]["until_ts"], NOW + 30)

    def test_once_exits_1_when_the_write_fails(self):
        with mock.patch("os.replace", side_effect=OSError(28, "full")), self.assertLogs("fleet_limits", "INFO"):
            self.assertEqual(fl.main(["--once"], env=self.env(), clock=lambda: NOW), 1)

    def test_once_as_a_real_process_on_a_temporary_tree(self):
        import time
        now = time.time()
        self.audit_file("alpha", "d.jsonl", [jev(now - 5), jev(now - 4)])
        self.audit_file("alpha/lifecycle", "d.jsonl", [jev(now - 5, 10**6, 10**6)])
        self.owner.write_text(json.dumps({"llm": {"until_ts": now + 3600, "reason": "owner says no"}}))
        env = {"PATH": os.environ.get("PATH", ""), **self.env(FLEET_JEV_TOKENS_PER_HOUR=300)}
        out = subprocess.run([sys.executable, str(TOOL / "fleet_limits.py"), "--once"], env=env,
                             capture_output=True, text=True, timeout=30, cwd=str(self.tmp))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("jev 300 of 300", out.stderr)
        doc = self.written()
        self.assertEqual(sorted(doc["blocks"]), ["jev", "llm"])
        self.assertEqual(doc["blocks"]["llm"]["reason"], "owner says no")
        self.assertEqual([p.name for p in self.fleet.iterdir() if p.name.startswith(".")], [])


class Loop(TmpCase):
    def run_loop(self, until, **extra):
        stop = threading.Event()
        t = threading.Thread(target=fl.main, kwargs={"argv": [], "stop": stop,
                                                     "env": self.env(FLEET_LIMITS_PERIOD_S=0.01, **extra)})
        t.start()
        try:
            for _ in range(500):
                if until():
                    break
                stop.wait(0.02)
        finally:
            stop.set()
            t.join(10)
        self.assertFalse(t.is_alive())

    def test_loop_writes_and_stops_on_the_event(self):
        self.run_loop(lambda: self.limits.exists())
        self.assertEqual(self.written()["version"], 1)

    def test_a_cycle_that_raises_is_logged_once_and_the_loop_goes_on(self):
        real = fl.hour_sums
        calls = []

        def flaky(records, now):
            calls.append(1)
            if len(calls) < 4:
                raise RuntimeError("boom")
            return real(records, now)

        with mock.patch.object(fl, "hour_sums", flaky), self.assertLogs("fleet_limits", "WARNING") as logs:
            self.run_loop(lambda: self.limits.exists())
        self.assertTrue(self.limits.exists())
        self.assertEqual(len([r for r in logs.records if "cycle failed" in r.getMessage()]), 1)

    def test_keyboard_interrupt_is_not_swallowed_by_the_cycle_guard_and_exits_cleanly(self):
        with mock.patch.object(fl, "hour_sums", side_effect=KeyboardInterrupt):
            self.assertEqual(fl.main([], env=self.env(), stop=threading.Event()), 0)

    def test_sigterm_handler_is_restored(self):
        import signal
        before = signal.getsignal(signal.SIGTERM)
        stop = threading.Event()
        stop.set()
        with mock.patch.object(fl.threading, "Event", return_value=stop):
            fl.main([], env=self.env())
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


class ReaderContract(TmpCase):
    """The file the writer produces, read by the real agent.fleetlimits.FleetLimits."""

    def test_blocked_while_over_the_cap_and_not_31_seconds_after_the_writer_stops(self):
        self.audit_file("alpha", "d.jsonl", [jev(NOW - 5, 600, 500), llm(NOW - 5, 5, 5)], NOW)
        w = self.writer(FLEET_JEV_TOKENS_PER_HOUR=1000, FLEET_LLM_TOKENS_PER_HOUR=1000)
        w.cycle(NOW)
        clock = [NOW + 1]
        limits = FleetLimits(str(self.limits), clock=lambda: clock[0])
        reason = limits.blocked("jev")
        self.assertIn("fleet jev budget spent: 1100 of 1000", reason or "")
        self.assertIsNone(limits.blocked("llm"))
        self.assertEqual(limits.health[0], "ok")
        clock[0] = NOW + 29.9
        self.assertIsNotNone(limits.blocked("jev"))
        clock[0] = NOW + 31   # the writer has stopped: nothing refreshed the file
        self.assertIsNone(limits.blocked("jev"))

    def test_under_the_cap_blocks_nothing(self):
        self.audit_file("alpha", "d.jsonl", [jev(NOW - 5)], NOW)
        self.writer(FLEET_JEV_TOKENS_PER_HOUR=151).cycle(NOW)
        limits = FleetLimits(str(self.limits), clock=lambda: NOW + 1)
        self.assertIsNone(limits.blocked("jev"))
        self.assertEqual(limits.health, ("ok", NOW))

    def test_an_owner_stop_blocks_for_its_own_duration(self):
        self.owner.write_text(json.dumps({"llm": {"until_ts": NOW + 7200, "reason": "owner  says\nno"}}))
        self.writer().cycle(NOW)
        limits = FleetLimits(str(self.limits), clock=lambda: NOW + 3600)
        self.assertIn("owner says no", limits.blocked("llm") or "")
        self.assertIsNone(limits.blocked("jev"))

    def test_the_largest_possible_file_is_within_the_readers_size_limit(self):
        owner = {"jev": {"until_ts": NOW + 9, "reason": "x" * 20000}, "llm": {"until_ts": NOW + 9, "reason": "y" * 20000}}
        self.owner.write_text(json.dumps(owner))
        self.writer().cycle(NOW)
        self.assertLess(self.limits.stat().st_size, 65536)
        limits = FleetLimits(str(self.limits), clock=lambda: NOW + 1)
        self.assertIsNotNone(limits.blocked("jev"))
        self.assertEqual(limits.health[0], "ok")


class ImportDiscipline(unittest.TestCase):
    def test_this_module_restored_sys_path(self):
        self.assertNotIn(str(TOOL), sys.path)


if __name__ == "__main__":
    unittest.main()
