"""Helpers that give a test back the process state it borrowed (#684).

Not a test module (no `test_` prefix). Call from `setUp`:

    reset_counters(self)    # a test that asserts on a metrics counter's value
    join_new_threads(self)  # a test whose code under test starts a thread it cannot name
"""

import threading
import unittest

from agent import metrics

JOIN_TIMEOUT_S = 5.0


def reset_counters(test: unittest.TestCase) -> None:
    """Start the test with ROUTER_COUNTS, SWALLOWED and RECONNECTS at their initial values and put back what was there."""
    for counter, initial in ((metrics.ROUTER_COUNTS, {}), (metrics.SWALLOWED, {}),
                             (metrics.RECONNECTS, dict.fromkeys(metrics.RECONNECT_REASONS, 0))):
        saved = dict(counter)
        counter.clear()
        counter.update(initial)
        test.addCleanup(lambda c=counter, s=saved: (c.clear(), c.update(s)))


def join_new_threads(test: unittest.TestCase) -> None:
    """At cleanup, wait (bounded) for every thread started since now to end; fail the test if one does not.

    Registered first, so it runs after the test's own cleanups (which may be what releases the thread)."""
    before = set(threading.enumerate())

    def check() -> None:
        stuck = []
        for t in threading.enumerate():
            if t not in before and t is not threading.current_thread():
                t.join(JOIN_TIMEOUT_S)
                if t.is_alive():
                    stuck.append(t.name)
        if stuck:
            test.fail(f"threads still alive after {JOIN_TIMEOUT_S:g}s: {stuck}")

    test.addCleanup(check)
