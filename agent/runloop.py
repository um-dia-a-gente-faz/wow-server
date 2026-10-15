"""One session attempt: the follow and rest reflex threads and the think loop (ADR 0009).
#292: moved out of agent/__main__.py, unchanged."""

import contextlib
import json
import logging
import threading
import time

from .metrics import swallowed
from .reflexes.follow import get_follow_reflex
from .reflexes.rest import get_rest_reflex
from .think import ThinkState, think_and_act

FOLLOW_REFLEX_TICK_INTERVAL_S = 0.3  # 250-500 ms, per UM-58's card
REST_REFLEX_TICK_INTERVAL_S = 0.3  # same cadence, per UM-43's card


def run_follow_reflex_loop(sess, stop_event: threading.Event,
                             tick_interval: float = FOLLOW_REFLEX_TICK_INTERVAL_S):
    """Runs agent.reflexes.follow's FollowReflex.tick() on its own thread at
    a fixed cadence, independent of the (much slower) LLM think_interval —
    this is what makes it a reflex ("between LLM steps") rather than
    another step of the think loop. One bad tick shouldn't kill the whole
    loop or the recv thread, so exceptions are logged and swallowed."""
    log = logging.getLogger("agent.reflexes.follow")
    reflex = get_follow_reflex(sess)
    while not stop_event.is_set():
        try:
            reflex.tick(sess, sess.world_state)
        except Exception:  # noqa: BLE001 — one bad tick must not end the reflex
            swallowed("reflex.follow_tick", log)
        stop_event.wait(tick_interval)


def run_rest_reflex_loop(sess, stop_event: threading.Event,
                           tick_interval: float = REST_REFLEX_TICK_INTERVAL_S):
    """Same shape as run_follow_reflex_loop, driving agent.reflexes.rest's
    RestReflex.tick() (UM-43) on its own thread."""
    log = logging.getLogger("agent.reflexes.rest")
    reflex = get_rest_reflex(sess)
    while not stop_event.is_set():
        try:
            reflex.tick(sess, sess.world_state)
        except Exception:  # noqa: BLE001 — one bad tick must not end the reflex
            swallowed("reflex.rest_tick", log)
        stop_event.wait(tick_interval)


def run_loop(sess, cfg, duration: float | None, perception_dump: bool = False, brain=None,
              audit_logger=None, observer=None, stop: threading.Event | None = None, unmet=None) -> bool:
    start = time.monotonic()

    reflex_stop = threading.Event()
    reflex_threads = [
        threading.Thread(target=run_follow_reflex_loop, args=(sess, reflex_stop), daemon=True),
        threading.Thread(target=run_rest_reflex_loop, args=(sess, reflex_stop), daemon=True),
    ]
    for t in reflex_threads:
        t.start()
    try:
        return run_think_loop(sess, cfg, duration, start, perception_dump=perception_dump,
                                brain=brain, audit_logger=audit_logger, observer=observer, stop=stop,
                                unmet=unmet)
    finally:
        if observer is not None:
            observer.detach()
        reflex_stop.set()
        for t in reflex_threads:
            t.join(timeout=2.0)


def reflex_state(sess) -> dict:
    """Best-effort snapshot of active reflexes for the audit log — never
    raises, since a reflex's internal shape isn't this loop's business."""
    state = {}
    follow = getattr(sess, "follow_reflex", None)
    if follow is not None and getattr(follow, "enabled", False):
        state["follow"] = {
            "enabled": True,
            "leader_guid": getattr(follow, "leader_guid", None),
            "leader_name": getattr(follow, "leader_name", None),
            "assist": getattr(follow, "assist", False),
        }
    rest = getattr(sess, "rest_reflex", None)
    if rest is not None and getattr(rest, "active", False):
        state["rest"] = {"active": True, "method": getattr(rest, "method", None)}
    return state


def remaining_sleep(cycle_start: float, interval: float, now: float) -> float:
    """Seconds left until the cycle's deadline; 0 (never negative) when it overran."""
    return max(0.0, interval - (now - cycle_start))


def run_think_loop(sess, cfg, duration: float | None, start: float, perception_dump: bool = False,
                     brain=None, audit_logger=None, observer=None,
                     stop: threading.Event | None = None, unmet=None) -> bool:
    """`stop` (#401) ends the loop at once, mid-sleep, as a clean stop (False).
    Returns True if the loop ended because of an unexpected disconnect
    (agent.session.WoWSession.unexpected_disconnect, UM-43 — the reconnect
    supervisor should retry), False for a normal end (duration elapsed)."""
    log = logging.getLogger("agent")
    cycle = 0
    think_state = ThinkState()  # UM-90: recent-action history + loop guard, per session attempt
    if observer is not None:
        observer.attach(sess, think_state, reflex_state)  # UM-50: read-only HTTP view
    while (duration is None or time.monotonic() - start < duration) and not (stop and stop.is_set()):
        cycle_start = time.monotonic()
        if sess.unexpected_disconnect:
            log.warning("world connection dropped unexpectedly — ending this session attempt")
            return True

        # ── perceive ───────────────────────────────────────────
        objects = sess.world_state.get_objects()
        units = sum(1 for o in objects.values() if o.object_type == "unit")
        players = sum(1 for o in objects.values() if o.object_type == "player")
        gameobjects = sum(1 for o in objects.values() if o.object_type == "gameobject")
        if objects:
            log.info("perception: %d objects tracked (%d units, %d players, %d gameobjects)",
                      len(objects), units, players, gameobjects)
            if log.isEnabledFor(logging.DEBUG):
                me = sess.world_state.get_my_object()
                pos = me.position if me else sess.player_position
                nearest = sorted(
                    (o for o in objects.values() if o.guid != sess.player_guid and o.distance_to(pos or ()) is not None),
                    key=lambda o: o.distance_to(pos),
                )[:5]
                for o in nearest:
                    hp_pct = f"{o.health / o.max_health:.0%}" if o.health is not None and o.max_health else "?"
                    log.debug("  %5.1fyd  entry=%s level=%s hp=%s  %s",
                              o.distance_to(pos), o.entry, o.level, hp_pct, o)
        if perception_dump:
            snapshot = sess.world_state.snapshot(my_position=sess.player_position)
            print(json.dumps(snapshot))

        # ── think + act (brain call → one validated action per cycle) ────
        if brain is not None:
            cycle += 1
            # #178: an operator walk (agent/control.py) holds this lock while it moves the
            # character, so a think cycle never starts a competing move mid-walk.
            with (observer.action_lock if observer is not None else contextlib.nullcontext()):
                result = think_and_act(sess, sess.world_state, brain,
                                        persona=cfg.persona, my_position=sess.player_position,
                                        audit_logger=audit_logger, cycle=cycle,
                                        reflex_state=reflex_state(sess), state=think_state, unmet=unmet)
            if not result.ok:
                log.info("think cycle: no action taken (%s)", result.error)

        # #215: sleep to a deadline so the period is the interval, not interval + cycle time.
        wait = remaining_sleep(cycle_start, cfg.think_interval, time.monotonic())
        stop.wait(wait) if stop else time.sleep(wait)

    return False
