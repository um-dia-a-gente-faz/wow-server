"""A reader on another thread never iterates GameState.events live (#692).

The recv thread appends through record_event; an action waiting on an event scans on the
think thread. A live `for e in session.events` raises "deque mutated during iteration"
when an append lands mid-scan. The scan below is a real action helper (trade's
_find_event_since), run against a recv-thread stand-in appending in a tight loop."""

import threading
import unittest

from agent.actions.trade import _find_event_since
from agent.state import EVENTS_MAXLEN, GameState

JOIN_S = 60.0
SCANS = 40000  # ~0.5 s per shape; the live-iteration version raised ~1 in 1000 scans


def _scan_while_appending(state, below_maxlen: bool) -> int:
    """Count the RuntimeErrors seen by SCANS full scans while another thread appends."""
    stop, started = threading.Event(), threading.Event()

    def append():
        started.set()
        while not stop.is_set():
            state.record_event("noise")
            if below_maxlen and len(state.events) > EVENTS_MAXLEN - 10:
                state.events.clear()  # stay under maxlen: the deque grows, never rotates

    t = threading.Thread(target=append)
    t.start()
    errors = 0
    try:
        started.wait(JOIN_S)
        for _ in range(SCANS):
            try:
                _find_event_since(state, 0.0, "never")  # no match: scans every entry
            except RuntimeError:
                errors += 1
    finally:
        stop.set()
        t.join(JOIN_S)
    assert not t.is_alive(), "appender thread did not stop"
    return errors


class EventsSnapshotTest(unittest.TestCase):
    def test_scan_survives_appends_below_maxlen(self):
        self.assertEqual(_scan_while_appending(GameState(), below_maxlen=True), 0)

    def test_scan_survives_appends_at_maxlen(self):
        state = GameState()
        for _ in range(EVENTS_MAXLEN):
            state.record_event("noise")
        self.assertEqual(_scan_while_appending(state, below_maxlen=False), 0)

    def test_snapshots_are_independent_lists_in_order(self):
        state = GameState()
        state.record_event("a")
        state.record_event("b")
        state.chat_inbox.append({"sender_name": "x"})
        events, inbox = state.events_snapshot(), state.chat_inbox_snapshot()
        state.record_event("c")
        state.chat_inbox.append({"sender_name": "y"})
        self.assertIsInstance(events, list)
        self.assertEqual([e["kind"] for e in events], ["a", "b"])
        self.assertEqual(inbox, [{"sender_name": "x"}])


if __name__ == "__main__":
    unittest.main()
