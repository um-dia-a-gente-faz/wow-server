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
import logging
import signal
import sys
import threading
import time

from .chat_relay import ChatRelay
from .config import ConfigError, load_config
from .auth import AuthRejected
from .brain import Brain
from .audit import AuditLogger
from .http_api import AgentObserver, start_server
from .login import authenticate_and_login, connect_and_login
from .runloop import run_loop
from .supervisor import supervise_connection


def _setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _install_stop_signals(stop: threading.Event | None = None) -> threading.Event:
    """#401: SIGTERM (docker stop) and SIGINT set the returned event; the loops unwind
    through the supervisor's finally: session.logout() instead of dying with the
    character in world. The first signal also restores the default action (#488), so a
    second ctrl+c / SIGTERM kills a shutdown that hangs."""
    stop = stop or threading.Event()

    def on_signal(*_):
        stop.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, signal.SIG_DFL)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)
    return stop


def main():
    p = argparse.ArgumentParser(prog="wow-agent")
    p.add_argument("--list-chars", action="store_true", help="list characters and exit")
    p.add_argument("--dry-run", action="store_true", help="auth+login, stay 30s, exit")
    p.add_argument("--duration", type=float, default=0.0, help="run for N seconds (0 = forever, env override AGENT_RUN_DURATION_S)")
    p.add_argument("--perception-dump", action="store_true", help="print snapshot() as JSON once per think cycle")
    args = p.parse_args()

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        sys.exit(1)
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
            sess, _chars = authenticate_and_login(cfg, log)
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

    chat_relay = _build_chat_relay(cfg, log)

    if args.dry_run:
        try:
            sess = connect_and_login(cfg, log, chat_relay)
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
        chat_relay.stop()
        log.info("dry-run complete.")
        return

    # #161: the one brain AGENT_BRAIN selects (llm by default; Jev is opt-in); its client
    # missing -> no think step, the agent idles.
    brain = Brain.from_config(cfg)
    _warn_jev_unselected(cfg, log)
    if brain is not None:
        log.info("brain: %s", brain.describe())
    else:
        log.warning("AGENT_BRAIN=%s has no usable config (LLM_BASE_URL/LLM_MODEL or "
                    "JEV_BASE_URL/JEV_API_KEY) — "
                    "think step disabled, agent will idle", cfg.agent_brain)

    audit_logger = AuditLogger(cfg.agent_name, base_dir=cfg.audit_dir,
                                retention_days=cfg.audit_retention_days)
    log.info("audit log: %s (retention %d days)", audit_logger.agent_dir, audit_logger.retention_days)
    if brain is not None:
        brain.restore_jev_spend(cfg.audit_dir, cfg.agent_name)  # #213: budget survives restarts

    observer, http_server = _start_observer(cfg, audit_logger, log, brain=brain)

    stop = _install_stop_signals()
    log.info("entering agent loop (SIGTERM or ctrl+c to stop) ...")
    try:
        supervise_connection(
            build_session=lambda: connect_and_login(cfg, log, chat_relay),
            run_session=lambda sess: run_loop(sess, cfg, duration, perception_dump=args.perception_dump,
                                               brain=brain, audit_logger=audit_logger, observer=observer,
                                               stop=stop),
            log=log, stop=stop,
        )
    except AuthRejected: sys.exit(1)  # #430: the supervisor already logged the one line
    finally:
        chat_relay.stop()
        if http_server is not None:
            http_server.shutdown()
    log.info("done.")


def _warn_jev_unselected(cfg, log):
    """Warn when Jev is configured but AGENT_BRAIN selects another brain, whether
    or not that brain has a usable config."""
    if cfg.jev_enabled and cfg.agent_brain != "jev":
        log.warning("Jev is configured (JEV_BASE_URL/JEV_API_KEY) but not selected: "
                    "AGENT_BRAIN=%s, so Jev will never be called. Set AGENT_BRAIN=jev "
                    "to use it", cfg.agent_brain)


def _start_observer(cfg, audit_logger, log, brain=None):
    """UM-50: start the read-only observability API if AGENT_HTTP_PORT is
    set. Returns (observer, server), both None when it's off. A port that
    can't be bound is logged and the agent plays on without it."""
    if not cfg.http_port:
        return None, None
    if brain is not None:
        observer = AgentObserver(cfg.agent_name, goal=cfg.persona, model=brain.model,
                                 brain="jev" if brain.jev is not None else "llm")
    else:
        observer = AgentObserver(cfg.agent_name, goal=cfg.persona, model=cfg.llm_model)
    observer.control_token = cfg.control_token  # #178: "" keeps the API read-only
    if audit_logger is not None:
        audit_logger.on_record = observer.record_decision
    try:
        server = start_server(observer, cfg.http_bind, cfg.http_port)
    except OSError as e:
        log.error("observability API disabled: cannot bind %s:%d (%s)", cfg.http_bind, cfg.http_port, e)
        return None, None
    return observer, server



def _build_chat_relay(cfg, log) -> ChatRelay:
    """UM-47: one relay per agent process, shared by every (re)connected
    session. Inert unless AGENT_CHAT_RELAY_URL is set."""
    relay = ChatRelay(cfg.chat_relay_url, agent_name=cfg.agent_name,
                      token=cfg.chat_relay_token, timeout=cfg.chat_relay_timeout)
    if relay.enabled:
        relay.start()
        log.info("chat relay: %s", relay.url)
    return relay


if __name__ == "__main__":
    main()