"""One process-wide "something perceivable changed" signal (issue #305).

The recv thread calls notify() after it records an event or applies an
update-object block; an action blocked in agent.actions.base._wait_for wakes
at once instead of at its next poll tick. Spurious wake-ups are harmless: the
waiter re-evaluates its predicate.
"""

import threading

_cond = threading.Condition()


def notify() -> None:
    with _cond:
        _cond.notify_all()


def wait(timeout: float) -> None:
    """Block until notify() or `timeout` seconds, whichever is first."""
    with _cond:
        _cond.wait(timeout)
