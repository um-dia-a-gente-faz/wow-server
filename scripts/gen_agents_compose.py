#!/usr/bin/env python3
"""Render docker-compose.agents.yml from agents/roster.json (UM-63).

    python3 scripts/gen_agents_compose.py            # rewrite docker-compose.agents.yml
    python3 scripts/gen_agents_compose.py --check    # exit 1 if the file is out of date

The roster is the source of truth; never edit the generated services by hand.
Edit HEADER/service template here instead, then regenerate. A test
(agent/tests/test_roster.py) fails when the committed compose file drifts.

Layout of the output:
  * one service per roster entry, named `agent-<lowercase character>`; the first
    PARTY_SIZE agents are in profiles [party, raid], the rest in [raid];
  * agent N publishes its read-only API on 9600 + N (9601..9625);
  * every container gets mem_limit/cpus from the x-agent defaults.

`docker compose -f docker-compose.agents.yml up -d agent-luaprata` still works
with profiles: naming a service explicitly enables its profiles.
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROSTER_PATH = REPO / "agents" / "roster.json"
COMPOSE_PATH = REPO / "docker-compose.agents.yml"
FIRST_PORT = 9601
PARTY_SIZE = 5
DEFAULT_AUDIT_DIR = "/opt/wow-server-metrics/audit"

HEADER = """\
# Agent containers, one per character: 5 for a party, 25 for a raid (UM-63).
# GENERATED from agents/roster.json by scripts/gen_agents_compose.py. Do not
# edit the services by hand; edit the roster or the generator and regenerate.
# Each has its own account and character, all sharing ${AGENT_PASSWORD}.
# Communication is in-game only (chat, party).
#
# Compose profiles:
#   party  the first 5 agents      docker compose -f docker-compose.agents.yml --profile party up -d
#   raid   all 25 agents           docker compose -f docker-compose.agents.yml --profile raid up -d
# Naming a service starts it whatever the profile:
#   docker compose -f docker-compose.agents.yml up -d agent-luaprata
# Profile jev-mock adds a local stand-in for Jev (see the jev-mock service).

x-agent: &agent-defaults
  build: .
  image: wow-agent:latest
  restart: unless-stopped
  # Per-container limits so 25 agents fit on a small VM. An agent is a stdlib
  # Python process that mostly waits on the network and the LLM.
  mem_limit: 128m
  cpus: 0.25
  environment: &env-defaults
    WOW_HOST: 192.168.1.64
    WOW_AUTH_PORT: 3724
    WOW_PASSWORD: ${AGENT_PASSWORD:?set AGENT_PASSWORD in .env (see .env.example)}
    AGENT_THINK_INTERVAL_S: 5
    LOG_LEVEL: INFO
    # UM-44 think loop — optional (agent/__main__.py idles gracefully without
    # them, per its own "LLM_BASE_URL/LLM_MODEL not set" warning), but the
    # compose file must still pass them through or every agent silently
    # idles even with a real key sitting in .env. Found live 2026-09-17: the
    # key was set and working, but never reached any container because this
    # block never referenced it.
    LLM_PROVIDER: ${LLM_PROVIDER:-openai-compatible}
    LLM_BASE_URL: ${LLM_BASE_URL:-}
    LLM_API_KEY: ${LLM_API_KEY:-}
    LLM_MODEL: ${LLM_MODEL:-}  # one id, or a comma-separated fallback list (UM-94, see .env.example)
    # UM-101: Jev, the primary brain when set (agent/brain.py); empty = off.
    # Same trap as above: unless passed through here, a key in .env never
    # reaches the containers.
    AGENT_BRAIN: ${AGENT_BRAIN:-llm}  # llm | jev (#161)
    AGENT_BRAIN_FALLBACK: ${AGENT_BRAIN_FALLBACK:-none}  # none | llm: only with AGENT_BRAIN=jev
    JEV_BASE_URL: ${JEV_BASE_URL:-}
    JEV_API_KEY: ${JEV_API_KEY:-}
    JEV_MODEL: ${JEV_MODEL:-}
    AGENT_MAX_TOKENS_PER_HOUR: ${AGENT_MAX_TOKENS_PER_HOUR:-0}
    # UM-50 read-only observability API (agent/http_api.py). Each service
    # sets its own AGENT_HTTP_PORT and publishes it (9601-9625). Bound to
    # all interfaces inside the container so the published port works; the
    # data (position, perception, chat heard, decisions) is visible to the
    # whole LAN, but no secrets and no write endpoints. Set
    # AGENT_HTTP_PUBLISH_IP=127.0.0.1 in .env to keep it host-local.
    AGENT_HTTP_BIND: 0.0.0.0
    # #178: the one write endpoint (POST /control/walk, agent/control.py), used by
    # tools/agent-runner for "walk to" from the console. It reuses the runner's own
    # token, so nothing new goes in .env; empty = the API stays read-only.
    AGENT_CONTROL_TOKEN: ${AGENT_RUNNER_TOKEN:-}
  # Decision audit log (UM-51) — one JSONL file per agent per day. Host path
  # matches monitoring/docker-compose.yml's node-exporter textfile mount
  # input (see monitoring/agent_metrics_textfile.py), so the same directory
  # feeds both the replay viewer and the Prometheus exporter.
  volumes: &audit-volume
    - /opt/wow-server-metrics/audit:/data/audit

services:
"""

SERVICE = """\
  agent-{slug}:
    <<: *agent-defaults
    container_name: wow-agent-{slug}
    profiles: [{profiles}]
    environment:
      <<: *env-defaults
      WOW_ACCOUNT: {account}
      WOW_CHARACTER: {character}
      AGENT_NAME: {character}
      AGENT_HTTP_PORT: {port}
    ports:
      - "${{AGENT_HTTP_PUBLISH_IP:-0.0.0.0}}:{port}:{port}"
    labels:
      wow.agent.name: {character}
      wow.agent.http-port: "{port}"
"""


# UM-100: tools/jev-mock next to the agents, for dev runs without an OpenRouter
# key. Opt-in on both sides (its own profile, and JEV_BASE_URL stays empty by
# default): the mock answers at random, so it must never become a brain by
# accident on the live realm.
JEV_MOCK = """\
  # Random-answer stand-in for Jev's Decisions API (tools/jev-mock). Dev only:
  #   JEV_BASE_URL=http://jev-mock:8090/api/alpha \\
  #     docker compose -f docker-compose.agents.yml --profile jev-mock up -d jev-mock agent-luaprata
  jev-mock:
    image: python:3.12-slim
    container_name: wow-jev-mock
    profiles: [jev-mock]
    restart: unless-stopped
    mem_limit: 64m
    cpus: 0.25
    environment:
      HOST: 0.0.0.0
      PORT: 8090
      PYTHONUNBUFFERED: "1"
      JEV_MOCK_SEED: ${JEV_MOCK_SEED:-}
    volumes:
      - ./tools/jev-mock/server.py:/app/server.py:ro
    command: ["python3", "/app/server.py"]
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2)"]
      interval: 10s
      timeout: 3s
      retries: 3
"""


def load_roster(path: Path = ROSTER_PATH) -> list[dict]:
    return json.loads(path.read_text())["agents"]


def render(agents: list[dict], audit_dir: str = DEFAULT_AUDIT_DIR) -> str:
    """The compose file text. `audit_dir` is the host path mounted at /data/audit;
    tools/agent-runner passes its own so the checkout's committed file stays the
    default (#136, ADR 0002 D5)."""
    seen = set()
    blocks = []
    for i, a in enumerate(agents):
        slug = a["character"].lower()
        if slug in seen or not slug.isalnum():
            raise ValueError(f"bad or duplicate character name in roster: {a['character']!r}")
        seen.add(slug)
        blocks.append(SERVICE.format(
            slug=slug, account=a["account"], character=a["character"], port=FIRST_PORT + i,
            profiles="party, raid" if i < PARTY_SIZE else "raid"))
    text = HEADER + "\n".join(blocks + [JEV_MOCK])
    return text.replace(f"- {DEFAULT_AUDIT_DIR}:/data/audit", f"- {audit_dir}:/data/audit")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--check", action="store_true", help="exit 1 if docker-compose.agents.yml is stale")
    args = p.parse_args(argv)
    out = render(load_roster())
    if args.check:
        if COMPOSE_PATH.read_text() != out:
            print("docker-compose.agents.yml is out of date; run scripts/gen_agents_compose.py", file=sys.stderr)
            return 1
        return 0
    COMPOSE_PATH.write_text(out)
    print(f"wrote {COMPOSE_PATH.relative_to(REPO)} ({len(load_roster())} services)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
