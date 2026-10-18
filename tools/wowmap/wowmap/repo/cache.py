"""Short-TTL, single-flight cache for repo functions (#281).

Concurrent callers of a key with no fresh value wait for one call and share its result.
Exceptions are not cached: they reach every waiter of that call, and the next call retries.
"""
import threading
import time


class _Call:
    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.error = None
        self.expires = 0.0


def ttl_cache(seconds, clock=None):
    """Decorator: cache the result per positional-args key for `seconds`.

    `clock` defaults to time.monotonic, read at call time so tests can patch it.
    Adds `cache_clear()` to the wrapped function. Keys are never evicted, so this suits
    functions called with a few fixed argument sets (ponytail: add LRU when keys grow)."""
    def decorator(fn):
        lock = threading.Lock()
        calls: dict[tuple, _Call] = {}

        def wrapper(*args):
            now = (clock or time.monotonic)
            with lock:
                call = calls.get(args)
                if call is not None and (not call.done.is_set() or now() < call.expires):
                    leader = False  # in flight, or filled and still fresh
                else:
                    call = calls[args] = _Call()
                    leader = True
            if not leader:
                call.done.wait()
                if call.error is not None:
                    raise call.error
                return call.value
            try:
                value = fn(*args)
            except BaseException as e:
                with lock:
                    if calls.get(args) is call:
                        del calls[args]
                call.error = e
                call.done.set()
                raise
            call.value = value
            call.expires = now() + seconds
            call.done.set()
            return value

        def cache_clear():
            with lock:
                calls.clear()

        wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
        return wrapper
    return decorator
