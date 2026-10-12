"""Typed loader for agents/roster.json: validation and the names and ports derived from it (#557).

Pure and stdlib-only. The generators under scripts/ and tools/agent-runner read the roster
through it (#391). Agent N (1-based position in the file) logs in on account_name(N), serves
its read-only API on FIRST_PORT + N - 1, and its service and container names come from the
character name.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
ROSTER_PATH = REPO / "agents" / "roster.json"
FIRST_PORT = 9601
PARTY_SIZE = 5
KNOWN_KEYS = frozenset({"account", "character", "race", "class", "gender"})
_ACCOUNT = re.compile(r"AGENT(0[1-9]|[1-9][0-9])")


class RosterError(ValueError):
    """The roster file breaks one of the validation rules; the message names the entry and field."""


@dataclass(frozen=True)
class Agent:
    index: int               # 1-based position in the file
    account: str
    character: str
    race: int | None
    class_id: int | None     # JSON key "class"
    gender: int | None

    @property
    def slug(self) -> str:
        return self.character.lower()

    @property
    def service(self) -> str:
        return "agent-" + self.slug

    @property
    def container(self) -> str:
        return "wow-agent-" + self.slug

    @property
    def api_port(self) -> int:
        return FIRST_PORT + self.index - 1

    @property
    def profiles(self) -> tuple[str, ...]:
        return ("party", "raid") if self.index <= PARTY_SIZE else ("raid",)

    def to_json(self) -> dict[str, Any]:
        return {"account": self.account, "character": self.character, "race": self.race,
                "class": self.class_id, "gender": self.gender}


def account_name(index: int) -> str:
    """The account of agent `index` (1-based): AGENT01, AGENT02, ..."""
    return f"AGENT{index:02d}"


def _fail(index: int, field: str, problem: str) -> RosterError:
    return RosterError(f"roster entry {index}: field {field!r} {problem}")


def _optional_int(entry: dict, index: int, field: str) -> int | None:
    value = entry[field]
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise _fail(index, field, f"must be an integer or null, got {value!r}")


def _parse_entry(entry: object, index: int, seen_accounts: set[str], seen_names: set[str]) -> Agent:
    if not isinstance(entry, dict):
        raise RosterError(f"roster entry {index}: must be an object, got {type(entry).__name__}")
    missing, unknown = sorted(KNOWN_KEYS - entry.keys()), sorted(entry.keys() - KNOWN_KEYS)
    if missing or unknown:
        raise RosterError(f"roster entry {index}: keys must be exactly {sorted(KNOWN_KEYS)}; "
                          f"missing {missing}, unknown {unknown}")

    account = entry["account"]
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        raise _fail(index, "account", f"must look like AGENT01..AGENT99, got {account!r}")
    if account in seen_accounts:
        raise _fail(index, "account", f"duplicates {account!r}")
    seen_accounts.add(account)

    character = entry["character"]
    if not isinstance(character, str) or not character or not character.isascii() or not character.isalnum():
        raise _fail(index, "character", f"must be a non-empty ASCII alphanumeric name, got {character!r}")
    if character.lower() in seen_names:
        raise _fail(index, "character", f"duplicates {character!r} (names are case-insensitive)")
    seen_names.add(character.lower())

    return Agent(index=index, account=account, character=character,
                 race=_optional_int(entry, index, "race"),
                 class_id=_optional_int(entry, index, "class"),
                 gender=_optional_int(entry, index, "gender"))


def parse_roster(data: object) -> list[Agent]:
    """Validate the decoded roster JSON and return its agents in file order."""
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        raise RosterError("roster must be an object with an 'agents' list")
    seen_accounts: set[str] = set()
    seen_names: set[str] = set()
    return [_parse_entry(entry, index, seen_accounts, seen_names)
            for index, entry in enumerate(data["agents"], start=1)]


def load_roster(path: Path = ROSTER_PATH) -> list[Agent]:
    return parse_roster(json.loads(path.read_text(encoding="utf-8")))
