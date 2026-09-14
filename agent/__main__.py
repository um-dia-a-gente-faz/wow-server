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
import sys
import time

from .config import load_config
from .auth import auth_logon
from .session import WoWSession


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

    # ═══════════════════════════════════════════════════════════
    # Auth
    # ═══════════════════════════════════════════════════════════
    log.info("authenticating to %s:%d as %s", cfg.wow_host, cfg.wow_auth_port, cfg.account)
    account, session_key, realms = auth_logon(cfg.wow_host, cfg.wow_auth_port,
                                               cfg.account, cfg.password)
    if not account:
        log.critical("auth failed — check WOW_ACCOUNT and WOW_PASSWORD")
        sys.exit(1)

    r = list(realms.values())[0]
    world_host, port_s = r['address'].rsplit(':', 1)
    world_port = int(port_s)
    log.info("realm: %s @ %s:%d", r['name'], world_host, world_port)

    # ═══════════════════════════════════════════════════════════
    # World connect
    # ═══════════════════════════════════════════════════════════
    sess = WoWSession(world_host, world_port, account, session_key, r['id'])
    sess.connect()
    chars = sess.enum_characters()

    for c in chars:
        log.info("  [%d] %-16s L%-2d class=%-2d race=%-2d",
                 c['guid'], c['name'], c['level'], c['class_'], c['race'])

    if args.list_chars:
        sess.logout()
        return

    # Resolve character
    choice = None
    if cfg.char_guid:
        choice = next((c for c in chars if c['guid'] == cfg.char_guid), None)
    if not choice and cfg.character:
        choice = next((c for c in chars if c['name'].lower() == cfg.character.lower()), None)
    if not choice and chars:
        choice = chars[0]
    if not choice:
        log.critical("no characters on realm (and nothing to pick)")
        sys.exit(1)

    log.info("logging in: %s (guid %d)", choice['name'], choice['guid'])
    sess.login_character(choice['guid'])
    log.info("online — position %s", sess.player_position)

    # ═══════════════════════════════════════════════════════════
    # Run
    # ═══════════════════════════════════════════════════════════
    duration = args.duration or cfg.run_duration
    if duration <= 0:
        duration = 30.0 if args.dry_run else None  # None = forever

    if args.dry_run:
        log.info("dry-run: sleeping %s s ...", duration)
        time.sleep(duration)
        sess.logout()
        log.info("dry-run complete.")
        return

    log.info("entering agent loop (ctrl+c to stop) ...")
    try:
        _run_loop(sess, cfg, duration)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        sess.logout()
        log.info("done.")


def _run_loop(sess, cfg, duration: float | None):
    log = logging.getLogger("agent")
    start = time.monotonic()

    while duration is None or time.monotonic() - start < duration:
        # ── perceive ───────────────────────────────────────────
        objects = sess.world_state.get_objects()
        if len(objects) > 0:
            log.info("perception: %d objects in world-state", len(objects))

        # ── think (LLM call — placeholder for now) ─────────────

        # ── act (placeholder) ──────────────────────────────────
        # For now: idle.  Real action loop comes with layer 2+3.

        time.sleep(cfg.think_interval)


if __name__ == "__main__":
    main()