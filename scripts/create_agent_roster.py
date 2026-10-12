#!/usr/bin/env python3
"""Create the agent roster: accounts AGENT06..AGENT25, one random Horde character each (UM-63).

`agents/roster.json` is the source of truth: it lists every agent (account,
character, race, class) and `scripts/gen_agents_compose.py` renders
`docker-compose.agents.yml` from it. A fresh clone already has the 20 planned
characters in it, so reviewers can read the names and classes before anything
touches the server.

    python3 scripts/create_agent_roster.py --dry-run       # print the plan; no network, writes nothing
    python3 scripts/create_agent_roster.py --write-plan    # write agents/roster.json; no network
    python3 scripts/create_agent_roster.py                 # create what is missing on the realm

A real run is idempotent. For each roster entry it logs in as the account; if
the login fails it creates the account through the worldserver console
(`account create`, same path as scripts/wow_console.py) and logs in again. If the
account has no character yet it creates the planned one over the 3.3.5a
protocol (CMSG_CHAR_CREATE, WoWSession.create_character). Accounts that already
have a character are left alone, and their real name/race/class are written
back into the roster.

Passwords: every agent account uses the shared AGENT_PASSWORD from `.env`, the
same one docker-compose.agents.yml already passes to AGENT01..AGENT05. It is
read from the environment and never printed; console output is not echoed.

Rules (verified against TrinityCore 3.3.5a CharacterHandler.cpp):
  * Death Knights are excluded: creating one needs an existing level 55+
    character on the same account (CONFIG_CHARACTER_CREATING_MIN_LEVEL_FOR_
    DEATH_KNIGHT), which a fresh AGENTnn account never has.
  * Names are 2-12 letters; the server answers CHAR_CREATE_NAME_IN_USE (50) or
    CHAR_NAME_RESERVED when a name is taken, and the script then draws a new one.
"""
import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agents import roster  # noqa: E402
# valid_name is not used here: it stays readable as create_agent_roster.valid_name for the tests.
from agents.character_rules import CLASS_NAMES, HORDE, gen_name, valid_name  # noqa: E402, F401
from agents.realm import Console, Realm  # noqa: E402

ROSTER_PATH = roster.ROSTER_PATH
TOTAL = 25

# AGENT01..05 predate the roster. Their race/class/gender are null here and are read
# back from the realm on a real run (the characters are not in this repo's data).
LEGACY = [("AGENT01", "Luaprata"), ("AGENT02", "Farstrider"), ("AGENT03", "Shadowblade"),
          ("AGENT04", "Sunspeaker"), ("AGENT05", "Spellweaver")]
PLAN_SEED = 63             # seed used for the committed agents/roster.json (UM-63)


def plan_agents(rng: random.Random, existing: list[dict], total: int = TOTAL) -> list[dict]:
    """Roster entries for AGENT01..AGENT<total>. Existing entries are kept as they are;
    missing ones get a random Horde race, a class valid for it, a gender and a name."""
    legacy = [{"account": a, "character": c, "race": None, "class": None, "gender": None}
              for a, c in LEGACY]
    by_account = {a["account"]: a for a in legacy + existing}   # the roster file wins
    taken = {a["character"].lower() for a in by_account.values()}
    out = []
    for n in range(1, total + 1):
        acc = roster.account_name(n)
        if acc in by_account:
            out.append(by_account[acc])
            continue
        race = rng.choice(sorted(HORDE))
        class_ = rng.choice(HORDE[race][1])
        name = gen_name(rng, race, taken)
        taken.add(name.lower())
        out.append({"account": acc, "character": name, "race": race, "class": class_,
                    "gender": rng.randint(0, 1)})
    return out


def load_roster(path: Path | None = None) -> list[dict]:
    """The validated entries as the five JSON keys, [] when the file does not exist yet.
    A malformed file raises roster.RosterError."""
    path = path or ROSTER_PATH          # resolved per call so tests can redirect it
    if not path.exists():
        return []
    return [a.to_json() for a in roster.load_roster(path)]


def save_roster(agents: list[dict], path: Path | None = None) -> None:
    path = path or ROSTER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agents": agents}, indent=2) + "\n")


def describe(a: dict) -> str:
    race = HORDE.get(a.get("race"), ("?",))[0]
    cls = CLASS_NAMES.get(a.get("class"), "?")
    return f"{a['account']}  {a['character']:<12} {race} {cls}"


# ── live side ────────────────────────────────────────────────────────────────

# SharedDefines.h ResponseCodes: CHAR_CREATE_NAME_IN_USE = 50, and CHAR_NAME_FAILURE..
# CHAR_NAME_RUSSIAN_SILENT_CHARACTER_AT_BEGINNING_OR_END = 88..102 (CHAR_NAME_RESERVED = 95).
# All of them mean "pick another name"; anything else is a real failure.
_NAME_RETRY_CODES = {50, *range(88, 103)}


def ensure_agent(entry: dict, password: str, realm, console, rng: random.Random,
                 taken: set[str], log=print, sleep=time.sleep) -> str:
    """Make sure `entry`'s account and character exist. Returns "exists" or "created".
    Updates `entry` in place with what is really on the server."""
    acc = entry["account"]
    sess = realm.open(acc, password)
    created = False
    if sess is None:
        console.create_account(acc, password)
        for _ in range(5):                      # the console answers before the DB row is visible
            sess = realm.open(acc, password)
            if sess is not None:
                break
            sleep(2)
        else:
            raise RuntimeError(f"{acc}: login still fails after `account create`")
        created = True
    try:
        chars = sess.enum_characters()
        if chars:
            c = chars[0]
            entry.update(character=c["name"], race=c["race"], gender=c["gender"],
                         **{"class": c["class_"]})
            return "exists"
        if entry.get("race") is None:
            raise RuntimeError(f"{acc}: no character on the account and none planned in the roster")
        for _ in range(10):
            try:
                sess.create_character(entry["character"], entry["race"], entry["class"],
                                      entry.get("gender") or 0)
                log(f"  {acc}: created {entry['character']}" + (" (new account)" if created else ""))
                return "created"
            except RuntimeError as e:
                m = re.search(r"response code (\d+)", str(e))
                if not m or int(m.group(1)) not in _NAME_RETRY_CODES:
                    raise
                entry["character"] = gen_name(rng, entry["race"], taken)
                taken.add(entry["character"].lower())
        raise RuntimeError(f"{acc}: no free name after 10 tries")
    finally:
        sess.logout()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="print the plan and exit; no network, nothing written")
    mode.add_argument("--write-plan", action="store_true",
                      help="write the plan to agents/roster.json and exit; no network")
    p.add_argument("--total", type=int, default=TOTAL, help="agents in the roster (default 25)")
    p.add_argument("--seed", type=int, default=None,
                   help=f"seed for planning new entries (default {PLAN_SEED} with --write-plan, else random)")
    p.add_argument("--host", default=os.environ.get("WOW_HOST", "192.168.1.64"))
    p.add_argument("--auth-port", type=int, default=int(os.environ.get("WOW_AUTH_PORT", "3724")))
    args = p.parse_args(argv)

    seed = args.seed if args.seed is not None or not args.write_plan else PLAN_SEED
    rng = random.Random(seed)
    on_roster = load_roster()
    agents = plan_agents(rng, on_roster, args.total)
    legacy = {a for a, _ in LEGACY}

    if args.dry_run or args.write_plan:
        planned = [a for a in agents if a["account"] not in legacy]
        print(f"{len(agents)} agents: {len(agents) - len(planned)} pre-existing, "
              f"{len(planned)} planned (seed {seed}; roster entries already in {ROSTER_PATH.name} win)")
        for a in agents:
            print(("  + " if a in planned else "    ") + describe(a))
        if args.write_plan:
            save_roster(agents)
            print(f"wrote agents/{ROSTER_PATH.name}; no connection made")
        else:
            print("dry run: nothing written, no connection made")
        return 0

    password = os.environ.get("AGENT_PASSWORD", "")
    if not password:
        print("AGENT_PASSWORD is not set (see .env.example)", file=sys.stderr)
        return 2
    realm, console = Realm(args.host, args.auth_port), Console(args.host)
    taken = {a["character"].lower() for a in agents}
    counts = {"created": 0, "exists": 0}
    try:
        for entry in agents:
            counts[ensure_agent(entry, password, realm, console, rng, taken)] += 1
    finally:
        save_roster(agents)        # keep what was learned even if a later agent failed
    print(f"done: {counts['created']} created, {counts['exists']} already there; "
          f"wrote agents/{ROSTER_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
