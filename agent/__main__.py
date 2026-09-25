#!/usr/bin/env python3
"""Entry point: python3 -m agent [--list-chars|--dry-run|--once]
Master `docker run` entrypoint: reads env, validates config, decides what to do.

Modes:
  --list-chars   authenticate, list characters, exit
  --dry-run      auth + login + perception 30s, then exit
  --once         one full think cycle (perceive → act) and exit
  (default)      run the agent loop forever
"""

import argparse
import json
import logging
import sys
import threading
import time

from .channels import parse_channel_spec
from .config import load_config
from .auth import auth_logon
from .session import WoWSession
from .llm import LLMClient
from .think import think_and_act
from .audit import AuditLogger
from .reflexes.follow import get_follow_reflex
from .reflexes.rest import get_rest_reflex


def _setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    p = argparse.ArgumentParser(prog="wow-agent")
    p.add_argument("--list-chars", action="store_true", help="list characters and exit")
    p.add_argument("--dry-run", action="store_true", help="auth+login, stay 30s, exit")
    p.add_argument("--duration", type=float, default=0.0, help="run for N seconds (0 = forever, env override AGENT_RUN_DURATION_S)")
    p.add_argument("--perception-dump", action="store_true", help="print snapshot() as JSON once per think cycle")
    args = p.parse_args()

    cfg = load_config()
    problems = cfg.validate(require_character=not args.list_chars)
    if problems:
        for pr in problems:
            print(f"CONFIG ERROR: {pr}", file=sys.stderr)
        sys.exit(1)

    _setup_logging(cfg.log_level)
    log = logging.getLogger("agent")
    log.info("config: %s", cfg.redacted())

    if args.list_chars:
        try:
            sess, _chars = _authenticate_and_login(cfg, log)
        except RuntimeError as e:
            log.critical("%s", e)
            sys.exit(1)
        sess.logout()
        return

    # ═══════════════════════════════════════════════════════════
    # Run
    # ═══════════════════════════════════════════════════════════
    duration = args.duration or cfg.run_duration
    if duration <= 0:
        duration = 30.0 if args.dry_run else None  # None = forever

    if args.dry_run:
        try:
            sess = _connect_and_login(cfg, log)
        except RuntimeError as e:
            log.critical("%s", e)
            sys.exit(1)
        log.info("dry-run: sleeping %s s ...", duration)
        time.sleep(duration)
        log.info("perception: %d objects tracked (dry-run; recv thread %s, %d packets dropped)",
                 len(sess.world_state.get_objects()),
                 "alive" if sess.recv_thread_alive() else "DEAD",
                 sess.dropped_packets)
        sess.logout()
        log.info("dry-run complete.")
        return

    llm_client = None
    if cfg.llm_base_url and cfg.llm_model:
        llm_client = LLMClient(cfg.llm_base_url, cfg.llm_model, api_key=cfg.llm_api_key)
        log.info("llm: %s @ %s", cfg.llm_model, cfg.llm_base_url)
    else:
        log.warning("LLM_BASE_URL/LLM_MODEL not set — think step disabled, agent will idle")

    audit_logger = AuditLogger(cfg.agent_name, base_dir=cfg.audit_dir,
                                retention_days=cfg.audit_retention_days)
    log.info("audit log: %s (retention %d days)", audit_logger.agent_dir, audit_logger.retention_days)

    log.info("entering agent loop (ctrl+c to stop) ...")
    try:
        _supervise_connection(
            build_session=lambda: _connect_and_login(cfg, log),
            run_session=lambda sess: _run_loop(sess, cfg, duration, perception_dump=args.perception_dump,
                                                llm_client=llm_client, audit_logger=audit_logger),
            log=log,
        )
    except KeyboardInterrupt:
        log.info("interrupted")
    log.info("done.")


def _authenticate_and_login(cfg, log) -> tuple:
    """Auth against the auth server, connect to the world server, and list
    characters. Returns (session, chars) with the session logged in only as
    far as the world connection — no character selected yet. Raises on any
    failure (auth_logon returning no account, a socket error, ...) —
    callers decide how to react (main()'s --list-chars path exits;
    _connect_and_login()/the reconnect supervisor propagate it up)."""
    log.info("authenticating to %s:%d as %s", cfg.wow_host, cfg.wow_auth_port, cfg.account)
    account, session_key, realms = auth_logon(cfg.wow_host, cfg.wow_auth_port,
                                               cfg.account, cfg.password)
    if not account:
        raise RuntimeError("auth failed — check WOW_ACCOUNT and WOW_PASSWORD")

    r = list(realms.values())[0]
    world_host, port_s = r['address'].rsplit(':', 1)
    world_port = int(port_s)
    log.info("realm: %s @ %s:%d", r['name'], world_host, world_port)

    sess = WoWSession(world_host, world_port, account, session_key, r['id'],
                      verbose_packets=cfg.verbose_packets,
                      dump_packets_dir=cfg.dump_packets_dir)
    sess.connect()
    chars = sess.enum_characters()
    for c in chars:
        log.info("  [%d] %-16s L%-2d class=%-2d race=%-2d",
                 c['guid'], c['name'], c['level'], c['class_'], c['race'])
    return sess, chars


def _resolve_character(cfg, chars: list) -> dict:
    choice = None
    if cfg.char_guid:
        choice = next((c for c in chars if c['guid'] == cfg.char_guid), None)
    if not choice and cfg.character:
        choice = next((c for c in chars if c['name'].lower() == cfg.character.lower()), None)
    if not choice and chars:
        choice = chars[0]
    if not choice:
        raise RuntimeError("no characters on realm (and nothing to pick)")
    return choice


def _connect_and_login(cfg, log) -> WoWSession:
    """Full auth -> connect -> character login sequence, returning a
    WoWSession already logged in as the configured (or first available)
    character. Raises on failure. Used directly by main() for --dry-run,
    and as the reconnect supervisor's build_session() for the default
    run-forever mode (UM-43) — each call builds a brand-new WoWSession, and
    therefore a brand-new WorldState (agent.perception), from scratch."""
    sess, chars = _authenticate_and_login(cfg, log)
    choice = _resolve_character(cfg, chars)
    log.info("logging in: %s (guid %d)", choice['name'], choice['guid'])
    sess.race = choice['race']
    sess.class_ = choice['class_']
    sess.login_character(choice['guid'])
    log.info("online — guid %d, position %s", sess.player_guid, sess.player_position)
    # UM-93: the server doesn't auto-join General at login (a real client
    # asks for it), so join the configured channels now. A failed join is
    # logged and otherwise ignored; channel_say just won't list it.
    sess.join_channels(parse_channel_spec(getattr(cfg, "channels", None)))
    return sess


INITIAL_RECONNECT_BACKOFF_S = 5.0
MAX_RECONNECT_BACKOFF_S = 300.0


def _supervise_connection(build_session, run_session, log,
                           sleep=time.sleep,
                           initial_backoff: float = INITIAL_RECONNECT_BACKOFF_S,
                           max_backoff: float = MAX_RECONNECT_BACKOFF_S):
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
    itself needing to restart. KeyboardInterrupt propagates straight out
    (the per-attempt session.logout() below still runs first, via finally)
    rather than being treated as a retryable failure — only main() decides
    that Ctrl+C means "stop for good".

    `sleep` is injectable so tests can assert on backoff timing without
    actually waiting (see agent/tests/test_reconnect.py)."""
    backoff = initial_backoff
    attempt = 0
    while True:
        attempt += 1
        try:
            session = build_session()
        except Exception:
            log.exception("connect/login attempt %d failed", attempt)
            log.warning("retrying in %.0fs", backoff)
            sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            continue

        backoff = initial_backoff  # reset after a successful (re)connect
        try:
            disconnected = run_session(session)
        finally:
            try:
                session.logout()
            except Exception:
                log.exception("error logging out after this session attempt")

        if not disconnected:
            return
        log.warning("connection lost unexpectedly — reconnecting in %.0fs", backoff)
        sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


FOLLOW_REFLEX_TICK_INTERVAL_S = 0.3  # 250-500 ms, per UM-58's card
REST_REFLEX_TICK_INTERVAL_S = 0.3  # same cadence, per UM-43's card


def _run_follow_reflex_loop(sess, stop_event: threading.Event,
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
        except Exception:
            log.exception("follow reflex tick failed")
        stop_event.wait(tick_interval)


def _run_rest_reflex_loop(sess, stop_event: threading.Event,
                           tick_interval: float = REST_REFLEX_TICK_INTERVAL_S):
    """Same shape as _run_follow_reflex_loop, driving agent.reflexes.rest's
    RestReflex.tick() (UM-43) on its own thread."""
    log = logging.getLogger("agent.reflexes.rest")
    reflex = get_rest_reflex(sess)
    while not stop_event.is_set():
        try:
            reflex.tick(sess, sess.world_state)
        except Exception:
            log.exception("rest reflex tick failed")
        stop_event.wait(tick_interval)


def _run_loop(sess, cfg, duration: float | None, perception_dump: bool = False, llm_client=None,
              audit_logger=None) -> bool:
    start = time.monotonic()

    reflex_stop = threading.Event()
    reflex_threads = [
        threading.Thread(target=_run_follow_reflex_loop, args=(sess, reflex_stop), daemon=True),
        threading.Thread(target=_run_rest_reflex_loop, args=(sess, reflex_stop), daemon=True),
    ]
    for t in reflex_threads:
        t.start()
    try:
        return _run_think_loop(sess, cfg, duration, start, perception_dump=perception_dump,
                                llm_client=llm_client, audit_logger=audit_logger)
    finally:
        reflex_stop.set()
        for t in reflex_threads:
            t.join(timeout=2.0)


def _reflex_state(sess) -> dict:
    """Best-effort snapshot of active reflexes for the audit log — never
    raises, since a reflex's internal shape isn't this loop's business."""
    state = {}
    follow = getattr(sess, "_follow_reflex", None)
    if follow is not None and getattr(follow, "enabled", False):
        state["follow"] = {
            "enabled": True,
            "leader_guid": getattr(follow, "leader_guid", None),
            "leader_name": getattr(follow, "leader_name", None),
            "assist": getattr(follow, "assist", False),
        }
    rest = getattr(sess, "_rest_reflex", None)
    if rest is not None and getattr(rest, "active", False):
        state["rest"] = {"active": True, "method": getattr(rest, "method", None)}
    return state


def _run_think_loop(sess, cfg, duration: float | None, start: float, perception_dump: bool = False,
                     llm_client=None, audit_logger=None) -> bool:
    """Returns True if the loop ended because of an unexpected disconnect
    (agent.session.WoWSession.unexpected_disconnect, UM-43 — the reconnect
    supervisor should retry), False for a normal end (duration elapsed)."""
    log = logging.getLogger("agent")
    cycle = 0
    while duration is None or time.monotonic() - start < duration:
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

        # ── think + act (LLM call → one validated action per cycle) ────
        if llm_client is not None:
            cycle += 1
            result = think_and_act(sess, sess.world_state, llm_client,
                                    persona=cfg.persona, my_position=sess.player_position,
                                    audit_logger=audit_logger, cycle=cycle,
                                    reflex_state=_reflex_state(sess))
            if not result.ok:
                log.info("think cycle: no action taken (%s)", result.error)

        time.sleep(cfg.think_interval)

    return False


if __name__ == "__main__":
    main()