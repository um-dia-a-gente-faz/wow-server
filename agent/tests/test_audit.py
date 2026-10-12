"""Unit tests for agent.audit.AuditLogger: file layout, size-based
rotation, retention, and secret redaction — all against a tmp dir, no
network/game state needed."""

import glob
import json
import os
import tempfile
import time
import unittest

from agent.audit import AuditLogger, _redact
from agent.tests.restore import reset_counters


class AuditLoggerWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logger = AuditLogger("TestAgent", base_dir=self.tmp.name, retention_days=14)

    def _read_lines(self, path):
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_writes_one_record_to_todays_file(self):
        self.logger.record(
            cycle=1, snapshot={"position": {"x": 1}},
            tool_call={"name": "face", "args": {"guid": 5}},
            valid=True, result={"ok": True, "error": None},
        )
        today = time.strftime("%Y-%m-%d")
        path = os.path.join(self.tmp.name, "TestAgent", f"{today}.jsonl")
        self.assertTrue(os.path.exists(path))
        recs = self._read_lines(path)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["agent"], "TestAgent")
        self.assertEqual(recs[0]["cycle"], 1)
        self.assertEqual(recs[0]["tool_call"], {"name": "face", "args": {"guid": 5}})
        self.assertIsNone(recs[0]["brain"])
        self.assertIsNone(recs[0]["confidence"])

    def test_failing_observer_is_counted_and_auditing_continues(self):
        from agent import metrics
        reset_counters(self)

        def boom(rec):
            raise RuntimeError("observer down")
        self.logger.on_record = boom
        with self.assertLogs("agent.audit", level="ERROR"):
            self.logger.record(cycle=1, snapshot={}, tool_call={"name": "face", "args": {}},
                               valid=True, result={"ok": True})
        self.assertEqual(metrics.SWALLOWED, {"audit.on_record": 1})
        today = time.strftime("%Y-%m-%d")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "TestAgent", f"{today}.jsonl")))

    def test_records_brain_and_jev_confidence(self):
        # UM-101: which brain decided, and Jev's confidence when it did.
        rec = self.logger.record(
            cycle=1, snapshot={}, tool_call={"name": "loot", "args": {"guid": 7}},
            valid=True, result={"ok": True, "error": None}, model="typesafe/jev-1.13",
            brain="jev", confidence=0.91, candidates=6, prompt_tokens=400, completion_tokens=0)
        d = rec.to_dict()
        self.assertEqual((d["brain"], d["confidence"], d["candidates"], d["fallback"]),
                         ("jev", 0.91, 6, None))
        rec = self.logger.record(
            cycle=2, snapshot={}, tool_call={"name": "loot", "args": {"guid": 7}},
            valid=True, result={"ok": True, "error": None}, brain="llm",
            fallback="jev call failed: HTTP 429")
        self.assertEqual(rec.to_dict()["fallback"], "jev call failed: HTTP 429")

    def test_jev_usage_is_bounded_and_keeps_only_token_cost_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditLogger("Jev", base_dir=tmp)
            rec = audit.record(cycle=1, snapshot={}, tool_call={}, valid=True,
                               result={"ok": True}, brain="jev",
                               usage={"input_tokens": 10, "output_tokens": 2, "cost": 0.001,
                                      "api_key": "not-a-secret-to-store", "extra": "discard"},
                               jev_status="success")
            d = rec.to_dict()
            self.assertEqual(d["usage"], {"input_tokens": 10, "output_tokens": 2, "cost": 0.001})
            self.assertEqual(d["jev_status"], "success")

    def test_full_snapshot_included_every_nth_cycle_only(self):
        logger = AuditLogger("A", base_dir=self.tmp.name, full_snapshot_every=3)
        snap = {"position": {"x": 1}}
        for cycle in range(1, 4):
            logger.record(cycle=cycle, snapshot=snap, tool_call={"name": "x", "args": {}},
                           valid=True, result={"ok": True, "error": None})
        path = os.path.join(self.tmp.name, "A", f"{time.strftime('%Y-%m-%d')}.jsonl")
        recs = self._read_lines(path)
        self.assertNotIn("snapshot", recs[0])  # cycle 1
        self.assertNotIn("snapshot", recs[1])  # cycle 2
        self.assertIn("snapshot", recs[2])     # cycle 3 (multiple of 3)

    def test_full_snapshot_included_on_invalid_or_failed_cycles(self):
        logger = AuditLogger("A", base_dir=self.tmp.name, full_snapshot_every=100)
        snap = {"position": {"x": 1}}
        logger.record(cycle=1, snapshot=snap, tool_call={"name": None, "args": {}},
                       valid=False, result={"ok": False, "error": "unknown action"})
        logger.record(cycle=2, snapshot=snap, tool_call={"name": "move_to", "args": {}},
                       valid=True, result={"ok": False, "error": "blocked"})
        path = os.path.join(self.tmp.name, "A", f"{time.strftime('%Y-%m-%d')}.jsonl")
        recs = self._read_lines(path)
        self.assertIn("snapshot", recs[0])
        self.assertIn("snapshot", recs[1])

    def test_never_writes_api_key_or_password(self):
        self.logger.record(
            cycle=1, snapshot={"config": {"api_key": "sk-secret", "password": "hunter2"}},
            tool_call={"name": "face", "args": {}}, valid=True,
            result={"ok": True, "error": None},
        )
        path = os.path.join(self.tmp.name, "TestAgent", f"{time.strftime('%Y-%m-%d')}.jsonl")
        with open(path) as f:
            raw = f.read()
        self.assertNotIn("sk-secret", raw)
        self.assertNotIn("hunter2", raw)

    def test_redact_masks_secret_keys_recursively(self):
        redacted = _redact({"a": {"api_key": "secret", "b": [{"password": "pw"}]}})
        self.assertEqual(redacted["a"]["api_key"], "***")
        self.assertEqual(redacted["a"]["b"][0]["password"], "***")


class LifecycleRecordTest(unittest.TestCase):
    """#292: lifecycle transitions go to <agent>/lifecycle/<day>.jsonl, apart from the decision records."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logger = AuditLogger("TestAgent", base_dir=self.tmp.name, retention_days=14)
        self.agent_dir = os.path.join(self.tmp.name, "TestAgent")

    def test_writes_one_line_per_transition_in_the_lifecycle_subdirectory(self):
        ts = time.time()
        first = self.logger.record_lifecycle("connecting", "online", "connected", ts=ts)
        self.logger.record_lifecycle("online", "backoff", "disconnected", ts=ts + 1)
        path = os.path.join(self.agent_dir, "lifecycle", time.strftime("%Y-%m-%d", time.localtime(ts)) + ".jsonl")
        with open(path) as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual(lines, [
            {"schema": 1, "ts": ts, "agent": "TestAgent", "from": "connecting", "to": "online", "reason": "connected"},
            {"schema": 1, "ts": ts + 1, "agent": "TestAgent", "from": "online", "to": "backoff",
             "reason": "disconnected"},
        ])
        self.assertEqual(first, lines[0])

    def test_ts_defaults_to_now(self):
        before = time.time()
        d = self.logger.record_lifecycle("connecting", "backoff", "connect_failed")
        self.assertGreaterEqual(d["ts"], before)
        self.assertLessEqual(d["ts"], time.time())

    def test_lifecycle_line_does_not_reach_on_record(self):
        seen = []
        self.logger.on_record = seen.append
        self.logger.record_lifecycle("connecting", "online", "connected")
        self.assertEqual(seen, [])

    def test_decision_readers_do_not_see_lifecycle_lines(self):
        from agent import metrics
        self.logger.record(cycle=1, snapshot={}, tool_call={"name": "idle", "args": {}},
                           valid=True, result={"ok": True, "error": None})
        self.logger.record_lifecycle("connecting", "online", "connected")
        files = metrics.find_audit_files(self.tmp.name)
        self.assertEqual(files, [os.path.join(self.agent_dir, time.strftime("%Y-%m-%d") + ".jsonl")])
        records, bad = metrics.read_audit(files)
        self.assertEqual([r["cycle"] for r in records], [1])
        self.assertEqual(bad, {})
        self.assertEqual(sorted(os.listdir(self.agent_dir)), sorted(["lifecycle", os.path.basename(files[0])]))

    def test_retention_also_cleans_the_lifecycle_subdirectory(self):
        directory = os.path.join(self.agent_dir, "lifecycle")
        os.makedirs(directory)
        now = time.time()
        old = os.path.join(directory, time.strftime("%Y-%m-%d", time.localtime(now - 30 * 86400)) + ".jsonl")
        new = os.path.join(directory, time.strftime("%Y-%m-%d", time.localtime(now)) + ".jsonl")
        for path in (old, new):
            with open(path, "w") as f:
                f.write("{}\n")
        self.assertEqual(self.logger.clean_retention(now=now), [old])
        self.assertEqual(os.listdir(directory), [os.path.basename(new)])


class AuditLoggerRotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_rotates_when_max_bytes_exceeded(self):
        logger = AuditLogger("A", base_dir=self.tmp.name, max_bytes=200)
        big_snapshot = {"position": {"x": 1}, "filler": "x" * 100}
        for cycle in range(1, 8):
            logger.record(cycle=cycle, snapshot=big_snapshot,
                           tool_call={"name": "face", "args": {}},
                           valid=True, result={"ok": True, "error": None})
        agent_dir = os.path.join(self.tmp.name, "A")
        rotated = glob.glob(os.path.join(agent_dir, "*.jsonl.1"))
        self.assertTrue(rotated, f"expected a rotated file in {os.listdir(agent_dir)}")

    def test_rotation_bumps_existing_numbered_files(self):
        logger = AuditLogger("A", base_dir=self.tmp.name, max_bytes=50)
        for cycle in range(1, 15):
            logger.record(cycle=cycle, snapshot={"x": 1},
                           tool_call={"name": "face", "args": {}},
                           valid=True, result={"ok": True, "error": None})
        agent_dir = os.path.join(self.tmp.name, "A")
        self.assertTrue(glob.glob(os.path.join(agent_dir, "*.jsonl.2")) or
                        glob.glob(os.path.join(agent_dir, "*.jsonl.1")))


class AuditLoggerRetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_clean_retention_deletes_files_older_than_window(self):
        logger = AuditLogger("A", base_dir=self.tmp.name, retention_days=1)
        agent_dir = os.path.join(self.tmp.name, "A")
        os.makedirs(agent_dir, exist_ok=True)
        old_path = os.path.join(agent_dir, "2000-01-01.jsonl")
        with open(old_path, "w") as f:
            f.write('{"cycle": 1}\n')
        recent_path = os.path.join(agent_dir, f"{time.strftime('%Y-%m-%d')}.jsonl")
        with open(recent_path, "w") as f:
            f.write('{"cycle": 2}\n')

        deleted = logger.clean_retention(now=time.time())

        self.assertIn(old_path, deleted)
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(recent_path))

    def test_keeps_files_within_retention_window(self):
        logger = AuditLogger("A", base_dir=self.tmp.name, retention_days=30)
        agent_dir = os.path.join(self.tmp.name, "A")
        os.makedirs(agent_dir, exist_ok=True)
        recent = os.path.join(agent_dir, f"{time.strftime('%Y-%m-%d')}.jsonl")
        with open(recent, "w") as f:
            f.write("{}\n")
        deleted = logger.clean_retention(now=time.time())
        self.assertEqual(deleted, [])
        self.assertTrue(os.path.exists(recent))


if __name__ == "__main__":
    unittest.main()
