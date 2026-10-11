"""agent.tokenbudget (#454): one rolling-hour budget, shared by every decider.
#505: the budget is safe to share between threads, and the window is pinned."""
import sys
import threading
import unittest

from agent import brain, tokenbudget

THREAD_ITERATIONS = 4000   # bounded: each thread does this many calls, then stops


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

    def test_window_is_one_hour(self):
        # Pinned with literals, not WINDOW_S, so changing the constant fails here.
        c = Clock()
        b = tokenbudget.TokenBudget(100, c)
        b.record(40, at=c.t)
        c.t += 3599
        self.assertEqual(b.used(), 40)
        c.t += 2   # now t+3601
        self.assertEqual(b.used(), 0)

    def test_two_preloads_do_not_count_expired_spend(self):
        # The first preload leaves an old entry; the second adds a newer one
        # that sorts in front of it. Pruning must still drop the old entry.
        c = Clock()
        b = tokenbudget.TokenBudget(1000, c)
        b.preload([(c.t - 3000, 60)])
        b.preload([(c.t - 5, 10)])
        c.t += 700   # cutoff is now past t-3000 and before t-5
        self.assertEqual(b.used(), 10)

    def test_record_and_check_from_two_threads(self):
        # Without the lock, used() iterating the deque while the other thread
        # appends raises "deque mutated during iteration".
        b = tokenbudget.TokenBudget(10**9, Clock())   # cap far away: checks never trip
        errors: list[BaseException] = []

        def record():
            try:
                for _ in range(THREAD_ITERATIONS):
                    b.record(1)
            except BaseException as e:   # noqa: BLE001 - captured to assert on below
                errors.append(e)

        def check():
            try:
                for _ in range(THREAD_ITERATIONS):
                    b.exhausted()
                    b.used()
            except BaseException as e:   # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=record), threading.Thread(target=check)]
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)   # switch threads often, so a race shows up
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
        finally:
            sys.setswitchinterval(old_interval)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(b.used(), THREAD_ITERATIONS)


if __name__ == "__main__":
    unittest.main()
