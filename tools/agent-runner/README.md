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
| `AGENT_RUNNER_REALM_HOST` | `192.168.1.64` | realm used to create characters and accounts |
| `AGENT_RUNNER_REALM_PORT` | `3724` | its auth port |
| `AGENT_PASSWORD` | required for characters | shared account password; handed to the realm and the worldserver console only |

## API

| | |
|---|---|
| `GET /healthz` | `{"ok": true}`, no token |
| `GET /agents` | every configured agent, plus `image` (present, created, stale) |
| `GET /agents/<name>` | one agent (name is the character, case-insensitive) |
| `POST /agents/<name>/start` | `compose up -d <service>`; adds `--build` when the image is missing or older than the last commit touching `agent/` or `Dockerfile`, or with `?build=1` |
| `POST /agents/<name>/stop` | `compose stop <service>` |
| `POST /agents` | create a level-1 character, record it, start its container (below) |
| `POST /agents/<name>/retire` | stop the container and mark the agent retired; the character is never deleted |
| `GET /characters?account=AGENT07` | what the realm has for that account: `exists`, `characters`, `free_slot` |

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

## Creating and retiring characters (#138, ADR 0002 D6)

`POST /agents` with `{"account": "AGENT07", "race": 10, "class": 8}` plus optional
`name` and `gender` (0/1):

1. Validates before touching the realm: account `AGENT01..AGENT25`, a Horde race with a
   class it can play (the table in `scripts/create_agent_roster.py`), no death knight
   (they need an existing level-55 character), name 2-12 letters with no letter three
   times in a row. A name already used by an agent in the roster is refused locally;
   the realm answers for every other name.
2. Logs in as the account; if it does not exist, creates it through the worldserver
   console (the shared password is used there and nowhere else) and waits for it.
3. Refuses with 409 if the account already has a character (one per account).
4. Sends `CMSG_CHAR_CREATE` (`WoWSession.create_character`), then lists the account again
   and returns the character as the realm reports it (`level` 1).
   A name in use comes back as 409 `CHAR_CREATE_NAME_IN_USE` with `code` 50, a rejected
   name as 400, any other refusal as 502 with the server's `code`. There is no retry:
   the caller picks another name.
5. Records the agent in `<runtime>/state.json`, regenerates the runner's compose file
   (`AGENT_NAME` is the character name, so audit files land under that name) and starts the
   container. Answers 201 with the new agent's status entry and a `character` block.

If the character exists but the container did not start, the answer is 502 with
`partial`: `character_created`, `recorded`, `container_started` and the account and
name. The character is recorded, so `POST /agents/<name>/start` finishes the job.

`POST /agents/<name>/retire` stops the container and sets `retired: true` in the state
file. It is idempotent and fine when the container is already gone. A retired agent
stays in the list (so the other agents' ports do not shift) and `start` on it is a 409.

Nothing here deletes a character, writes `agents/roster.json` or touches the checkout.
Not built: a ready-to-paste roster snippet for a runtime-created agent. The state file is
the record; adding agents to the committed plan stays a normal PR.

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
  returned. The shared account password is never returned, logged or written to the
  state file or the compose file.
- It never restarts the worldserver, never runs a GM command on a character and never
  writes to the realm database. The one console command it can send is `account create`
  for a missing `AGENTnn` account; characters are created through the game protocol.

## Test

```bash
python3 -m unittest discover -s tools/agent-runner/tests
```

No real containers: `docker` is a fake script on `PATH`, and the agents' APIs
and wowmap are local HTTP servers.
