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
    AGENT_MAX_TOKENS_PER_HOUR: ${AGENT_MAX_TOKENS_PER_HOUR:-0}
    # UM-50 read-only observability API (agent/http_api.py). Each service
    # sets its own AGENT_HTTP_PORT and publishes it (9601-9625). Bound to
    # all interfaces inside the container so the published port works; the
    # data (position, perception, chat heard, decisions) is visible to the
    # whole LAN, but no secrets and no write endpoints. Set
    # AGENT_HTTP_PUBLISH_IP=127.0.0.1 in .env to keep it host-local.
    AGENT_HTTP_BIND: 0.0.0.0
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


def load_roster(path: Path = ROSTER_PATH) -> list[dict]:
    return json.loads(path.read_text())["agents"]


def render(agents: list[dict]) -> str:
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
    return HEADER + "\n".join(blocks)


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
