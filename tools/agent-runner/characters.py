"""Character half of agent-runner (#138, ADR 0002 D6): create a level-1 agent
character through the protocol, list what the realm has for an account.

Create-only: nothing here deletes a character. One character per account
(AGENT01..AGENT25). The shared account password comes from the runner's
environment (`AGENT_PASSWORD`), is handed to the realm and the worldserver
console only, and is never returned, logged or written to runtime state.
"""

import os
import re
import time
from pathlib import Path
import random
import sys

ACCOUNT_RE = re.compile(r"^AGENT(0[1-9]|1[0-9]|2[0-5])$")
DEATH_KNIGHT = 6
NAME_IN_USE = 50
NAME_REJECTED = range(88, 103)  # SharedDefines.h CHAR_NAME_* codes: the server refused the name
LOGIN_RETRIES = 5
LOGIN_RETRY_DELAY_S = 2.0


def load_script(repo, module):
    """A module from the checkout's scripts/ directory (it puts the repo root on sys.path)."""
    sys.path.insert(0, str(Path(repo) / "scripts"))
    try:
        return __import__(module)
    finally:
        sys.path.pop(0)


class CharacterError(Exception):
    """A refused or failed request; `status` is the HTTP status to answer with."""

    def __init__(self, message, status=400, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


class CharacterService:
    def __init__(self, car, realm_host, realm_auth_port, env=None, sleep=time.sleep, rng=None):
        """`car` is scripts/create_agent_roster.py (or a stand-in): Realm, Console,
        HORDE, CLASS_NAMES, valid_name, gen_name."""
        self.car = car
        self.realm_host, self.realm_auth_port = realm_host, realm_auth_port
        self.env = os.environ if env is None else env
        self.sleep = sleep
        self.rng = rng or random.Random()

    # ── validation ───────────────────────────────────────────────────
    def validate_account(self, account):
        if not isinstance(account, str) or not ACCOUNT_RE.match(account):
            raise CharacterError("account must be one of AGENT01..AGENT25")
        return account

    def validate(self, body):
        """Check a create request. Returns (account, name or None, race, class, gender)."""
        account = self.validate_account(body.get("account"))
        race, class_ = body.get("race"), body.get("class")
        gender = body.get("gender", 0)
        if not _is_int(race) or not _is_int(class_):
            raise CharacterError("race and class must be numbers")
        if gender not in (0, 1) or isinstance(gender, bool):
            raise CharacterError("gender must be 0 (male) or 1 (female)")
        if class_ == DEATH_KNIGHT:
            raise CharacterError("death knights need an existing level-55 character on the account")
        if race not in self.car.HORDE:
            raise CharacterError(f"unsupported race {race}: agents are Horde "
                                 f"({', '.join(f'{r} {n}' for r, (n, _) in self.car.HORDE.items())})")
        race_name, classes = self.car.HORDE[race]
        if class_ not in classes:
            raise CharacterError(f"a {race_name} cannot be class {class_}; allowed: "
                                 + ", ".join(f"{c} {self.car.CLASS_NAMES[c]}" for c in classes))
        name = body.get("name")
        if name is not None and (not isinstance(name, str) or not self.car.valid_name(name)):
            raise CharacterError("name must be 2-12 ASCII letters, no letter three times in a row")
        return account, name, race, class_, gender

    # ── realm ────────────────────────────────────────────────────────
    def _password(self):
        password = self.env.get("AGENT_PASSWORD", "")
        if not password:
            raise CharacterError("AGENT_PASSWORD is not set on the runner", status=503)
        return password

    def _open(self, account, create_account=False):
        """A world session for the account, or None when it does not exist.
        With create_account, make it through the worldserver console first."""
        password = self._password()
        realm = self.car.Realm(self.realm_host, self.realm_auth_port)
        try:
            sess = realm.open(account, password)
            if sess is None and create_account:
                self.car.Console(self.realm_host).create_account(account, password)
                for _ in range(LOGIN_RETRIES):  # the console answers before the row is visible
                    sess = realm.open(account, password)
                    if sess is not None:
                        break
                    self.sleep(LOGIN_RETRY_DELAY_S)
                else:
                    raise CharacterError(f"{account}: login still fails after account create",
                                         status=502)
        except CharacterError:
            raise
        except Exception as e:  # noqa: BLE001 - realm down, auth refused, socket errors
            raise CharacterError(f"realm unreachable or login failed ({type(e).__name__})",
                                 status=502)
        return sess

    @staticmethod
    def _summary(chars):
        return [{"name": c["name"], "race": c["race"], "class": c["class_"],
                 "gender": c["gender"], "level": c["level"]} for c in chars]

    def list(self, account):
        """What the realm has for the account (never creates anything)."""
        self.validate_account(account)
        sess = self._open(account)
        if sess is None:
            return {"account": account, "exists": False, "characters": [], "free_slot": True}
        try:
            chars = self._summary(sess.enum_characters())
        finally:
            sess.logout()
        return {"account": account, "exists": True, "characters": chars,
                "free_slot": not chars}

    def create(self, account, name, race, class_, gender, taken):
        """Create the character. `taken` is the set of lowercase names already in
        the roster (local only: the realm answers for the rest). Returns the
        entry to record: {account, character, race, class, gender, level}."""
        if name is None:
            name = self.car.gen_name(self.rng, race, set(taken))
        if name.lower() in taken:
            raise CharacterError(f"name {name!r} is already used by an agent in the roster", status=409)
        sess = self._open(account, create_account=True)
        try:
            if sess.enum_characters():
                raise CharacterError(f"{account} already has a character (one per account)", status=409)
            try:
                sess.create_character(name, race, class_, gender)
            except RuntimeError as e:
                raise self._create_error(name, e)
            except Exception as e:  # noqa: BLE001 - socket dropped mid-create
                raise CharacterError(f"realm connection failed during create ({type(e).__name__})",
                                     status=502)
            made = next((c for c in sess.enum_characters() if c["name"].lower() == name.lower()), None)
        finally:
            sess.logout()
        if made is None:
            raise CharacterError(f"server accepted {name!r} but the account does not list it", status=502)
        return {"account": account, "character": made["name"], "race": made["race"],
                "class": made["class_"], "gender": made["gender"], "level": made["level"]}

    @staticmethod
    def _create_error(name, exc):
        m = re.search(r"response code (\d+)", str(exc))
        if not m:
            return CharacterError(f"char create failed for {name!r}: {exc}", status=502)
        code = int(m.group(1))
        if code == NAME_IN_USE:
            return CharacterError(f"name {name!r} is already in use on the realm "
                                  f"(CHAR_CREATE_NAME_IN_USE, code {code})", status=409, code=code)
        if code in NAME_REJECTED:
            return CharacterError(f"the realm rejected the name {name!r} (code {code})",
                                  status=400, code=code)
        return CharacterError(f"the realm refused the character (code {code})", status=502, code=code)
