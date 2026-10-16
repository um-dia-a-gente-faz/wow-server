# ADR 0021: Agent scale-out, persistent per-agent state

## Status

Accepted (owner decision, 2026-10-16, on #252, option B). Closes #721, which asked the same
question. The measurements in the last section are not taken yet: they need the fleet host and
live agents, which no CI or loop agent can reach.

## Context

Each agent is one container with `mem_limit: 128m` and `cpus: 0.25` (`agents/compose.py`,
`x-agent`). A raid is 25 of them. Every container starts with an empty `~/.cache` in its writable
layer, so the creature and gameobject name cache (`names.json`, ADR 0018) is cold after every
container recreate, which is every new image tag. Each agent then re-asks the server for the same
templates that its 24 siblings already asked for. ADR 0016 measured the cold cache and found it
was never invalidated; #751 fixed that with the server's cache id, and #749 added a shared read-only
world bundle. What was still open is where a warm cache lives across a recreate.

Two options were on the table:

- **A. One process, N sessions.** Host the 25 characters in one process with a shared cache. This
  removes the per-container overhead, but it changes the one-container-one-agent isolation that the
  roster, the audit log, the observability ports and the per-agent restart all rely on.
- **B. Keep the containers, add persistent state.** Each agent gets its own directory on the agent
  host, mounted read-write at `/data/state`, and its `names.json` lives there.

## Decision

Option B. Containers stay; option A is out.

- **Per agent, not shared.** `persist.write_atomic` names its temp file `<path>.tmp<pid>.<thread>`,
  and every container is pid 1 with the same thread ids, so two containers writing one shared file
  could collide on the temp name. Per-agent files also keep ADR 0009's rule of one writer per file.
- **The directory is mounted, not the file.** The mount is `/opt/wow-server-metrics/agent-state:/data/state`
  (a directory, read-write), and each service sets `AGENT_STATE_DIR: /data/state/<slug>`
  (`agents/compose.py`, SERVICE). The variable is per service, so it is not in the shared
  `env-defaults` anchor, and a value in `.env` never reaches the containers
  (`agent/config.py`, `compose=False`). A standalone run leaves it empty and uses the home cache.
- **Set before the load.** `agent/login.py::apply_state_dir` points `names.cache_path` and
  `items.cache_path` into that directory, called in `connect_and_login` after the session exists and
  before `login_character()` runs `names.load()`. The order is tested.
- **`items.json` stays unwritten** (#712: a whole-file save per answer costs too much for a file
  nothing reads). Its default path moves with `names.json`, so a future caller finds it in the same
  place. No save or load is added.

## Consequences

- **Disk.** About 1 MB per agent, about 25 MB for a raid. No pruning is needed.
- **Memory.** 25 containers at `mem_limit` 128 MB is 3.2 GB of the 6 GB VM, by construction and not
  by measurement. The measurements below check it against real use.
- **Invalidation.** A cache written under one server data generation is dropped on the next login
  whose server id differs (#751, `NameCache._discard_file_names_if_stale`). A recreate does not make
  a stale cache trusted.
- **Ownership.** Docker creates the bind-mounted directory as root, and the agent runs as root, so no
  ownership step is needed.
- **Rollback.** An older image reading a newer-schema file leaves it alone and does not save to it
  (ADR 0018, names.json row). Rolling back loses the warm cache, not data.
- **Runner.** `tools/agent-runner` renders the same mount under its runtime directory
  (`<runtime>/state`), the same way it renders `/data/world`.

## Measurements (not taken)

To be taken during the next fleet run, from the agent host (human step, unticked in #252's PR):

1. `docker stats --no-stream` across the raid, sampled for 10 minutes, to check the 128 MB and
   0.25 CPU limits hold under load.
2. The count of creature and gameobject queries in the first 5 minutes after login, cold (no
   `names.json`) against warm (second login), from one agent's log.

The numbers go on #252. If the cold count is small, the cache is not worth the volume and this
ADR is revisited; if the warm count is near zero, it is confirmed.
