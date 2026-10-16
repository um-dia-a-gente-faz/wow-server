"""tools/agent_dev/scenario_samples.py (#541): the N-sample count and table, and the CLI exit codes.

A stub stands in for Jev everywhere, so no test touches the network or a credential. The real
SCENARIOS table is read (never edited) to check the wiring.
"""
import contextlib
import io
import pathlib
import sys
import unittest
from collections import namedtuple
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.brain import Brain  # noqa: E402
from agent.tests import test_brain_scenarios as tbs  # noqa: E402
from tools.agent_dev import scenario_samples as ss  # noqa: E402

Dec = namedtuple("Dec", "action params")


class StubBrain:
    """decide() returns the scripted (action, params) for each call, in order, and records its calls."""

    def __init__(self, script=(("idle", {}),), jev=True):
        self.script = list(script)
        self.jev, self.llm = (object() if jev else None), None
        self.calls = []

    def decide(self, snapshot, *, my_guid=None, history=None, handles=None, **_):
        self.calls.append({"snapshot": snapshot, "my_guid": my_guid, "history": history, "handles": handles})
        action, params = self.script[(len(self.calls) - 1) % len(self.script)]
        return Dec(action, params)


def idle_is(action, params):
    return action == "idle"


def scen(**kw):
    return dict(snapshot={"me": {}}, chosen=idle_is, **kw)


class SampleTest(unittest.TestCase):
    def test_violations_and_share_are_counted_per_scenario(self):
        brain = StubBrain([("idle", {}), ("loot", {"guid": 10}), ("idle", {}), ("loot", {"guid": 11}),
                           ("loot", {"guid": 12})])
        rows = ss.sample({"a": scen()}, brain, 5)
        self.assertEqual(rows, [ss.Row("a", 5, 3, 0.6)])

    def test_a_scenario_with_no_violation_has_share_zero(self):
        rows = ss.sample({"a": scen()}, StubBrain(), 4)
        self.assertEqual(rows, [ss.Row("a", 4, 0, 0.0)])

    def test_each_scenario_gets_n_calls_with_its_own_inputs(self):
        brain = StubBrain()
        scenarios = {
            "plain": scen(),
            "with_context": scen(my_guid=77, history=[{"action": "idle"}], handles={"u1": 5}),
        }
        rows = ss.sample(scenarios, brain, 3)
        self.assertEqual([r.name for r in rows], ["plain", "with_context"])
        self.assertEqual(len(brain.calls), 6)
        plain, ctx = brain.calls[:3], brain.calls[3:]
        for c in plain:
            self.assertEqual((c["my_guid"], c["history"]), (tbs.ME, None))
            self.assertIsNone(c["handles"])
            self.assertIs(c["snapshot"], scenarios["plain"]["snapshot"])
        for c in ctx:
            self.assertEqual((c["my_guid"], c["history"]), (77, [{"action": "idle"}]))
            self.assertEqual(c["handles"].resolve("u1"), 5)

    def test_known_gap_scenarios_are_not_sampled(self):
        brain = StubBrain()
        rows = ss.sample({"gap": scen(known_gap=True), "ok": scen()}, brain, 2)
        self.assertEqual([r.name for r in rows], ["ok"])
        self.assertEqual(len(brain.calls), 2)

    def test_scenarios_without_chosen_are_skipped(self):
        brain = StubBrain()
        rows = ss.sample({"offered_only": {"snapshot": {"me": {}}, "offered": lambda cs: True}}, brain, 2)
        self.assertEqual(rows, [])
        self.assertEqual(brain.calls, [])

    def test_the_real_table_yields_one_row_per_enforced_scenario(self):
        brain = StubBrain()
        rows = ss.sample(tbs.SCENARIOS, brain, 1)
        want = [k for k, s in tbs.SCENARIOS.items() if "chosen" in s and not s.get("known_gap")]
        self.assertEqual([r.name for r in rows], want)
        self.assertEqual(len(brain.calls), len(want))

    def test_format_table_lists_every_row(self):
        text = ss.format_table([ss.Row("loot_first", 5, 1, 0.2), ss.Row("dead_releases_spirit", 5, 0, 0.0)])
        lines = text.splitlines()
        self.assertIn("scenario", lines[0])
        self.assertIn("violations", lines[0])
        self.assertTrue(any("loot_first" in l and "0.20" in l for l in lines))
        self.assertTrue(any("dead_releases_spirit" in l and "0.00" in l for l in lines))


class MainTest(unittest.TestCase):
    """main(): the brain is patched in, so no config is read and nothing is called."""

    def run_main(self, argv, brain=None, no_brain=False, scenarios=None):
        built = None if no_brain else (brain if brain is not None else StubBrain())
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ss, "load_config", return_value=object()), \
                mock.patch.object(Brain, "from_config", return_value=built), \
                mock.patch.object(ss, "SCENARIOS", scenarios if scenarios is not None else {"a": scen()}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ss.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_exit_0_when_every_share_is_within_the_threshold(self):
        brain = StubBrain([("idle", {}), ("loot", {})])   # share 0.5, not above a 0.5 threshold
        rc, out, _ = self.run_main(["--n", "2", "--threshold", "0.5"], brain=brain)
        self.assertEqual(rc, 0)
        self.assertIn("a", out)

    def test_exit_1_when_a_share_is_above_the_threshold(self):
        brain = StubBrain([("loot", {})])
        rc, out, _ = self.run_main(["--n", "5"], brain=brain)
        self.assertEqual(rc, 1)
        self.assertIn("1.00", out)

    def test_threshold_defaults_to_0_2_and_n_to_5(self):
        brain = StubBrain([("idle", {}), ("idle", {}), ("idle", {}), ("idle", {}), ("loot", {})])
        rc, out, _ = self.run_main([], brain=brain)   # 1 of 5 = 0.2, not above 0.2
        self.assertEqual(rc, 0)
        self.assertEqual(len(brain.calls), 5)
        self.assertIn("0.20", out)

    def test_default_threshold_fails_two_violations_in_five(self):
        brain = StubBrain([("idle", {}), ("idle", {}), ("idle", {}), ("loot", {}), ("loot", {})])   # 2 of 5 = 0.4
        rc, out, _ = self.run_main([], brain=brain)
        self.assertEqual(rc, 1)
        self.assertIn("0.40", out)

    def test_exit_2_when_no_brain_is_configured(self):
        rc, out, err = self.run_main([], no_brain=True)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("Jev", err)

    def test_exit_2_when_the_selected_brain_is_not_jev(self):
        rc, out, err = self.run_main([], brain=StubBrain(jev=None))
        self.assertEqual(rc, 2)
        self.assertIn("Jev", err)

    def test_only_jev_decides_even_when_an_llm_is_configured(self):
        brain = StubBrain()
        brain.llm = object()
        self.run_main([], brain=brain)
        self.assertIsNone(brain.llm)

    def test_a_failed_decision_is_reported_not_counted(self):
        from agent.brain import BrainError

        class Failing(StubBrain):
            def decide(self, *a, **kw):
                raise BrainError("jev cooling down", None)

        rc, _, err = self.run_main([], brain=Failing())
        self.assertEqual(rc, 1)
        self.assertIn("jev cooling down", err)

    def test_n_below_one_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            self.run_main(["--n", "0"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
