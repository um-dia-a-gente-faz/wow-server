#!/usr/bin/env python3
"""Agent configuration — everything comes from environment variables.

One container = one agent. No shared state between agents; they only
interact through the game server (chat, party, trade).
"""

import os
from dataclasses import dataclass, field

# Only used when JEV_PROVIDER=openrouter; see Config.jev_base_url.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/alpha"


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class Config:
    # ── Server ────────────────────────────────────────────────
    wow_host: str = field(default_factory=lambda: _env_str("WOW_HOST", "192.168.1.64"))
    wow_auth_port: int = field(default_factory=lambda: _env_int("WOW_AUTH_PORT", 3724))
    wow_build: int = field(default_factory=lambda: _env_int("WOW_BUILD", 12340))

    # ── Account ───────────────────────────────────────────────
    account: str = field(default_factory=lambda: _env_str("WOW_ACCOUNT"))
    password: str = field(default_factory=lambda: _env_str("WOW_PASSWORD"))

    # ── Character ─────────────────────────────────────────────
    # Either a name (preferred, human-readable) or a numeric GUID.
    character: str = field(default_factory=lambda: _env_str("WOW_CHARACTER"))
    char_guid: int = field(default_factory=lambda: _env_int("WOW_CHAR_GUID", 0))

    # ── Agent identity / behaviour ────────────────────────────
    agent_name: str = field(default_factory=lambda: _env_str("AGENT_NAME", "Agent"))
    think_interval: float = field(default_factory=lambda: _env_float("AGENT_THINK_INTERVAL_S", 3.0))
    run_duration: float = field(default_factory=lambda: _env_float("AGENT_RUN_DURATION_S", 0.0))  # 0 = forever
    persona: str = field(default_factory=lambda: _env_str("AGENT_PERSONA", ""))
    # UM-93: chat channels to join after login, comma-separated ("General,world");
    # "none" disables. See agent/channels.py::parse_channel_spec.
    channels: str = field(default_factory=lambda: _env_str("AGENT_CHANNELS", "General"))

    # ── Brain selection (#161, ADR 0001) ──────────────────────
    # One brain per agent: "llm" (default) or "jev". A Jev failure is a failed
    # cycle unless AGENT_BRAIN_FALLBACK=llm explicitly lets the LLM take over.
    agent_brain: str = field(default_factory=lambda: _env_str("AGENT_BRAIN", "llm").lower() or "llm")
    agent_brain_fallback: str = field(default_factory=lambda: _env_str(
        "AGENT_BRAIN_FALLBACK", "none").lower() or "none")

    # ── LLM (the llm brain, or the opt-in fallback) ────────────
    llm_base_url: str = field(default_factory=lambda: _env_str("LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: _env_str("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env_str("LLM_MODEL", ""))

    # ── Jev decision model (UM-99, ADR 0001) ──────────────────
    # Direct provider (#164): JEV_BASE_URL, JEV_API_KEY and JEV_MODEL are all
    # explicit; there is no default endpoint and no OPENROUTER_API_KEY fallback.
    # Jev is the think step's brain (UM-101, agent/brain.py) only when
    # AGENT_BRAIN=jev and JEV_BASE_URL is set (tools/jev-mock needs no key);
    # a key without a base URL is off.
    # JEV_PROVIDER=openrouter opts back in to OpenRouter's default base URL and
    # its OPENROUTER_API_KEY. `or`, not a default: compose passes unset vars as "".
    jev_provider: str = field(default_factory=lambda: _env_str("JEV_PROVIDER").lower())
    jev_base_url: str = field(default_factory=lambda: _env_str("JEV_BASE_URL") or (
        OPENROUTER_BASE_URL if _env_str("JEV_PROVIDER").lower() == "openrouter" else ""))
    jev_api_key: str = field(default_factory=lambda: _env_str("JEV_API_KEY") or (
        _env_str("OPENROUTER_API_KEY") if _env_str("JEV_PROVIDER").lower() == "openrouter" else ""))
    # Endpoint path appended to jev_base_url (GH-195): "/decisions" for the
    # OpenRouter proxy, "/v1/systemone" for native TypeSafe. `or`, not a
    # default: an unset or empty JEV_PATH falls back to "/decisions".
    jev_path: str = field(default_factory=lambda: _env_str("JEV_PATH") or "/decisions")
    jev_model: str = field(default_factory=lambda: _env_str("JEV_MODEL") or "typesafe/jev-1.13")
    # Confidence policy (GH-165, agent/brain.py): below this Jev's choice is
    # replaced by the safe candidate (idle). 0.0 = rule off until measured.
    jev_min_confidence: float = field(default_factory=lambda: _env_float("JEV_MIN_CONFIDENCE", 0.0))
    # Spend cap (#213, agent/brain.py): input+output tokens this agent may spend
    # on Jev in any rolling 60 minutes; 0 = unlimited. Per agent, not fleet-wide.
    max_tokens_per_hour: int = field(default_factory=lambda: _env_int("AGENT_MAX_TOKENS_PER_HOUR", 0))
    jev_enabled: bool = field(default_factory=lambda: bool(
        _env_str("JEV_BASE_URL") or (
            _env_str("JEV_PROVIDER").lower() == "openrouter"
            and (_env_str("JEV_API_KEY") or _env_str("OPENROUTER_API_KEY")))))

    # ── Logging ───────────────────────────────────────────────
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO"))
    verbose_packets: bool = field(default_factory=lambda: _env_bool("VERBOSE_PACKETS", False))
    # Directory to dump raw (decrypted, inflated) SMSG_UPDATE_OBJECT payloads into.
    dump_packets_dir: str = field(default_factory=lambda: _env_str("AGENT_DUMP_PACKETS"))

    # ── Chat relay (UM-47) ────────────────────────────────────
    # Where to mirror heard chat (tools/chat-feed, port 9500). Empty = off;
    # either the base URL or the full /api/chat/ingest URL works.
    chat_relay_url: str = field(default_factory=lambda: _env_str("AGENT_CHAT_RELAY_URL"))
    chat_relay_token: str = field(default_factory=lambda: _env_str("AGENT_CHAT_RELAY_TOKEN"))
    chat_relay_timeout: float = field(default_factory=lambda: _env_float("AGENT_CHAT_RELAY_TIMEOUT_S", 3.0))

    # ── Audit log (UM-51) ─────────────────────────────────────
    audit_dir: str = field(default_factory=lambda: _env_str("AGENT_AUDIT_DIR", "/data/audit"))
    audit_retention_days: int = field(default_factory=lambda: _env_int("AGENT_AUDIT_RETENTION_DAYS", 14))

    # ── Observability API (UM-50) ─────────────────────────────
    # Read-only HTTP view of this agent (agent/http_api.py). 0 = off (default).
    # Bind defaults to loopback; containers set 0.0.0.0 so the port can be
    # published (see docker-compose.agents.yml).
    http_port: int = field(default_factory=lambda: _env_int("AGENT_HTTP_PORT", 0))
    http_bind: str = field(default_factory=lambda: _env_str("AGENT_HTTP_BIND", "127.0.0.1"))
    # #178: bearer token for the one write endpoint, POST /control/walk (agent/control.py).
    # Empty (default) = the API stays strictly read-only. Never logged or returned.
    control_token: str = field(default_factory=lambda: _env_str("AGENT_CONTROL_TOKEN"))

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