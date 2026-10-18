"""The text of docker-compose.agents.yml, rendered from roster entries (#391).

Pure and stdlib-only: nothing here reads or writes a file. scripts/gen_agents_compose.py is the
command line that writes the committed file; tools/agent-runner renders its own copy with its
own audit directory. Edit HEADER or SERVICE here, then run scripts/gen_agents_compose.py.
"""
from . import roster

DEFAULT_AUDIT_DIR = "/opt/wow-server-metrics/audit"
DEFAULT_FLEET_DIR = "/opt/wow-server-metrics/fleet"
DEFAULT_WORLD_DIR = "/opt/wow-server-metrics/world"
DEFAULT_STATE_DIR = "/opt/wow-server-metrics/agent-state"
RAID_SIZE = 25  # the size the HEADER text describes; the port range it names ends at FIRST_PORT + 24

# #539: agent i (0-based position in the list given to render) waits (i * START_DELAY_STEP_S) %
# START_DELAY_PERIOD_S before its first connection, so a fleet restart does not hit the auth
# server at once. Modulo, not a cap: a cap would give every position past the cap the same delay.
START_DELAY_STEP_S = 3
START_DELAY_PERIOD_S = 60

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
  image: wow-agent:${WOW_AGENT_TAG:?set WOW_AGENT_TAG in .env (a git SHA; see docs/DEPLOYMENT.md)}
  restart: unless-stopped
  # Per-container limits so 25 agents fit on a small VM. An agent is a stdlib
  # Python process that mostly waits on the network and the LLM.
  mem_limit: 128m
  cpus: 0.25
  environment: &env-defaults
    WOW_HOST: 192.168.1.64
    WOW_AUTH_PORT: 3724
    # #816: the pathfinder sidecar (tools/pathfinder) on the wow-server VM, next to wowmap. LAN only, like WOW_HOST.
    AGENT_PATHFINDER_URL: http://192.168.1.64:8770
    WOW_PASSWORD: ${AGENT_PASSWORD:?set AGENT_PASSWORD in .env (see .env.example)}
    AGENT_THINK_INTERVAL_S: ${AGENT_THINK_INTERVAL_S:-5}
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
    # UM-101/#191: Jev is opt-in (agent/brain.py): the LLM plays unless AGENT_BRAIN=jev;
    # a configured Jev key with AGENT_BRAIN=llm only logs a warning.
    # Same trap as above: unless passed through here, a key in .env never
    # reaches the containers.
    AGENT_BRAIN: ${AGENT_BRAIN:-llm}  # llm | jev (#161)
    AGENT_BRAIN_FALLBACK: ${AGENT_BRAIN_FALLBACK:-none}  # none | llm: only with AGENT_BRAIN=jev
    # JEV_PROVIDER=openrouter opts in to OpenRouter's base URL and
    # OPENROUTER_API_KEY (#164); the direct provider is the default.
    JEV_PROVIDER: ${JEV_PROVIDER:-}
    JEV_BASE_URL: ${JEV_BASE_URL:-}
    JEV_PATH: ${JEV_PATH:-}
    JEV_API_KEY: ${JEV_API_KEY:-}
    JEV_MODEL: ${JEV_MODEL:-}
    JEV_MIN_CONFIDENCE: ${JEV_MIN_CONFIDENCE:-}  # #165 confidence policy; empty = 0.0 = rule off
    # Read only when JEV_PROVIDER=openrouter (agent/config.py); passed so
    # setting it in .env reaches the containers (#190).
    OPENROUTER_API_KEY: ${OPENROUTER_API_KEY:-}
    AGENT_MAX_TOKENS_PER_HOUR: ${AGENT_MAX_TOKENS_PER_HOUR:-0}
    # #190: documented in .env.example and read by agent/config.py, so they must
    # be passed or setting them in .env silently does nothing. Empty = the
    # code's own default (AGENT_CHANNELS is _env_str, so it needs the default
    # spelled out). WOW_CHAR_GUID is deliberately not passed: every service
    # names its own WOW_CHARACTER (see agent/tests/test_compose_env.py).
    VERBOSE_PACKETS: ${VERBOSE_PACKETS:-}
    AGENT_DUMP_PACKETS: ${AGENT_DUMP_PACKETS:-}
    AGENT_PERSONA: ${AGENT_PERSONA:-}
    AGENT_RUN_DURATION_S: ${AGENT_RUN_DURATION_S:-}
    AGENT_CHANNELS: ${AGENT_CHANNELS:-General}
    # #747 (ADR 0017): the fleet limits file, e.g. /data/fleet/limits.json. Empty = off: no file is
    # touched. The owner sets it in .env on the agent host; the directory below is always mounted.
    AGENT_FLEET_LIMITS_FILE: ${AGENT_FLEET_LIMITS_FILE:-}
    # #749 (ADR 0016): the world bundle file, e.g. /data/world/world.sqlite3. Empty = off: no file is
    # touched. The owner sets it in .env on the agent host; the directory below is always mounted.
    AGENT_WORLD_BUNDLE: ${AGENT_WORLD_BUNDLE:-}
    # ADR 0020 (#390): 1 = the agent writes its activity events (level-ups) on its audit line; wowmap
    # then shows them as the agent's own. Empty = off (agent/config.py); the default is off.
    AGENT_ACTIVITY_EVENTS: ${AGENT_ACTIVITY_EVENTS:-}
    # UM-50 read-only observability API (agent/http_api.py). Each service
    # sets its own AGENT_HTTP_PORT and publishes it (@API_PORTS@). Bound to
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
    # #747: the DIRECTORY of the fleet limits file, read-only. Never the file: a single-file bind
    # mount pins the inode, and the writer replaces the file by rename (ADR 0017, Consequences).
    - /opt/wow-server-metrics/fleet:/data/fleet:ro
    # #749: the DIRECTORY of the world bundle, read-only. Never the file: tools/worldknowledge/build.py
    # replaces the bundle by rename, and a single-file bind mount would pin the old inode.
    - /opt/wow-server-metrics/world:/data/world:ro
    # #252 (ADR 0021): the DIRECTORY of the per-agent state, read-write; each agent writes only its own
    # subdirectory (names.json). A directory, never a file: the caches are replaced by rename.
    - /opt/wow-server-metrics/agent-state:/data/state

services:
"""

SERVICE = """\
  {service}:
    <<: *agent-defaults
    container_name: {container}
    profiles: [{profiles}]
    environment:
      <<: *env-defaults
      WOW_ACCOUNT: {account}
      WOW_CHARACTER: {character}
      AGENT_NAME: {character}
      # #509: the whole roster's character names, so the follow rule (#318) sees every agent, not only the leader
      AGENT_ROSTER: "{roster}"
      # #762 (#314): this service's own mission from agents/roster.json (default level_to_cap)
      AGENT_MISSION: {mission}
      AGENT_HTTP_PORT: {port}
      AGENT_START_DELAY_S: "{start_delay}"
      # #252: this agent's own cache directory (names.json), under the state volume above
      AGENT_STATE_DIR: /data/state/{slug}
    ports:
      - "${{AGENT_HTTP_PUBLISH_IP:-0.0.0.0}}:{port}:{port}"
    labels:
      wow.agent.name: {character}
      wow.agent.http-port: "{port}"
"""


# UM-100: tools/jev_mock next to the agents, for dev runs without an OpenRouter
# key. Opt-in on both sides (its own profile, and JEV_BASE_URL stays empty by
# default): the mock answers at random, so it must never become a brain by
# accident on the live realm.
JEV_MOCK = """\
  # Random-answer stand-in for Jev's Decisions API (tools/jev_mock). Dev only:
  #   JEV_BASE_URL=http://jev-mock:8090/api/alpha \\
  #     docker compose -f docker-compose.agents.yml --profile jev-mock up -d jev-mock agent-luaprata
  jev-mock:
    image: python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
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
      - ./tools/jev_mock/server.py:/app/server.py:ro
    command: ["python3", "/app/server.py"]
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2)"]
      interval: 10s
      timeout: 3s
      retries: 3
"""


def derive(position: int, entry: dict) -> roster.Agent:
    """The library's Agent for the entry at 0-based `position`. Reads only the known keys, so the
    runner's extra keys (`retired`) pass through unvalidated."""
    return roster.Agent(index=position + 1, account=entry.get("account"), character=entry.get("character"),
                        race=entry.get("race"), class_id=entry.get("class"), gender=entry.get("gender"),
                        mission=entry.get("mission", roster.DEFAULT_MISSION))


def render(agents: list[dict], audit_dir: str = DEFAULT_AUDIT_DIR, fleet_dir: str = DEFAULT_FLEET_DIR,
           world_dir: str = DEFAULT_WORLD_DIR, state_dir: str = DEFAULT_STATE_DIR) -> str:
    """The compose file text. `audit_dir` is the host path mounted at /data/audit;
    tools/agent-runner passes its own so the checkout's committed file stays the
    default (#136, ADR 0002 D5). `fleet_dir` is the host path mounted read-only at /data/fleet (#747);
    `world_dir` the one mounted read-only at /data/world (#749); `state_dir` the one mounted
    read-write at /data/state (#252), each agent's cache directory is a subdirectory of it."""
    seen = set()
    blocks = []
    names = ",".join(derive(i, a).character for i, a in enumerate(agents))
    for i, a in enumerate(agents):
        agent = derive(i, a)
        if agent.slug in seen or not agent.slug.isalnum():
            raise ValueError(f"bad or duplicate character name in roster: {a['character']!r}")
        seen.add(agent.slug)
        blocks.append(SERVICE.format(
            service=agent.service, container=agent.container,
            account=agent.account, character=agent.character, port=agent.api_port, roster=names,
            mission=agent.mission,
            profiles=", ".join(agent.profiles),
            start_delay=(i * START_DELAY_STEP_S) % START_DELAY_PERIOD_S, slug=agent.slug))
    last_port = roster.FIRST_PORT + RAID_SIZE - 1
    header = HEADER.replace("@API_PORTS@", f"{roster.FIRST_PORT}-{last_port}")
    text = header + "\n".join(blocks + [JEV_MOCK])
    return (text.replace(f"- {DEFAULT_AUDIT_DIR}:/data/audit", f"- {audit_dir}:/data/audit")
            .replace(f"- {DEFAULT_FLEET_DIR}:/data/fleet:ro", f"- {fleet_dir}:/data/fleet:ro")
            .replace(f"- {DEFAULT_WORLD_DIR}:/data/world:ro", f"- {world_dir}:/data/world:ro")
            .replace(f"- {DEFAULT_STATE_DIR}:/data/state", f"- {state_dir}:/data/state"))
