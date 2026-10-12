#!/usr/bin/env python3
"""agent-runner: fleet status and start/stop for the agent containers (#136, ADR 0002).

One small stdlib-only service on the agent host (`wow-agents`). The console
asks it "what is the fleet doing" and "start/stop this agent", so `wowmap`
never needs the Docker socket.

    GET  /healthz                 {"ok": true}                       (no token)
    GET  /agents                  {"agents": [...], "image": {...}}  (token)
    GET  /agents/<name>           one entry                          (token)
    POST /agents/<name>/start     compose up -d [--build] <service>  (token)
    POST /agents/<name>/stop      compose stop <service>             (token)
    POST /agents                  create a level-1 character (#138)  (token)
    POST /agents/<name>/retire    stop + mark retired, never delete  (token)
    POST /agents/<name>/walk      walk to {x,y[,z]} or {near_player} (#178)  (token)
                                  [?dry_run=1 resolves and plans without moving]
    GET  /characters?account=X    what the realm has for an account  (token)

Each agent entry keeps its signals apart, never one boolean:
  container   running | exited | absent | unknown, + status, exit_code, since
  agent_api   ok | unreachable, from the agent's own /healthz (+ position)
  audit       newest record in <audit_dir>/<agent>/<day>.jsonl and its age
  brain       the last cycle's model, valid, last_error
  character   level, playtime_seconds, zone, and which source said so

`unknown` / null means "could not find out", never "fine".

Environment:
    AGENT_RUNNER_TOKEN        shared token, required (Authorization: Bearer ...)
    AGENT_RUNNER_TOKEN_LABEL  name logged for the token (default "shared")
    AGENT_RUNNER_BIND         bind address (default 127.0.0.1; set the LAN IP)
    AGENT_RUNNER_PORT         listen port (default 9700)
    AGENT_RUNNER_REPO         checkout holding agents/roster.json, the
                              Dockerfile and .env (default: this file's repo)
    AGENT_RUNNER_RUNTIME_DIR  runtime state, outside the checkout
                              (default /opt/wow-agent-runtime)
    AGENT_RUNNER_AUDIT_DIR    agents' audit dir (default <runtime>/audit)
    AGENT_RUNNER_PROJECT      compose project name (default wow-agents)
    AGENT_RUNNER_CHARACTER_URL  wowmap, for level/zone/playtime
                              (default http://192.168.1.64:9400)
    AGENT_RUNNER_AGENT_HOST   where the agents' published APIs answer
                              (default 127.0.0.1)
    AGENT_RUNNER_REALM_HOST   realm for character creation (default 192.168.1.64)
    AGENT_RUNNER_REALM_PORT   its auth port (default 3724)
    AGENT_PASSWORD            shared account password; used to log in and to
                              create accounts, never returned, logged or stored

Walk (#178) forwards the command to the agent's own session, which walks with its own
movement code (agent/control.py): never a teleport, a GM command or a database write.
The runner checks the container is running, passes its token to the agent as
AGENT_CONTROL_TOKEN (compose maps it from AGENT_RUNNER_TOKEN), and logs one line per
command with caller IP, operator, agent, target, outcome and start/end positions.

Never touches the worldserver, a GM command or the realm database. Output from
docker is scrubbed of the token and of every *PASSWORD/*KEY/*TOKEN/*SECRET
value in the environment before it is logged or returned.
"""

import logging
import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

# Wiring only (#279). The names in __all__ stay importable from here: tests and the launch
# command use runner.Config, runner.make_server, runner.scrub and friends.
from core import RunnerError, scrub
from docker_backend import DockerBackend
from fleet import SUPPORTED_API_VERSIONS, Fleet
from handler import Handler

__all__ =["Config", "DockerBackend", "Fleet", "Handler", "RunnerError", "SUPPORTED_API_VERSIONS",
           "main", "make_server", "scrub"]

LOG = logging.getLogger("agent-runner")

REPO_DEFAULT = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 9700
DEFAULT_RUNTIME_DIR = "/opt/wow-agent-runtime"
DEFAULT_PROJECT = "wow-agents"
DEFAULT_CHARACTER_URL = "http://192.168.1.64:9400"


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.token = env.get("AGENT_RUNNER_TOKEN", "")
        self.token_label = env.get("AGENT_RUNNER_TOKEN_LABEL", "shared")
        self.bind = env.get("AGENT_RUNNER_BIND", "127.0.0.1")
        self.port = int(env.get("AGENT_RUNNER_PORT", DEFAULT_PORT))
        self.repo = Path(env.get("AGENT_RUNNER_REPO", REPO_DEFAULT))
        self.runtime_dir = Path(env.get("AGENT_RUNNER_RUNTIME_DIR", DEFAULT_RUNTIME_DIR))
        self.audit_dir = Path(env.get("AGENT_RUNNER_AUDIT_DIR", str(self.runtime_dir / "audit")))
        self.project = env.get("AGENT_RUNNER_PROJECT", DEFAULT_PROJECT)
        self.character_url = env.get("AGENT_RUNNER_CHARACTER_URL", DEFAULT_CHARACTER_URL).rstrip("/")
        self.agent_host = env.get("AGENT_RUNNER_AGENT_HOST", "127.0.0.1")
        self.realm_host = env.get("AGENT_RUNNER_REALM_HOST", "192.168.1.64")
        self.realm_port = int(env.get("AGENT_RUNNER_REALM_PORT", 3724))

    @property
    def compose_path(self):
        return self.runtime_dir / "docker-compose.agents.yml"


def make_server(config, fleet=None):
    handler = type("BoundHandler", (Handler,), {"fleet": fleet or Fleet(config), "config": config})
    return ThreadingHTTPServer((config.bind, config.port), handler)


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = Config()
    if not config.token:
        sys.exit("AGENT_RUNNER_TOKEN is not set; refusing to start without auth")
    if config.bind in ("0.0.0.0", "::"):
        LOG.warning("bound to every interface; bind the LAN address with AGENT_RUNNER_BIND")
    server = make_server(config)
    LOG.info("agent-runner on %s:%d (repo %s, runtime %s)", config.bind, config.port,
             config.repo, config.runtime_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
