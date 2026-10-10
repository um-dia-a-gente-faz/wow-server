"""One process-wide "something perceivable changed" signal (issue #305).

The recv thread calls notify() after it records an event or applies an
update-object block (never while holding another lock, ADR 0009); an action
blocked in agent.actions.base._wait_for wakes at once instead of at its next
poll tick. A generation counter closes the lost-wakeup window: a waiter takes
generation() BEFORE it evaluates its predicate and wait()s only while that
value is unchanged, so a notify() landing in between is not missed. Spurious
wake-ups are harmless: the waiter re-evaluates its predicate. The condition
lock is only ever held for the counter, never while taking another lock.
"""

import threading

_cond = threading.Condition()
_generation = 0


def generation() -> int:
    with _cond:
        return _generation


def notify() -> None:
    global _generation
    with _cond:
        _generation += 1
        _cond.notify_all()


def wait(seen: int, timeout: float) -> None:
    """Block until generation() != `seen` or `timeout` seconds pass."""
    with _cond:
        _cond.wait_for(lambda: _generation != seen, timeout)
