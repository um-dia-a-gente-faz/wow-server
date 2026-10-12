"""Connection lifecycle state machine (#584): agent/lifecycle.py.

The transition table is checked from every direction: each ordered pair of states,
the walk the supervisor makes today, history and snapshot shape, the callback and
its lock, and the ADR table that documents the same pairs (docs/adr/0015).
"""
import json
import pathlib
import threading
import unittest

from agent.lifecycle import REASONS, TERMINAL, TRANSITIONS, InvalidTransition, Lifecycle, State, Transition

ROOT = pathlib.Path(__file__).resolve().parents[2]
ADR_PATH = ROOT / "docs" / "adr" / "0015-agent-connection-lifecycle.md"

C, O, B, S, STOPPED, FAILED = (State.CONNECTING, State.ONLINE, State.BACKOFF,
                               State.STOPPING, State.STOPPED, State.FAILED)

# One fixed valid path from CONNECTING to each state.
PATHS = {
    C: (),
    O: (O,),
    B: (B,),
    S: (S,),
    STOPPED: (S, STOPPED),
    FAILED: (FAILED,),
}


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def machine_in(state: State, **kwargs) -> Lifecycle:
    """A fresh machine driven to `state` along PATHS (every step is a checked transition)."""
    lc = Lifecycle(**kwargs)
    for step in PATHS[state]:
        lc.to(step, "setup")
    assert lc.state is state
    return lc


def reachable(start: State) -> set[State]:
    seen, todo = {start}, [start]
    while todo:
        for nxt in TRANSITIONS[todo.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def adr_table_pairs() -> list[tuple[str, str]]:
    """(From, To) of every row of the ADR's transition table. Fails loudly if the table is missing or empty."""
    lines = ADR_PATH.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "| From | To | When |"]
    if len(starts) != 1:
        raise AssertionError(f"expected one '| From | To | When |' table in {ADR_PATH.name}, found {len(starts)}")
    rows = []
    for line in lines[starts[0] + 2:]:   # skip the header and the |---| separator
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3:
            raise AssertionError(f"malformed row in the ADR table: {line!r}")
        rows.append((cells[0], cells[1]))
    if not rows:
        raise AssertionError("the ADR transition table has no rows")
    return rows


def adr_reason_rows() -> dict[str, set[tuple[str, str]]]:
    """reason -> {(From, To)} from the ADR's Reasons table. Fails loudly if the table is missing or malformed."""
    lines = ADR_PATH.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "| Reason | Transitions | When |"]
    if len(starts) != 1:
        raise AssertionError(f"expected one '| Reason | Transitions | When |' table in {ADR_PATH.name}, found {len(starts)}")
    rows: dict[str, set[tuple[str, str]]] = {}
    for line in lines[starts[0] + 2:]:   # skip the header and the |---| separator
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3 or not (cells[0].startswith("`") and cells[0].endswith("`")):
            raise AssertionError(f"malformed row in the ADR reasons table: {line!r}")
        pairs = set()
        for pair in cells[1].split(","):
            source, sep, target = (part.strip() for part in pair.partition(">"))
            if not sep or not source or not target:
                raise AssertionError(f"malformed transition {pair!r} in the ADR reasons table")
            pairs.add((source, target))
        if cells[0].strip("`") in rows:
            raise AssertionError(f"reason {cells[0]} is listed twice in the ADR reasons table")
        rows[cells[0].strip("`")] = pairs
    if not rows:
        raise AssertionError("the ADR reasons table has no rows")
    return rows


class TransitionTableTest(unittest.TestCase):
    def test_table_covers_every_state_and_terminal_is_the_sinks(self):
        self.assertEqual(set(TRANSITIONS), set(State))
        self.assertEqual({s for s in State if not TRANSITIONS[s]}, set(TERMINAL))
        self.assertEqual(TERMINAL, frozenset({STOPPED, FAILED}))

    def test_no_transition_leaves_a_terminal_state(self):
        for s in TERMINAL:
            with self.subTest(state=s):
                self.assertEqual(TRANSITIONS[s], frozenset())

    def test_every_non_terminal_state_can_reach_stopped(self):
        for s in set(State) - TERMINAL:
            with self.subTest(state=s):
                self.assertIn(STOPPED, reachable(s))


class EveryOrderedPairTest(unittest.TestCase):
    def test_all_36_pairs_follow_the_table(self):
        pairs = [(a, b) for a in State for b in State]
        self.assertEqual(len(pairs), 36)
        self.assertEqual(sum(len(v) for v in TRANSITIONS.values()), 9)
        for source, target in pairs:
            with self.subTest(source=source.value, target=target.value):
                lc = machine_in(source, clock=FakeClock(5.0))
                if target in TRANSITIONS[source]:
                    t = lc.to(target, "pair")
                    self.assertEqual((t.source, t.target), (source, target))
                    self.assertIs(lc.state, target)
                else:
                    before = lc.history()
                    with self.assertRaises(InvalidTransition) as cm:
                        lc.to(target, "pair")
                    self.assertIs(lc.state, source)
                    self.assertEqual(lc.history(), before)
                    self.assertIn(source.value, str(cm.exception))
                    self.assertIn(target.value, str(cm.exception))

    def test_invalid_transition_does_not_call_the_callback(self):
        calls = []
        lc = machine_in(B, on_transition=calls.append)
        calls.clear()   # the setup walk called the callback too
        with self.assertRaises(InvalidTransition):
            lc.to(FAILED, "nope")   # BACKOFF -> FAILED is not in the table
        with self.assertRaises(InvalidTransition):
            lc.to(B, "self")        # a self-transition is invalid too
        self.assertEqual(calls, [])


class SupervisorWalkTest(unittest.TestCase):
    def test_walk_the_supervisor_makes_today(self):
        clock = FakeClock(0.0)
        lc = Lifecycle(clock=clock)
        steps = [
            (1.0, B, "connect_failed"),
            (6.0, C, "backoff_elapsed"),
            (7.0, O, "connected"),
            (50.0, B, "disconnected"),
            (56.0, C, "backoff_elapsed"),
            (57.0, O, "connected"),
            (90.0, S, "stop"),
            (91.0, STOPPED, "teardown_done"),
        ]
        for at, target, reason in steps:
            clock.now = at
            lc.to(target, reason)

        expected = [
            Transition(C, B, "connect_failed", 1.0),
            Transition(B, C, "backoff_elapsed", 6.0),
            Transition(C, O, "connected", 7.0),
            Transition(O, B, "disconnected", 50.0),
            Transition(B, C, "backoff_elapsed", 56.0),
            Transition(C, O, "connected", 57.0),
            Transition(O, S, "stop", 90.0),
            Transition(S, STOPPED, "teardown_done", 91.0),
        ]
        self.assertEqual(lc.history(), tuple(expected))
        self.assertEqual(lc.snapshot(), {
            "state": "stopped",
            "since": 91.0,
            "reason": "teardown_done",
            "transitions": [{"from": t.source.value, "to": t.target.value, "reason": t.reason, "at": t.at}
                            for t in expected],
        })
        self.assertTrue(lc.terminal)

    def test_fresh_machine_starts_connecting_since_construction(self):
        lc = Lifecycle(clock=FakeClock(12.5))
        self.assertIs(lc.state, C)
        self.assertFalse(lc.terminal)
        self.assertEqual(lc.history(), ())
        self.assertEqual(lc.snapshot(), {"state": "connecting", "since": 12.5, "reason": "", "transitions": []})


class HistoryCapTest(unittest.TestCase):
    def test_history_keeps_the_newest_entries(self):
        clock = FakeClock(0.0)
        full = Lifecycle(clock=clock)
        capped = Lifecycle(clock=clock, history=3)
        for lc in (full, capped):
            clock.now = 0.0
            for i, (target, reason) in enumerate([(B, "a"), (C, "b"), (O, "c"), (B, "d"), (C, "e"), (O, "f"),
                                                    (S, "g"), (STOPPED, "h")]):
                clock.now = float(i)
                lc.to(target, reason)
        self.assertEqual(len(full.history()), 8)
        self.assertEqual(capped.history(), full.history()[-3:])
        self.assertEqual([t["reason"] for t in capped.snapshot()["transitions"]], ["f", "g", "h"])


class CallbackTest(unittest.TestCase):
    def test_callback_gets_each_transition_object_once(self):
        calls = []
        lc = Lifecycle(clock=FakeClock(1.0), on_transition=calls.append)
        first = lc.to(B, "connect_failed")
        second = lc.to(C, "backoff_elapsed")
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0], first)
        self.assertIs(calls[1], second)

    def test_callback_exception_propagates_after_the_state_changed(self):
        def boom(t: Transition) -> None:
            raise ValueError("observer broke")

        lc = Lifecycle(clock=FakeClock(), on_transition=boom)
        with self.assertRaises(ValueError):
            lc.to(B, "connect_failed")
        self.assertIs(lc.state, B)
        self.assertEqual(len(lc.history()), 1)

    def test_callback_may_read_state_and_snapshot_without_deadlock(self):
        seen: dict[str, object] = {}

        def reads_back(t: Transition) -> None:
            seen["state"] = lc.state
            seen["snapshot"] = lc.snapshot()

        lc = Lifecycle(clock=FakeClock(), on_transition=reads_back)
        worker = threading.Thread(target=lc.to, args=(B, "connect_failed"), daemon=True)
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "deadlock: the callback blocked on the lifecycle lock")
        self.assertIs(seen["state"], B)
        self.assertEqual(seen["snapshot"]["state"], "backoff")


class SnapshotTest(unittest.TestCase):
    def test_snapshot_is_json_ready(self):
        clock = FakeClock(0.0)
        lc = Lifecycle(clock=clock)
        clock.now = 3.0
        lc.to(B, "connect_failed")
        snap = lc.snapshot()
        self.assertEqual(json.loads(json.dumps(snap)), snap)
        self.assertEqual(snap["state"], "backoff")
        self.assertEqual(snap["transitions"][0], {"from": "connecting", "to": "backoff",
                                                  "reason": "connect_failed", "at": 3.0})


class CrashDecisionTest(unittest.TestCase):
    """#292: a crash ends in STOPPED with the reason "crashed"; FAILED stays the permanent refusal."""

    def test_every_working_state_unwinds_to_stopped_crashed(self):
        for source in (C, O, B):
            with self.subTest(source=source.value):
                lc = machine_in(source, clock=FakeClock(2.0))
                lc.to(S, "crashed")
                lc.to(STOPPED, "crashed")
                self.assertTrue(lc.terminal)
                self.assertEqual(lc.snapshot()["state"], "stopped")
                self.assertEqual(lc.snapshot()["reason"], "crashed")
                self.assertEqual([(t.source, t.target, t.reason) for t in lc.history()[-2:]],
                                 [(source, S, "crashed"), (S, STOPPED, "crashed")])

    def test_only_connecting_can_fail(self):
        self.assertEqual({s for s in State if FAILED in TRANSITIONS[s]}, {C})
        for source in (O, B, S):
            with self.subTest(source=source.value):
                lc = machine_in(source)
                with self.assertRaises(InvalidTransition):
                    lc.to(FAILED, "crashed")
                self.assertIs(lc.state, source)

    def test_reason_is_free_text_to_the_machine(self):
        lc = Lifecycle(clock=FakeClock())
        self.assertNotIn("anything at all", REASONS)
        self.assertEqual(lc.to(B, "anything at all").reason, "anything at all")


class AdrReasonsTest(unittest.TestCase):
    def test_adr_reasons_table_matches_reasons(self):
        self.assertEqual(set(adr_reason_rows()), set(REASONS))

    def test_every_reason_names_only_allowed_transitions(self):
        allowed = {(s.name, t.name) for s, ts in TRANSITIONS.items() for t in ts}
        for reason, pairs in adr_reason_rows().items():
            with self.subTest(reason=reason):
                self.assertLessEqual(pairs, allowed)

    def test_every_allowed_transition_has_a_reason(self):
        covered = set().union(*adr_reason_rows().values())
        self.assertEqual(covered, {(s.name, t.name) for s, ts in TRANSITIONS.items() for t in ts})

    def test_adr_states_table_carries_the_decided_wording(self):
        text = ADR_PATH.read_text()
        self.assertNotIn("tearing down the session |", text)
        self.assertNotIn("| STOPPED | Terminal, clean |", text)
        self.assertIn("## Decisions from the review of #587", text)


class AdrTableTest(unittest.TestCase):
    def test_adr_table_matches_transitions(self):
        rows = adr_table_pairs()
        self.assertEqual(len(rows), 9, "the ADR table has one row per allowed pair")
        self.assertEqual(set(rows), {(s.name, t.name) for s, ts in TRANSITIONS.items() for t in ts})


if __name__ == "__main__":
    unittest.main()
