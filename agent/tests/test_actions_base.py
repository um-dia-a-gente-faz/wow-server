"""Tests for agent.actions.base (split out of test_actions.py, issue #248).
"""

import threading
import time
import unittest

from agent import actions as ac
from agent import perception as per
from agent import update_object as uo
from agent.state import GameState


class ActionFrameworkTest(unittest.TestCase):
    def test_registry_has_set_target_and_face(self):
        self.assertIn("set_target", ac.REGISTRY)
        self.assertIn("face", ac.REGISTRY)

    def test_catalog_returns_valid_schema_for_every_action(self):
        for schema in ac.catalog():
            self.assertIn("name", schema)
            self.assertIn("description", schema)
            self.assertIn("parameters", schema)
            params = schema["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIsInstance(params["properties"], dict)
            self.assertIsInstance(params["required"], list)
            for req in params["required"]:
                self.assertIn(req, params["properties"])

    def test_run_short_circuits_on_check_failure_without_calling_execute(self):
        calls = []

        class Failing(ac.Action):
            name = "failing"

            def check(self, session, world, **params):
                return "nope"

            def execute(self, session, world, **params):
                calls.append(1)
                return ac.ActionResult(ok=True)

        result = Failing().run(None, None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "nope")
        self.assertEqual(calls, [])

    def test_wait_for_returns_true_once_predicate_flips(self):
        calls = {"n": 0}

        def predicate():
            calls["n"] += 1
            return calls["n"] >= 3  # false on the first two polls, true on the third

        self.assertTrue(ac._wait_for(predicate, timeout=1.0, interval=0.01))
        self.assertGreaterEqual(calls["n"], 3)

    def test_wait_for_times_out(self):
        self.assertFalse(ac._wait_for(lambda: False, timeout=0.05, interval=0.01))


class WaitWakeupTest(unittest.TestCase):
    """A confirmation is seen when it lands, not at the next poll tick (#305).
    The 5 s interval would make a sleep-polling wait take 5 s."""

    def _later(self, fn):
        t = threading.Timer(0.02, fn)
        t.start()
        self.addCleanup(t.join)

    def test_recorded_event_wakes_wait_for_value(self):
        state = GameState()
        self._later(lambda: state.record_event("cast_start"))
        start = time.monotonic()
        got = ac._wait_for_value(lambda: next(iter(state.events), None), timeout=5.0, interval=5.0)
        self.assertEqual(got["kind"], "cast_start")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_applied_update_block_wakes_wait_for(self):
        world = per.WorldState()
        block = uo.UpdateBlock(update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=7,
                               object_type=uo.TYPEID_UNIT, movement={}, fields={})
        self._later(lambda: world.update_object(block))
        start = time.monotonic()
        self.assertTrue(ac._wait_for(lambda: 7 in world.objects, timeout=5.0, interval=5.0))
        self.assertLess(time.monotonic() - start, 1.0)

    def test_signal_between_predicate_and_wait_is_not_lost(self):
        state = GameState()
        calls = []

        def predicate():
            if calls:
                return next(iter(state.events), None)
            calls.append(1)
            t = threading.Thread(target=lambda: state.record_event("late"))
            t.start()
            t.join()  # the signal has landed before this returns None
            return None

        start = time.monotonic()
        got = ac._wait_for_value(predicate, timeout=5.0, interval=5.0)
        self.assertEqual(got["kind"], "late")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_unsignalled_state_is_still_polled_at_interval(self):
        flag = []
        self._later(lambda: flag.append(1))
        self.assertTrue(ac._wait_for(lambda: bool(flag), timeout=1.0, interval=0.01))
