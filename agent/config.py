#!/usr/bin/env python3
"""Agent configuration: everything comes from environment variables.

One container = one agent. No shared state between agents; they only
interact through the game server (chat, party, trade).

SETTINGS is the single declaration of every environment variable the agent
reads (name, type, default, secret?, description, passed by compose?). This is
the only module that touches os.environ (agent/tests/test_config_schema.py bans
it elsewhere) and the tests fail when docker-compose.agents.yml or .env.example
drift from it. Invalid values fail at startup with a ConfigError naming the
variable.
"""

import os
from dataclasses import dataclass, field

# Only used when JEV_PROVIDER=openrouter; see Config.jev_base_url.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/alpha"


class ConfigError(ValueError):
    """An environment variable holds a value that cannot be used."""


@dataclass(frozen=True)
class Setting:
    name: str
    kind: type          # str | int | float | bool
    default: object
    description: str
    secret: bool = False
    compose: bool = True   # passed by docker-compose.agents.yml (False: standalone only or per-service)


def _s(name, kind, default, description, secret=False, compose=True):
    return Setting(name, kind, default, description, secret, compose)


SETTINGS: tuple[Setting, ...] = (
    _s("WOW_HOST", str, "192.168.1.64", "realm host"),
    _s("WOW_AUTH_PORT", int, 3724, "auth server port"),
    _s("WOW_BUILD", int, 12340, "client build, fixed at 12340 for 3.3.5a", compose=False),
    _s("WOW_ACCOUNT", str, "", "game account (set per service by compose)"),
    _s("WOW_PASSWORD", str, "", "game account password", secret=True),
    _s("WOW_CHARACTER", str, "", "character name (set per service by compose)"),
    _s("WOW_CHAR_GUID", int, 0, "character guid; wins over the name, so one global value would point "
       "every service at the same character", compose=False),
    _s("AGENT_NAME", str, "Agent", "agent name (set per service by compose)"),
    _s("AGENT_THINK_INTERVAL_S", float, 3.0, "minimum delay between think-loop decisions, seconds"),
    _s("AGENT_RUN_DURATION_S", float, 0.0, "run for N seconds, 0 = forever"),
    _s("AGENT_PERSONA", str, "", "persona text for the LLM"),
    _s("AGENT_CHANNELS", str, "General", 'chat channels to join after login, comma-separated; "none" disables'),
    _s("AGENT_BRAIN", str, "llm", "llm | jev (#161)"),
    _s("AGENT_BRAIN_FALLBACK", str, "none", "none | llm: only with AGENT_BRAIN=jev"),
    _s("LLM_BASE_URL", str, "", "OpenAI-compatible base URL of the llm brain"),
    _s("LLM_API_KEY", str, "", "key for LLM_BASE_URL", secret=True),
    _s("LLM_MODEL", str, "", "one model id or a comma-separated fallback list"),
    _s("JEV_PROVIDER", str, "", "openrouter opts in to OpenRouter's base URL and OPENROUTER_API_KEY"),
    _s("JEV_BASE_URL", str, "", "Jev Decisions API base URL; Jev is off without it"),
    _s("JEV_PATH", str, "/decisions", "endpoint path appended to JEV_BASE_URL"),
    _s("JEV_API_KEY", str, "", "Jev provider key", secret=True),
    _s("JEV_MODEL", str, "typesafe/jev-1.13", "Jev model id"),
    _s("JEV_MIN_CONFIDENCE", float, 0.0, "below this confidence the safe candidate runs; 0 = rule off"),
    _s("OPENROUTER_API_KEY", str, "", "only read when JEV_PROVIDER=openrouter", secret=True),
    _s("AGENT_MAX_TOKENS_PER_HOUR", int, 0, "Jev token spend cap per agent per rolling hour; 0 = unlimited"),
    _s("LOG_LEVEL", str, "INFO", "python logging level"),
    _s("VERBOSE_PACKETS", bool, False, "log every packet"),
    _s("AGENT_DUMP_PACKETS", str, "", "directory for raw SMSG_UPDATE_OBJECT dumps; empty = off"),
    _s("AGENT_CHAT_RELAY_URL", str, "", "tools/chat-feed URL to mirror heard chat to; empty = off", compose=False),
    _s("AGENT_CHAT_RELAY_TOKEN", str, "", "chat relay bearer token", secret=True, compose=False),
    _s("AGENT_CHAT_RELAY_TIMEOUT_S", float, 3.0, "chat relay HTTP timeout, seconds", compose=False),
    _s("AGENT_AUDIT_DIR", str, "/data/audit", "decision audit log directory (matches the compose volume)",
       compose=False),
    _s("AGENT_AUDIT_RETENTION_DAYS", int, 14, "days of audit logs kept", compose=False),
    _s("AGENT_HTTP_PORT", int, 0, "read-only observability API port; 0 = off (set per service by compose)"),
    _s("AGENT_HTTP_BIND", str, "127.0.0.1", "observability API bind address"),
    _s("AGENT_CONTROL_TOKEN", str, "", "bearer token for POST /control/walk; empty = read-only "
       "(compose passes AGENT_RUNNER_TOKEN)", secret=True),
    _s("CHAT_FEED_TOKEN", str, "", "tools/chat-feed token used by agent/tools/probe.py, run from a shell",
       secret=True, compose=False),
)
_BY_NAME = {s.name: s for s in SETTINGS}
assert len(_BY_NAME) == len(SETTINGS), "duplicate setting in SETTINGS"

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def default(name: str):
    return _BY_NAME[name].default


def get(name: str):
    """The typed value of setting `name`. Unset: the default. Empty: the default
    for int/float/bool, "" for str (so compose can pass an unset var as empty).
    Invalid: ConfigError naming the variable."""
    st = _BY_NAME[name]
    raw = os.environ.get(name)
    if raw is None:
        return st.default
    raw = raw.strip()
    if st.kind is str:
        return raw
    if not raw:
        return st.default
    try:
        if st.kind is bool:
            if raw.lower() in _TRUE:
                return True
            if raw.lower() in _FALSE:
                return False
            raise ValueError
        return st.kind(raw)
    except ValueError:
        raise ConfigError(f"{name} must be {st.kind.__name__}, got {raw!r}") from None


def _lower(name: str) -> str:
    return get(name).lower()


def _f(name: str):
    return field(default_factory=lambda: get(name), repr=not _BY_NAME[name].secret)


@dataclass
class Config:
    # ── Server ────────────────────────────────────────────────
    wow_host: str = _f("WOW_HOST")
    wow_auth_port: int = _f("WOW_AUTH_PORT")
    wow_build: int = _f("WOW_BUILD")

    # ── Account ───────────────────────────────────────────────
    account: str = _f("WOW_ACCOUNT")
    password: str = _f("WOW_PASSWORD")

    # ── Character ─────────────────────────────────────────────
    # Either a name (preferred, human-readable) or a numeric GUID.
    character: str = _f("WOW_CHARACTER")
    char_guid: int = _f("WOW_CHAR_GUID")

    # ── Agent identity / behaviour ────────────────────────────
    agent_name: str = _f("AGENT_NAME")
    think_interval: float = _f("AGENT_THINK_INTERVAL_S")
    run_duration: float = _f("AGENT_RUN_DURATION_S")  # 0 = forever
    persona: str = _f("AGENT_PERSONA")
    # UM-93: chat channels to join after login, comma-separated ("General,world");
    # "none" disables. See agent/channels.py::parse_channel_spec.
    channels: str = _f("AGENT_CHANNELS")

    # ── Brain selection (#161, ADR 0001) ──────────────────────
    # One brain per agent: "llm" (default) or "jev". A Jev failure is a failed
    # cycle unless AGENT_BRAIN_FALLBACK=llm explicitly lets the LLM take over.
    agent_brain: str = field(default_factory=lambda: _lower("AGENT_BRAIN") or default("AGENT_BRAIN"))
    agent_brain_fallback: str = field(default_factory=lambda: _lower("AGENT_BRAIN_FALLBACK") or default("AGENT_BRAIN_FALLBACK"))

    # ── LLM (the llm brain, or the opt-in fallback) ────────────
    llm_base_url: str = _f("LLM_BASE_URL")
    llm_api_key: str = _f("LLM_API_KEY")
    llm_model: str = _f("LLM_MODEL")

    # ── Jev decision model (UM-99, ADR 0001) ──────────────────
    # Direct provider (#164): JEV_BASE_URL, JEV_API_KEY and JEV_MODEL are all
    # explicit; there is no default endpoint and no OPENROUTER_API_KEY fallback.
    # Jev is the think step's brain (UM-101, agent/brain.py) only when
    # AGENT_BRAIN=jev and JEV_BASE_URL is set (tools/jev-mock needs no key);
    # a key without a base URL is off.
    # JEV_PROVIDER=openrouter opts back in to OpenRouter's default base URL and
    # its OPENROUTER_API_KEY. `or`, not a default: compose passes unset vars as "".
    jev_provider: str = field(default_factory=lambda: _lower("JEV_PROVIDER"))
    jev_base_url: str = field(default_factory=lambda: get("JEV_BASE_URL") or (
        OPENROUTER_BASE_URL if _lower("JEV_PROVIDER") == "openrouter" else ""))
    jev_api_key: str = field(repr=False, default_factory=lambda: get("JEV_API_KEY") or (
        get("OPENROUTER_API_KEY") if _lower("JEV_PROVIDER") == "openrouter" else ""))
    # Endpoint path appended to jev_base_url (GH-195): "/decisions" for the
    # OpenRouter proxy, "/v1/systemone" for native TypeSafe. `or`, not a
    # default: an unset or empty JEV_PATH falls back to "/decisions".
    jev_path: str = field(default_factory=lambda: get("JEV_PATH") or default("JEV_PATH"))
    jev_model: str = field(default_factory=lambda: get("JEV_MODEL") or default("JEV_MODEL"))
    # Confidence policy (GH-165, agent/brain.py): below this Jev's choice is
    # replaced by the safe candidate (idle). 0.0 = rule off until measured.
    jev_min_confidence: float = _f("JEV_MIN_CONFIDENCE")
    # Spend cap (#213, agent/brain.py): input+output tokens this agent may spend
    # on Jev in any rolling 60 minutes; 0 = unlimited. Per agent, not fleet-wide.
    max_tokens_per_hour: int = _f("AGENT_MAX_TOKENS_PER_HOUR")
    jev_enabled: bool = field(default_factory=lambda: bool(
        get("JEV_BASE_URL") or (
            _lower("JEV_PROVIDER") == "openrouter"
            and (get("JEV_API_KEY") or get("OPENROUTER_API_KEY")))))
    # agent/tools/probe.py, run from a shell; not passed to containers.
    chat_feed_token: str = _f("CHAT_FEED_TOKEN")

    # ── Logging ───────────────────────────────────────────────
    log_level: str = _f("LOG_LEVEL")
    verbose_packets: bool = _f("VERBOSE_PACKETS")
    # Directory to dump raw (decrypted, inflated) SMSG_UPDATE_OBJECT payloads into.
    dump_packets_dir: str = _f("AGENT_DUMP_PACKETS")

    # ── Chat relay (UM-47) ────────────────────────────────────
    # Where to mirror heard chat (tools/chat-feed, port 9500). Empty = off;
    # either the base URL or the full /api/chat/ingest URL works.
    chat_relay_url: str = _f("AGENT_CHAT_RELAY_URL")
    chat_relay_token: str = _f("AGENT_CHAT_RELAY_TOKEN")
    chat_relay_timeout: float = _f("AGENT_CHAT_RELAY_TIMEOUT_S")

    # ── Audit log (UM-51) ─────────────────────────────────────
    audit_dir: str = _f("AGENT_AUDIT_DIR")
    audit_retention_days: int = _f("AGENT_AUDIT_RETENTION_DAYS")

    # ── Observability API (UM-50) ─────────────────────────────
    # Read-only HTTP view of this agent (agent/http_api.py). 0 = off (default).
    # Bind defaults to loopback; containers set 0.0.0.0 so the port can be
    # published (see docker-compose.agents.yml).
    http_port: int = _f("AGENT_HTTP_PORT")
    http_bind: str = _f("AGENT_HTTP_BIND")
    # #178: bearer token for the one write endpoint, POST /control/walk (agent/control.py).
    # Empty (default) = the API stays strictly read-only. Never logged or returned.
    control_token: str = _f("AGENT_CONTROL_TOKEN")

    def validate(self, require_character: bool = True) -> list[str]:
        """Return a list of configuration problems (empty = OK)."""
        problems = []
        if not self.account:
            problems.append("WOW_ACCOUNT is required")
        if not self.password:
            problems.append("WOW_PASSWORD is required")
        if require_character and not self.character and not self.char_guid:
            problems.append("either WOW_CHARACTER (name) or WOW_CHAR_GUID is required")
        if self.wow_auth_port <= 0 or self.wow_auth_port > 65535:
            problems.append(f"WOW_AUTH_PORT out of range: {self.wow_auth_port}")
        if self.http_port < 0 or self.http_port > 65535:
            problems.append(f"AGENT_HTTP_PORT out of range: {self.http_port}")
        if self.agent_brain not in ("llm", "jev"):
            problems.append(f"AGENT_BRAIN must be llm or jev, got {self.agent_brain!r}")
        if self.agent_brain_fallback not in ("none", "llm"):
            problems.append(f"AGENT_BRAIN_FALLBACK must be none or llm, got {self.agent_brain_fallback!r}")
        return problems

    def redacted(self) -> dict:
        """Config for logging — password and API key masked."""
        return {
            "wow_host": self.wow_host,
            "wow_auth_port": self.wow_auth_port,
            "account": self.account,
            "password": "***" if self.password else "(unset)",
            "character": self.character or f"guid:{self.char_guid}",
            "agent_name": self.agent_name,
            "think_interval": self.think_interval,
            "run_duration": self.run_duration or "forever",
            "brain": self.agent_brain,
            "brain_fallback": self.agent_brain_fallback,
            "llm_model": self.llm_model or "(unset)",
            "jev": "on" if self.jev_enabled else "(off)",
            "jev_base_url": self.jev_base_url,
            "jev_path": self.jev_path,
            "jev_model": self.jev_model,
            "jev_min_confidence": self.jev_min_confidence,
            "jev_api_key": "***" if self.jev_api_key else "(unset)",
            "log_level": self.log_level,
            "dump_packets": self.dump_packets_dir or "(off)",
            "audit_dir": self.audit_dir,
            "audit_retention_days": self.audit_retention_days,
            "chat_relay_url": self.chat_relay_url or "(off)",
            # The token itself is never logged (CLAUDE.md: no credentials in logs).
            "chat_relay_token": "***" if self.chat_relay_token else "(unset)",
            "http": f"{self.http_bind}:{self.http_port}" if self.http_port else "(off)",
        }


def load_config() -> Config:
    cfg = Config()
    return cfg