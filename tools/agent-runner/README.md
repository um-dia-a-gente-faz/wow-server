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

`.env` needs `AGENT_RUNNER_TOKEN` (and `AGENT_PASSWORD` and `WOW_AGENT_TAG`, the agent image's SHA tag, for the agents). The
runner refuses to start without a token. Locally:
`AGENT_RUNNER_TOKEN=x python3 tools/agent-runner/runner.py`.

The `EnvironmentFile=/opt/wow-server/.env` line is mandatory, not a convenience. The runner reads
`WOW_AGENT_TAG` from its own environment, and that line is the only way the tag gets there. Without it,
the status says `reason: tag_unset` (the fleet page shows the banner), and a plain start runs
`docker compose` with no `--build`. Compose then reads `WOW_AGENT_TAG` from the checkout's `.env`
(the runner passes the checkout as the project directory), and if that image is missing it builds the
checkout under that tag, because the agent service has `build: .`. That is the build the #732 rule
exists to refuse. Reasoned from the code and Compose's documented behaviour, NOT run (no docker on the
review host, #734). The fix is the unit line, not a runner change: keep it, and restart the runner after
any change to the tag.

The runner's image status (`GET /agents`, `image`) follows `WOW_AGENT_TAG`: it reports
`wow-agent:<WOW_AGENT_TAG>`, read from the runner's environment at each request (#730). Unset or not a
valid Docker tag, it reports `present: false` and names the tag as not set; it never falls back to
`latest`. When `present` is false, `reason` says why: `tag_unset` (no valid tag) or `missing` (the tag
is valid and the image is not built). The key is absent when the image is present. The variable comes from the unit's `EnvironmentFile`, so restart the runner after changing
it in `.env` (the environment is read once at start, and compose inherits it too).

**When the runner builds (#131 as changed by #730).** The runner never rebuilds an existing pinned
tag on its own. Without `?build=1`, `start` adds `--build` only when `WOW_AGENT_TAG` gives a valid
image reference and that image is missing. #732: a build (automatic or `?build=1`) runs only when the tag
is the checkout's HEAD, the 12-digit commit `scripts/build-agent-image.sh` tags with (`git rev-parse --short=12 HEAD`).
Otherwise `start` answers HTTP 409 and does not call compose: `error` is `image_not_built` (the image is
missing) or `tag_is_not_head` (`?build=1` on a present image); `detail` names the tag and the checkout's
commit and says to check that commit out and run `scripts/build-agent-image.sh`. A git failure counts as
not HEAD. A tag that is not a commit SHA can therefore never be built through the runner. A stale image (`stale: true`: older than the last commit
touching `agent/` or `Dockerfile`) triggers nothing; it is information only, meaning "build a new image
and move the tag". `?build=1` is the explicit rebuild. With the tag unset or invalid, `start` adds no
`--build`, and compose reports the missing variable (HTTP 502, the scrubbed detail).

A rollback is: set `WOW_AGENT_TAG` back to an earlier SHA, restart the runner, start the agents. If that
image is still on the host, it starts as it is. If it was pruned, it is missing, so `start` builds the
checkout under that old tag: check out that commit and run `scripts/build-agent-image.sh` first.

| Variable | Default | |
|---|---|---|
| `AGENT_RUNNER_TOKEN` | required | shared token; `Authorization: Bearer <token>` |
| `AGENT_RUNNER_TOKEN_LABEL` | `shared` | name written to the action log |
| `AGENT_RUNNER_BIND` | `127.0.0.1` | set to the LAN address; `0.0.0.0` logs a warning |
| `AGENT_RUNNER_PORT` | `9700` | |
| `AGENT_RUNNER_REPO` | this checkout | holds `agents/roster.json`, `Dockerfile`, `.env`; the `agents/` library code always comes from the checkout `runner.py` is in (#391) |
| `AGENT_RUNNER_RUNTIME_DIR` | `/opt/wow-agent-runtime` | runtime state, outside the checkout |
| `AGENT_RUNNER_AUDIT_DIR` | `<runtime>/audit` | where the agents write `<agent>/<day>.jsonl` |
| `AGENT_RUNNER_ACTION_LOG` | `<runtime>/actions.jsonl` | append-only action log (below); empty string turns it off |
| `AGENT_RUNNER_PROJECT` | `wow-agents` | compose project name |
| `AGENT_RUNNER_CHARACTER_URL` | `http://192.168.1.64:9400` | wowmap, source of level/zone/playtime |
| `AGENT_RUNNER_AGENT_HOST` | `127.0.0.1` | where the agents' published APIs answer |
| `AGENT_RUNNER_REALM_HOST` | `192.168.1.64` | realm used to create characters and accounts |
| `AGENT_RUNNER_REALM_PORT` | `3724` | its auth port |
| `AGENT_PASSWORD` | required for characters | shared account password; handed to the realm and the worldserver console only |

## Modules (#279)

| File | What it holds |
|---|---|
| `runner.py` | entry point: `Config`, `make_server`, `main`; re-exports the names the tests and launch use |
| `handler.py` | HTTP routing, token check, one audit line per action (named so it does not shadow the stdlib `http`) |
| `fleet.py` | roster, per-agent signals, start/stop/retire, walk, character create |
| `docker_backend.py` | the only code that runs `docker` or `git`; `FakeDockerBackend` is the test double |
| `core.py` | `RunnerError`, `scrub`, `iso`, shared by the above |
| `characters.py` | the realm side of character create and list (#138) |

The Docker allow-list is `check_names` in `docker_backend.py`: every name handed to docker
must be a roster service or container. `inspect` and `compose` call it before anything runs.

## API

| | |
|---|---|
| `GET /healthz` | `{"ok": true}`, no token |
| `GET /agents` | every configured agent, plus `image` (present, created, stale) |
| `GET /agents/<name>` | one agent (name is the character, case-insensitive) |
| `POST /agents/<name>/start` | `compose up -d <service>`; adds `--build` only when `WOW_AGENT_TAG` names a valid image that is missing, or with `?build=1`, and only when the tag is the checkout's HEAD (else 409, #732; #131, changed by #730: see below) |
| `POST /agents/<name>/stop` | `compose stop <service>` |
| `POST /agents` | create a level-1 character, record it, start its container (below) |
| `POST /agents/<name>/retire` | stop the container and mark the agent retired; the character is never deleted |
| `POST /agents/<name>/walk` | walk the agent's character with its own movement (#178, below) |
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
   class it can play (the table in `agents/character_rules.py`), no death knight
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

The build rule of `start` (see below) is checked after the validation (step 1) and the roster duplicate
check, and before anything is created (#734). When the pinned
image is missing and `WOW_AGENT_TAG` is not the checkout's HEAD, the create answers the same 409 that
`start` would (`image_not_built`, with its `detail`), and nothing changes: no account, no character, no
state entry, no compose file. A present image, or a tag the rule does not refuse, goes on as before. With
`WOW_AGENT_TAG` unset or invalid the create answers 409 `tag_unset` before anything is created (#734, second
Decision; `start` keeps its 502 path). A create asks for no build.

If the character exists but the container did not start, the answer is 502 with
`partial`: `character_created`, `recorded`, `container_started` and the account and
name. The character is recorded, so `POST /agents/<name>/start` finishes the job.

`POST /agents/<name>/retire` stops the container and sets `retired: true` in the state
file. It is idempotent and fine when the container is already gone. A retired agent
stays in the list (so the other agents' ports do not shift) and `start` on it is a 409.

Nothing here deletes a character, writes `agents/roster.json` or touches the checkout.
Not built: a ready-to-paste roster snippet for a runtime-created agent. The state file is
the record; adding agents to the committed plan stays a normal PR.

## Walking an agent (#178)

`POST /agents/<name>/walk` with `{"x": .., "y": .., "z": ..}` (z optional, current map) or
`{"near_player": "Rubens"}`; optional `map` (refused unless it is the character's map),
`stop_distance` (yards) and `timeout_s` (default 60, max 120). `?dry_run=1` resolves the
target and returns the plan (target, distance, method) without moving anything.

The runner checks the container is running, then forwards to the agent's own
`POST /control/walk` (`agent/control.py`, enabled by `AGENT_CONTROL_TOKEN`, which the
generated compose file maps from `AGENT_RUNNER_TOKEN`). The character walks with its own
`move_to` / `move_towards`: never a teleport, a GM command or a database write. The think loop
is held for the duration, so the brain cannot start a competing move.

The answer always has `ok` and `outcome`; only `outcome: "arrived"` (HTTP 200) is success:

| outcome | HTTP | meaning |
|---|---|---|
| `arrived` | 200 | within `stop_distance`; `start`, `end`, `end_distance`, `moved` reported |
| `blocked` | 422 | no progress for 3 s or a server position correction (terrain, a mob, no straight path) |
| `timeout` | 422 | not there after `timeout_s`; the character was stopped |
| `stopped`, `target_lost`, `failed` | 422 | interrupted, the player left perception, anything else |
| `refused` | 400/404/409/502 | nothing moved; `code` says why: `player_not_perceived`, `not_running`, `retired`, `wrong_map`, `too_far`, `dead`, `not_connected`, `busy`, `control_disabled` (agent image older than #178 or token mismatch: start it with a rebuild), `agent_unreachable`, ... |

Movement is a straight line with no pathfinding, and `start`/`end` are the session's own view
of where it last told the server it was, not a database read. Every call is logged with
`caller` (wowmap's address), `operator` (the browser's, from `X-Operator-Address`), `agent`,
`target`, `outcome`, `start`, `end` and `http`; refused and failed calls are logged too.

## What it does to the host

- The runner generates its own compose file at
  `<runtime>/docker-compose.agents.yml` from `agents/roster.json` (rendered by
  `agents/compose.py`) plus an optional `<runtime>/state.json`
  (`{"agents": [...]}`, same entry shape, replacing or appending by account).
  It runs compose with `--project-directory <checkout>` so `build: .` and `.env`
  resolve, and writes nothing into the checkout.
- Every action is logged as one JSON line: `ts, action, agent, caller, token,
  result`. Refused writes are logged too.
- The action log (#458, `AGENT_RUNNER_ACTION_LOG`) appends one line per mutating request
  (start, stop, create, retire, walk; a dry-run walk moves nothing, so it is not logged):
  `{"ts", "who", "action", "target", "result": "ok"|"error", "status"}`. `who` is the
  token label, or `unauthenticated` for a rejected token. GETs are never logged, and the
  token, headers and body never are. An unwritable file is warned about once and never fails
  the request. No read endpoint yet.
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
and wowmap are local HTTP servers. `test_docker_backend.py` and the `FakeDockerTest` class
run the fleet against `FakeDockerBackend` with `subprocess.run` patched to raise.
