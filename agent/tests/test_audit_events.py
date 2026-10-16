"""ADR 0020 slice 1 (#390): the level-up as an activity event on the decision audit line."""
import collections
import glob
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from agent import think
from agent.audit import AuditLogger
from agent.handlers import spells as spell_handlers
from agent.state import ACTIVITY_MAXLEN, GameState
from agent.tests.builders import FakeSession
from agent.tests.test_think import FakeLLMClient
from agent.perception import WorldState


def _levelup_payload(level):
    return (struct.pack('<II', level, 20)
            + struct.pack('<7I', *range(7))
            + struct.pack('<5I', *range(5)))


def _record(audit, events=None):
    return audit.record(cycle=1, snapshot={}, tool_call={"name": "idle", "args": {}}, valid=True,
                        result={"ok": True, "error": None}, events=events, ts=1731000000.0)


def _only_line(base):
    (path,) = glob.glob(os.path.join(base, "Jev1", "*.jsonl"))
    with open(path, encoding="utf-8") as f:
        (line,) = f.readlines()
    return json.loads(line)


class ActivityOffTests(unittest.TestCase):
    def test_off_by_default_nothing_is_buffered(self):
        state = GameState()
        self.assertFalse(state.activity_events)
        state.emit_activity("level", to=5)
        self.assertEqual(state.drain_activity(), [])

    def test_off_records_no_events_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditLogger("Jev1", base_dir=tmp)
            _record(audit, events=GameState().drain_activity())
            self.assertNotIn("events", _only_line(tmp))


class ActivityOnTests(unittest.TestCase):
    def setUp(self):
        self.state = GameState()
        self.state.activity_events = True

    def test_levelup_info_makes_one_level_event_on_the_next_line(self):
        ctx = SimpleNamespace(state=self.state)
        before = time.time()
        spell_handlers.handle_levelup_info(ctx, _levelup_payload(5))
        events = self.state.drain_activity()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "level")
        self.assertEqual(events[0]["detail"], {"to": 5})
        self.assertLess(abs(events[0]["t"] - before), 60)  # wall clock, not time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            _record(AuditLogger("Jev1", base_dir=tmp), events=events)
            line = _only_line(tmp)
        self.assertEqual(line["events"], [{"kind": "level", "t": events[0]["t"], "detail": {"to": 5}}])

    def test_think_cycle_writes_the_buffered_event_on_its_line(self):
        sess = FakeSession(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.activity_events = True
        sess.emit_activity("level", to=6)
        with tempfile.TemporaryDirectory() as tmp:
            think.think_and_act(sess, WorldState(), FakeLLMClient("idle", {}),
                                audit_logger=AuditLogger("Jev1", base_dir=tmp))
            line = _only_line(tmp)
        self.assertEqual([(e["kind"], e["detail"]) for e in line["events"]], [("level", {"to": 6})])
        self.assertEqual(sess.drain_activity(), [])  # drained by the cycle

    def test_the_21st_event_drops_the_oldest(self):
        self.assertEqual(ACTIVITY_MAXLEN, 20)
        for to in range(1, 22):
            self.state.emit_activity("level", to=to)
        self.assertEqual([e["detail"]["to"] for e in self.state.drain_activity()], list(range(2, 22)))

    def test_drain_leaves_the_buffer_empty(self):
        self.state.emit_activity("level", to=2)
        self.assertEqual(len(self.state.drain_activity()), 1)
        self.assertEqual(self.state.drain_activity(), [])


class ConcurrentDrainTests(unittest.TestCase):
    """The receive thread appends while the think thread drains. Nothing may be lost or duplicated."""

    def test_union_of_drains_is_every_event_in_order(self):
        state = GameState()
        state.activity_events = True
        state._activity = collections.deque()  # unbounded: this test is about the drain, not the bound
        # Switch threads often, so a drain that copies and then clears is interrupted by an append.
        self.addCleanup(sys.setswitchinterval, sys.getswitchinterval())
        sys.setswitchinterval(1e-6)
        total = 5000
        drained, done = [], threading.Event()

        def appender():
            for n in range(1, total + 1):
                state.emit_activity("level", to=n)
            done.set()

        def drainer():
            while True:
                drained.extend(state.drain_activity())
                if done.is_set():
                    drained.extend(state.drain_activity())
                    return

        threads = [threading.Thread(target=appender), threading.Thread(target=drainer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual([e["detail"]["to"] for e in drained], list(range(1, total + 1)))


if __name__ == "__main__":
    unittest.main()
