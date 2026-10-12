"""Reconnect supervisor (UM-43). #292: moved out of agent/__main__.py, unchanged."""

import functools
import random
import threading
from collections.abc import Callable

from .auth import PERMANENT_AUTH_CODES, AuthRejected
from .metrics import count_reconnect, swallowed

INITIAL_RECONNECT_BACKOFF_S = 5.0
MAX_RECONNECT_BACKOFF_S = 300.0


def equal_jitter(backoff: float, rng: random.Random) -> float:
    """#402: the wait for one backoff step, uniform in [backoff / 2, backoff]. It never exceeds
    the backoff, so jitter only spreads the waits out; the backoff sequence itself is unchanged."""
    return rng.uniform(backoff / 2, backoff)


def supervise_connection(build_session, run_session, log,
                           sleep=None, stop: threading.Event | None = None,
                           initial_backoff: float = INITIAL_RECONNECT_BACKOFF_S,
                           max_backoff: float = MAX_RECONNECT_BACKOFF_S,
                           jitter: Callable[[float], float] | None = None):
    """Reconnect supervisor (UM-43). `build_session()` does auth+connect+
    login and returns a fresh WoWSession (and therefore fresh WorldState);
    `run_session(session)` drives one full agent run and returns True if it
    ended because of an unexpected disconnect (recv thread death,
    ConnectionError, or an unrequested SMSG_LOGOUT_COMPLETE — see
    agent.session.WoWSession.unexpected_disconnect) or False for a clean/
    requested stop (duration elapsed). Backoff is exponential, 5s -> 5min
    cap, reset to the initial value after each successful (re)connect.

    This is the inner layer on top of the outer `docker restart:
    unless-stopped`: it retries transient drops without the container
    itself needing to restart. SIGTERM/SIGINT (#401) set `stop`: the per-attempt
    session.logout() below still runs (via finally) and the supervisor returns
    instead of reconnecting.

    `sleep` is injectable so tests can assert on backoff timing without
    actually waiting (see agent/tests/test_reconnect.py). By default the backoff
    waits on `stop` (#401): once it is set (SIGTERM) the supervisor logs out the
    current session and returns instead of reconnecting.

    #402: each wait is `jitter(backoff)`, by default equal jitter from a per-supervisor
    OS-seeded RNG, so a fleet restarting together does not retry in lockstep. Jitter
    changes only the wait: `backoff` keeps doubling from the un-jittered value. Every
    reconnect wait is counted once under its reason (agent.metrics.count_reconnect)."""
    stop = stop or threading.Event()
    sleep = sleep or stop.wait
    if jitter is None:
        rng = random.Random()  # OS-seeded: each supervisor draws its own waits
        jitter = functools.partial(equal_jitter, rng=rng)
    backoff = initial_backoff
    attempt = 0
    while True:
        attempt += 1
        try:
            session = build_session()
        except Exception as e:
            if isinstance(e, AuthRejected) and e.code in PERMANENT_AUTH_CODES:
                # #430: a refusal is one readable line, never a traceback
                log.error("%s — not retrying", e)
                raise
            delay = jitter(backoff)
            if isinstance(e, AuthRejected):
                count_reconnect("auth_rejected")
                log.warning("%s — retrying in %.0fs", e, delay)
            else:
                count_reconnect("connect_failed")
                if isinstance(e, OSError):  # #571: an expected network error is one line, no traceback
                    log.warning("connect failed (%s: %s), attempt %d; retrying in %.0fs", type(e).__name__, e, attempt, delay)
                else:
                    swallowed("main.connect", log, f"attempt {attempt}")
                    log.warning("retrying in %.0fs", delay)
            sleep(delay)
            if stop.is_set():
                return
            backoff = min(backoff * 2, max_backoff)
            continue

        backoff = initial_backoff  # reset after a successful (re)connect
        try:
            disconnected = run_session(session)
        finally:
            try:
                session.logout()
            except Exception:  # noqa: BLE001 — a failed logout must not stop the supervisor
                swallowed("main.logout", log, "after this session attempt")

        if not disconnected or stop.is_set():
            return
        delay = jitter(backoff)
        count_reconnect("disconnected")
        log.warning("connection lost unexpectedly — reconnecting in %.0fs", delay)
        sleep(delay)
        if stop.is_set():
            return
        backoff = min(backoff * 2, max_backoff)
