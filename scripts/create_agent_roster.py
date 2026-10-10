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
sys.path.insert(0, str(Path(__file__).resolve().parent))

ROSTER_PATH = REPO / "agents" / "roster.json"
FIRST_PORT = 9601          # agent N publishes its observability API on 9600 + N
PARTY_SIZE = 5             # compose profile `party`; `raid` is everyone
TOTAL = 25

# Horde races (SharedDefines.h Races) -> the classes a fresh account can create.
# Death Knight (6) is left out on purpose, see the module docstring.
WARRIOR, PALADIN, HUNTER, ROGUE, PRIEST, SHAMAN, MAGE, WARLOCK, DRUID = 1, 2, 3, 4, 5, 7, 8, 9, 11
HORDE = {
    2:  ("Orc",       [WARRIOR, HUNTER, ROGUE, SHAMAN, WARLOCK]),
    5:  ("Undead",    [WARRIOR, ROGUE, PRIEST, MAGE, WARLOCK]),
    6:  ("Tauren",    [WARRIOR, HUNTER, SHAMAN, DRUID]),
    8:  ("Troll",     [WARRIOR, HUNTER, ROGUE, PRIEST, SHAMAN, MAGE]),
    10: ("Blood Elf", [WARRIOR, PALADIN, HUNTER, ROGUE, PRIEST, MAGE, WARLOCK]),
}
CLASS_NAMES = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest",
               7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}

# AGENT01..05 predate the roster. Their race/class/gender are null here and are read
# back from the realm on a real run (the characters are not in this repo's data).
LEGACY = [("AGENT01", "Luaprata"), ("AGENT02", "Farstrider"), ("AGENT03", "Shadowblade"),
          ("AGENT04", "Sunspeaker"), ("AGENT05", "Spellweaver")]
PLAN_SEED = 63             # seed used for the committed agents/roster.json (UM-63)

# Syllables per race, so names read like the race: "Grokhar", "Mulgore-ish", "Sylvaris".
_SYLLABLES = {
    2:  (["Gar", "Gro", "Thok", "Dra", "Kar", "Mog", "Ur", "Zug", "Bor"], ["hak", "gash", "mar", "nak", "thar", "dum", "gor"]),
    5:  (["Mor", "Vel", "Sar", "Ner", "Thal", "Ves", "Dal", "Cor"], ["wyn", "vis", "ric", "den", "gar", "lock", "mund"]),
    6:  (["Ba", "Ru", "Ma", "Ke", "Ta", "Hu", "Pa", "Mu"], ["hotah", "kuna", "wak", "hawk", "toka", "nuk", "rana"]),
    8:  (["Zul", "Jin", "Vol", "Rok", "Zan", "Sen", "Mah", "Tiki"], ["jin", "tar", "ja", "kazi", "ari", "ton", "rik"]),
    10: (["Sil", "Quel", "Aer", "Thal", "Lor", "Kael", "Anar", "Vel"], ["varis", "dorei", "ian", "anar", "eth", "thas", "lian"]),
}


def valid_name(name: str) -> bool:
    """2-12 ASCII letters, no letter three times in a row (TC's name rules)."""
    if not (2 <= len(name) <= 12) or not name.isascii() or not name.isalpha():
        return False
    lower = name.lower()
    return not any(lower[i] == lower[i + 1] == lower[i + 2] for i in range(len(lower) - 2))


def gen_name(rng: random.Random, race: int, taken: set[str]) -> str:
    pre, post = _SYLLABLES[race]
    for _ in range(500):
        name = rng.choice(pre) + rng.choice(post)
        if rng.random() < 0.3:
            name = rng.choice(pre) + rng.choice(pre).lower() + rng.choice(post)
        if valid_name(name) and name.lower() not in taken:
            return name
    raise RuntimeError(f"could not generate a free name for race {race}")


def account_name(n: int) -> str:
    return f"AGENT{n:02d}"


def plan_agents(rng: random.Random, existing: list[dict], total: int = TOTAL) -> list[dict]:
    """Roster entries for AGENT01..AGENT<total>. Existing entries are kept as they are;
    missing ones get a random Horde race, a class valid for it, a gender and a name."""
    legacy = [{"account": a, "character": c, "race": None, "class": None, "gender": None}
              for a, c in LEGACY]
    by_account = {a["account"]: a for a in legacy + existing}   # the roster file wins
    taken = {a["character"].lower() for a in by_account.values()}
    out = []
    for n in range(1, total + 1):
        acc = account_name(n)
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
    path = path or ROSTER_PATH          # resolved per call so tests can redirect it
    if not path.exists():
        return []
    return json.loads(path.read_text())["agents"]


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


class Console:
    """Fire-and-forget `account create` over the socket.io console of scripts/wow_console.py.

    Unlike wow_console.main() this never prints the command (it carries a password)
    nor the server's reply; callers confirm by logging in afterwards."""

    def __init__(self, host: str):
        self.host = host

    def create_account(self, account: str, password: str) -> None:
        import wow_console as wc
        wc.BASE = f"http://{self.host}:{wc.PORT}/socket.io/"
        with wc.urllib.request.urlopen(f"{wc.BASE}?EIO=4&transport=polling&t={int(time.time() * 1000)}",
                                       timeout=10) as r:
            hs = r.read().decode()
        sid = json.loads(hs[hs.find("{"):])["sid"]
        wc.post(sid, "40")
        time.sleep(0.5)
        wc.get(sid)
        wc.post(sid, "42" + json.dumps(["worldserver_input", f"account create {account} {password}"]))
        time.sleep(2.0)


class Realm:
    """One authenticated world session for an account (no character selected)."""

    def __init__(self, host: str, auth_port: int):
        self.host, self.auth_port = host, auth_port

    def open(self, account: str, password: str):
        """Returns a connected WoWSession, or None if the account/password is rejected."""
        from agent.auth import AuthRejected, auth_logon
        from agent.session import WoWSession
        try:
            acc, key, realms = auth_logon(self.host, self.auth_port, account, password)
        except AuthRejected:
            return None
        realm = list(realms.values())[0]
        world_host, port = realm["address"].rsplit(":", 1)
        sess = WoWSession(world_host, int(port), acc, key, realm["id"])
        sess.connect()
        return sess


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
