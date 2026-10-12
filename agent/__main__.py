#!/usr/bin/env python3
"""Entry point: python3 -m agent [--list-chars|--dry-run|--once]
Master `docker run` entrypoint: reads env, validates config, decides what to do.

Modes:
  --list-chars   authenticate, list characters, exit
  --dry-run      auth + login + perception 30s, then exit
  --once         one full think cycle (perceive → act) and exit
  (default)      run the agent loop forever

Arguments, config, logging and signals live here. The wiring is agent/root.py (#292).
"""

import argparse
import logging
import signal
import sys
import threading

from .auth import AuthRejected
from .config import ConfigError, load_config
from .root import build_agent, dry_run, list_characters


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
        if not list_characters(cfg, log):
            sys.exit(1)
        return

    duration = args.duration or cfg.run_duration
    if duration <= 0:
        duration = 30.0 if args.dry_run else None  # None = forever

    if args.dry_run:
        if not dry_run(cfg, log, duration):
            sys.exit(1)
        return

    agent = build_agent(cfg, log, duration, perception_dump=args.perception_dump)
    stop = _install_stop_signals()  # after the wiring, as before #292: a signal during it still kills at once
    log.info("entering agent loop (SIGTERM or ctrl+c to stop) ...")
    try:
        agent.run(stop)
    except AuthRejected:
        sys.exit(1)  # #430: the supervisor already logged the one line
    log.info("done.")


if __name__ == "__main__":
    main()
