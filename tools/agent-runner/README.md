# agent-runner

Control plane for the agent fleet (#136; topology decisions in ADR 0002, #134). One
stdlib-only service on the agent host (`wow-agents`) that reports what every
agent container is doing and starts/stops them, so `wowmap` never needs the
Docker socket. Python 3.12, no pip packages.

## Run

Recommended: a host systemd unit, not a container, so the Docker socket never
enters a process that also serves HTTP, and `git fetch` auto-updates reach it.

```ini
# /etc/systemd/system/agent-runner.service
[Service]
EnvironmentFile=/opt/wow-server/.env
Environment=AGENT_RUNNER_BIND=<LAN IP of wow-agents>
ExecStart=/usr/bin/python3 /opt/wow-server/tools/agent-runner/runner.py
Restart=on-failure
```

`.env` needs `AGENT_RUNNER_TOKEN` (and `AGENT_PASSWORD` for the agents). The
runner refuses to start without a token. Locally:
`AGENT_RUNNER_TOKEN=x python3 tools/agent-runner/runner.py`.

| Variable | Default | |
|---|---|---|
| `AGENT_RUNNER_TOKEN` | required | shared token; `Authorization: Bearer <token>` |
| `AGENT_RUNNER_TOKEN_LABEL` | `shared` | name written to the action log |
| `AGENT_RUNNER_BIND` | `127.0.0.1` | set to the LAN address; `0.0.0.0` logs a warning |
| `AGENT_RUNNER_PORT` | `9700` | |
| `AGENT_RUNNER_REPO` | this checkout | holds `agents/roster.json`, `Dockerfile`, `.env` |
| `AGENT_RUNNER_RUNTIME_DIR` | `/opt/wow-agent-runtime` | runtime state, outside the checkout |
| `AGENT_RUNNER_AUDIT_DIR` | `<runtime>/audit` | where the agents write `<agent>/<day>.jsonl` |
| `AGENT_RUNNER_PROJECT` | `wow-agents` | compose project name |
| `AGENT_RUNNER_CHARACTER_URL` | `http://192.168.1.64:9400` | wowmap, source of level/zone/playtime |
| `AGENT_RUNNER_AGENT_HOST` | `127.0.0.1` | where the agents' published APIs answer |

## API

| | |
|---|---|
| `GET /healthz` | `{"ok": true}`, no token |
| `GET /agents` | every configured agent, plus `image` (present, created, stale) |
| `GET /agents/<name>` | one agent (name is the character, case-insensitive) |
| `POST /agents/<name>/start` | `compose up -d <service>`; adds `--build` when the image is missing or older than the last commit touching `agent/` or `Dockerfile`, or with `?build=1` |
| `POST /agents/<name>/stop` | `compose stop <service>` |

Everything but `/healthz` needs the token (401 without). Writes return the
agent's resulting status. Both are idempotent: starting a running agent or
stopping an absent one succeeds.

An agent entry keeps its signals separate:

```json
{"name": "Spellweaver", "account": "AGENT05", "service": "agent-spellweaver",
 "container": {"state": "exited", "status": "exited", "exit_code": 137, "since": "2026-09-19T08:30:00Z"},
 "agent_api": {"state": "unreachable"},
 "audit": {"last_ts": "2026-10-03T19:07:12Z", "age_seconds": 3600},
 "brain": {"model": "auto", "valid": false, "last_error": "…503 no_providers_configured", "brain": "llm", "cycle": 9},
 "character": {"level": 2, "playtime_seconds": 13088, "zone": "Eversong Woods", "source": "wowmap http://192.168.1.64:9400"}}
```

- `container.state` is `running`, `exited`, `absent` or `unknown`. `unknown`
  means docker could not answer; it is never reported as `absent`.
- `agent_api` is the agent's own `GET /healthz` (plus `/state` for position and
  level): a running container with an unreachable API is the stale-image trap
  of #131.
- `audit` and `brain` come from the newest record in the agent's audit log, so
  they work when the agent's API is down. Audit age is measured at the source
  (this host), which tells "agent silent" from "audit pull broken".
- `character` comes from wowmap on the wow-server VM (the runner is stdlib-only
  and has no MySQL driver); the source is named in the entry, and an error is
  reported as `error`, not as a zero.
- If the runner itself is down, the caller gets no answer at all and must show
  *unknown*.

## What it does to the host

- The runner generates its own compose file at
  `<runtime>/docker-compose.agents.yml` from `agents/roster.json` (through
  `scripts/gen_agents_compose.py`) plus an optional `<runtime>/state.json`
  (`{"agents": [...]}`, same entry shape, replacing or appending by account).
  It runs compose with `--project-directory <checkout>` so `build: .` and `.env`
  resolve, and writes nothing into the checkout.
- Every action is logged as one JSON line: `ts, action, agent, caller, token,
  result`. Refused writes are logged too.
- Docker output is scrubbed of the token and of every `*PASSWORD`, `*KEY`,
  `*TOKEN` and `*SECRET` value in the environment before it is logged or
  returned.
- It never touches the worldserver, never runs a GM command and never writes to
  the realm database.

## Test

```bash
python3 -m unittest discover -s tools/agent-runner/tests
```

No real containers: `docker` is a fake script on `PATH`, and the agents' APIs
and wowmap are local HTTP servers.
