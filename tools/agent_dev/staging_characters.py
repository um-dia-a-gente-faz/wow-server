"""Staging characters (#803): one character on each STAGINGnn account, so the staging smoke can log in.

    WOW_HOST=<wow-agents IP> WOW_AUTH_PORT=3725 STAGING_ACCOUNT_PASSWORD=... \
        python3 -m tools.agent_dev.staging_characters [--accounts STAGING01..STAGING05]

For each account: log in with agent.login.authenticate_and_login and list its characters. An account with
none gets one (WoWSession.create_character), named after the account (STAGING01 -> Stagingone) and an Orc
Warrior. An account that has a character is left alone. Prints one line per account: `created <name>`,
`kept <name>` or `FAIL <account>: <reason>`. Exit 0 when none fails, 1 when one does, 2 for a bad
configuration. It refuses the live realm with the smoke's check (tools/agent_dev/staging_smoke.py) and
never prints the password.
"""
import argparse
import copy
import logging
import os
import re
import sys

from agents.character_rules import valid_name
from tools.agent_dev import staging_smoke as smoke

ACCOUNT_RANGE = "STAGING01..STAGING05"
# ponytail: five words cover STAGING01..05; add the next word when another staging account is seeded.
NUMBER_WORDS = ("one", "two", "three", "four", "five")
RACE, CLASS = 2, 1   # Orc, Warrior: HORDE[2] lists WARRIOR (agents/character_rules.py)


def expand_accounts(spec: str) -> list[str]:
    """`STAGING01..STAGING03` and comma lists (`STAGING04,STAGING01`) -> account names."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\D*)(\d+)\.\.(\D*)(\d+)", part)
        if not m:
            out.append(part)
            continue
        prefix, first, prefix2, last = m.groups()
        if prefix != prefix2 or int(last) < int(first):
            raise ValueError(f"bad account range {part!r}")
        out += [f"{prefix}{n:0{len(first)}d}" for n in range(int(first), int(last) + 1)]
    return out


def name_for(account: str) -> str:
    """STAGING02 -> Stagingtwo: the account's own number as a word, if the character rules allow the name."""
    m = re.search(r"(\d+)$", account)
    n = int(m.group(1)) if m else 0
    if not 1 <= n <= len(NUMBER_WORDS):
        raise ValueError(f"no character name for {account}")
    name = "Staging" + NUMBER_WORDS[n - 1]
    if not valid_name(name):
        raise ValueError(f"{name!r} breaks the character name rules")
    return name


def _authenticate(cfg, log):
    from agent.login import authenticate_and_login
    return authenticate_and_login(cfg, log)


def _load_config():
    from agent.config import load_config
    return load_config()


def _ensure(account: str, cfg, log, login) -> str:
    """Returns the line for this account: `created <name>` or `kept <name>`. Raises on any failure."""
    acc_cfg = copy.copy(cfg)
    acc_cfg.account = account
    sess, chars = login(acc_cfg, log)
    try:
        if chars:
            return f"kept {chars[0]['name']}"
        name = name_for(account)
        sess.create_character(name, RACE, CLASS)
        return f"created {name}"
    finally:
        sess.logout()


def run(accounts, password, cfg, log, login=_authenticate) -> int:
    failed = False
    for account in accounts:
        try:
            print(_ensure(account, cfg, log, login))
        except Exception as e:  # noqa: BLE001 - one account's failure is reported and the others still run
            failed = True
            # the password is redacted from the reason: a login error must not echo it to stdout
            print(f"FAIL {account}: {type(e).__name__}: {e}".replace(password, "***"))
    return 1 if failed else 0


def main(argv=None, env=os.environ, load=_load_config, login=_authenticate) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--accounts", type=expand_accounts, default=ACCOUNT_RANGE,
                   help=f"accounts to seed, a range or a comma list (default {ACCOUNT_RANGE})")
    args = p.parse_args(argv)
    accounts = args.accounts
    password = env.get("STAGING_ACCOUNT_PASSWORD", "")
    if not password:
        problems = ["STAGING_ACCOUNT_PASSWORD is required"]
    else:   # the smoke's own check: WOW_HOST and WOW_AUTH_PORT set, and not the live realm
        problems = smoke.config_problems({"WOW_HOST": env.get("WOW_HOST", ""),
                                          "WOW_AUTH_PORT": env.get("WOW_AUTH_PORT", ""),
                                          "WOW_ACCOUNT": accounts[0], "WOW_PASSWORD": password})
    if problems:
        for prob in problems:
            print(f"error: {prob}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING)
    cfg = copy.copy(load())
    cfg.password = password
    return run(accounts, password, cfg, logging.getLogger("staging_characters"), login)


if __name__ == "__main__":
    sys.exit(main())
