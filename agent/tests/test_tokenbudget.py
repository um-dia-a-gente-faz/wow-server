"""agent.tokenbudget (#454): one rolling-hour budget, shared by every decider."""
import unittest

from agent import brain, tokenbudget


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeJev:
    model = "m"
    base_url = "u"
    last_latency_ms = 1.0
    last_confidence = 1.0
    last_usage = {"input_tokens": 40, "output_tokens": 10}

    def choose_action(self, snapshot, options, persona="", history=None):
        return "idle", {}


class TokenBudgetTest(unittest.TestCase):
    def test_off_at_zero(self):
        b = tokenbudget.TokenBudget(0)
        b.record(10**9)
        self.assertIsNone(b.exhausted())

    def test_exhausts_at_the_cap_and_names_the_setting(self):
        b = tokenbudget.TokenBudget(100, Clock())
        b.record(99)
        self.assertIsNone(b.exhausted())
        b.record(1)
        self.assertIn("AGENT_MAX_TOKENS_PER_HOUR", b.exhausted() or "")
        self.assertIn("100 of 100", b.exhausted() or "")

    def test_window_rolls(self):
        c = Clock()
        b = tokenbudget.TokenBudget(100, c)
        b.record(100)
        c.t += tokenbudget.WINDOW_S - 1
        self.assertIsNotNone(b.exhausted())
        c.t += 1
        self.assertIsNone(b.exhausted())

    def test_preload_counts(self):
        c = Clock()
        b = tokenbudget.TokenBudget(100, c)
        b.preload([(c.t - 10, 60), (c.t - 20, 60)])
        self.assertEqual(b.used(), 120)

    def test_two_deciders_sharing_one_budget_exhaust_it_together(self):
        c = Clock()
        shared = tokenbudget.TokenBudget(100, c)
        a = brain.Brain(jev=FakeJev(), clock=c, budget=shared)
        b = brain.Brain(jev=FakeJev(), clock=c, budget=shared)
        a.decide({})
        b.decide({})   # 50 + 50 = the cap
        for who in (a, b):
            with self.assertRaises(brain.BrainError) as cm:
                who.decide({})
            self.assertEqual(cm.exception.decision.jev_status, brain.JEV_STATUS_BUDGET)

    def test_separate_budgets_do_not_share(self):
        c = Clock()
        one, two = tokenbudget.TokenBudget(10, c), tokenbudget.TokenBudget(10, c)
        one.record(10)
        self.assertIsNotNone(one.exhausted())
        self.assertIsNone(two.exhausted())


if __name__ == "__main__":
    unittest.main()
