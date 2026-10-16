"""Staging smoke (#795): the agent's own login code against the staging realm, run by hand.

    WOW_HOST=<wow-agents IP> WOW_AUTH_PORT=3725 WOW_ACCOUNT=STAGING01 WOW_PASSWORD=... \
        python3 -m tools.agent_dev.staging_smoke

Steps: login, enter world, logout, reconnect, enter world, logout. Each prints PASS or FAIL. Exit 0
when all pass, 1 when one fails, 2 for a bad configuration. It refuses the live realm (192.168.1.64,
auth 3724) and never prints the password. Not in CI against a real realm: its unit tests run against
a stub login (tools/tests/test_staging_smoke.py).
"""
import contextlib
import logging
import os
import sys

LIVE_HOST = "192.168.1.64"
LIVE_AUTH_PORT = 3724
REQUIRED = ("WOW_HOST", "WOW_AUTH_PORT", "WOW_ACCOUNT", "WOW_PASSWORD")


def config_problems(env) -> list[str]:
    """Problems with the environment (empty = OK). WOW_HOST and WOW_AUTH_PORT must be set: the
    agent's defaults are the live realm."""
    problems = [f"{name} is required" for name in REQUIRED if not env.get(name)]
    if problems:
        return problems
    if not env["WOW_AUTH_PORT"].isdigit():
        return [f"WOW_AUTH_PORT must be a port number, got {env['WOW_AUTH_PORT']!r}"]
    if env["WOW_HOST"] == LIVE_HOST or int(env["WOW_AUTH_PORT"]) == LIVE_AUTH_PORT:
        return [f"refusing the live realm ({LIVE_HOST}:{LIVE_AUTH_PORT}): this smoke runs against staging"]
    return []


def _connect_and_login(cfg, log):
    from agent.login import connect_and_login
    return connect_and_login(cfg, log)


def _load_config():
    from agent.config import load_config
    return load_config()


def _enter_world(sess):
    if not sess.player_guid:
        raise RuntimeError("no character guid after login")


def _logout(sess):
    sess.logout()
    if sess.unexpected_disconnect:
        raise RuntimeError("the server dropped the session instead of answering the logout")


def run(cfg, log, login=_connect_and_login) -> bool:
    """login, enter world, logout, reconnect, enter world, logout: one PASS or FAIL line per step.
    Stops at the first FAIL. Any session still open when it stops is logged out."""
    open_sessions = []

    def attempt(name, fn, *args):
        try:
            value = fn(*args)
        except Exception as e:  # noqa: BLE001 - a smoke step reports any failure and stops
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            return False, None
        print(f"PASS {name}")
        return True, value

    try:
        for label in ("", " (2)"):
            ok, sess = attempt("login" if not label else "reconnect", login, cfg, log)
            if not ok:
                return False
            open_sessions.append(sess)
            if not attempt("enter world" + label, _enter_world, sess)[0]:
                return False
            if not attempt("logout" + label, _logout, sess)[0]:
                return False
            open_sessions.remove(sess)
        return True
    finally:
        # cleanup only: a failed step has already been reported
        for sess in open_sessions:
            with contextlib.suppress(Exception):
                sess.logout()


def main(env=os.environ, load=_load_config, login=_connect_and_login) -> int:
    problems = config_problems(env)
    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING)
    return 0 if run(load(), logging.getLogger("staging_smoke"), login) else 1


if __name__ == "__main__":
    sys.exit(main())
