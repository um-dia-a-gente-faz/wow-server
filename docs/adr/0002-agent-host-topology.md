# ADR 0002: Agent host and control-plane topology for the agent fleet console

## Status

Accepted (decisions taken by the owner 2026-10-03; recorded in #134)

## Context

The console milestone (*console-v3-agent-control*) adds start/stop/create for
the agent fleet from the observability site. Five of its tickets point at the
same topology questions: where the agents run, which process may touch the
Docker socket, where the panel lives, how the decision audit log reaches the
existing consumers, and what "create a character" means. Without a decision of
record each ticket would re-decide them slightly differently.

Facts that shaped the choice:

- The `wow-server` VM (192.168.1.64, 6 GB) has no room for 25 agent containers
  (`docs/DEPLOYMENT.md`, *Resource limits*), and every merge to `main`
  auto-deploys it, which disconnects players. The fleet must not share that
  blast radius.
- `tools/wowmap` has no authentication and is reachable from the whole LAN.
  Mounting `docker.sock` into it would give anyone who loads the page root on
  the agent host.
- The audit log (`<agent>/<day>.jsonl`, UM-51) already feeds the wowmap
  activity feed, the node-exporter textfile metrics and Grafana, all on the
  wow-server VM, reading `/opt/wow-server-metrics/audit`.
- The agent host's checkout is updated by `git fetch` + fast-forward, so a
  dirty tracked file breaks that silently.

## Decision

| # | Decision |
|---|---|
| D1 | The agent fleet gets its own guest on `pv1`: a **VM** named `wow-agents` (2 vCPU / 4 GiB / 40 GB, `cpu host`, Docker + compose). Not an LXC with Docker nesting, and not VM 305. |
| D2 | The control plane is a single service, `tools/agent-runner`, on the agent host (the Docker socket, the audit dir and the agents' APIs are all local to it). HTTP on **:9700**, shared token (`AGENT_RUNNER_TOKEN`) from `.env`, LAN only, every action logged with the caller IP. |
| D3 | The website stays on the wow-server VM (192.168.1.64). The fleet panel is a new section of `tools/wowmap`; it reaches the runner over the LAN and never holds the Docker socket. |
| D4 | The agent host owns the audit dir; the wow-server VM **pulls** it on a short timer so the existing consumers (wowmap activity feed, node-exporter textfile metrics, Grafana) keep working unchanged. No NFS mount that agent audit writes depend on. |
| D5 | `agents/roster.json` stays the committed plan of record. Runtime state lives outside the git checkout on the agent host (`/opt/wow-agent-runtime/`) and the runner generates its own compose file there. Nothing in the checkout is written at runtime. |
| D6 | Character creation is create-only. Retiring = stop the container + mark it retired; the character is never deleted. One character per account (AGENT01..25) for now. |

### Audit flow (D4, concretely)

- **Who writes:** the agent containers on `wow-agents`, to
  `/opt/wow-agent-runtime/audit/<agent>/<day>.jsonl` (bind-mounted at
  `/data/audit`, size-rotated and pruned by the agent itself, 14 days).
- **Who pulls:** a systemd timer on the wow-server VM runs
  `rsync -a` over SSH with a read-only key, from
  `wow-agents:/opt/wow-agent-runtime/audit/` into
  `/opt/wow-server-metrics/audit/`, every 30 s. The files are append-only
  JSONL, so a repeated pull is idempotent and a missed one is caught up by the
  next. The mirror is pruned on the pull side (files older than the 14-day
  retention); the pull never uses `--delete`, so an empty or rebuilt agent host
  cannot wipe history.
- **Consumers are unchanged:** wowmap, `monitoring/agent_metrics_textfile.py`
  and Grafana keep reading `/opt/wow-server-metrics/audit` on the wow-server VM.
- **When the pull fails:** nothing on the game side breaks. The activity feed
  and agent metrics go stale (the newest file's age grows) and recover on their
  own when the pull succeeds again. The runner reports the audit age from the
  *source* directory (`GET /agents` → `audit.age_seconds`), so "agent silent"
  and "pull broken" are distinguishable: fresh at the source and stale on the
  mirror means the pull. The timer's `OnFailure` unit logs to the journal; it
  does not retry in a loop.

The exact timer cadence and SSH key setup are provisioning details owned by
#135; the contract here is "pull, short interval, read-only, no `--delete`".

### Naming trap

**VM 305 is called `agents` but is the coding-CLI dev host** (claude, codex,
antigravity, opencode and kimi users, Ubuntu 26.04, no Docker). The WoW fleet's
guest is **`wow-agents`**. One word apart, guaranteed to be conflated: anything
about the fleet means `wow-agents`.

## Consequences

- A second guest to keep patched and backed up, in exchange for isolating the
  fleet's CPU/RAM and keeping auto-deploys of the realm independent of it.
- `wowmap` needs the runner's URL and token (`AGENT_RUNNER_URL`,
  `AGENT_RUNNER_TOKEN`); if the runner is down the panel must show *unknown*,
  never "healthy".
- The Docker socket is only ever reachable by a host systemd unit running the
  runner, not by a container that also serves HTTP.
- The audit log exists in two places with up to one pull interval of lag on the
  wow-server copy.
- `docker-compose.agents.yml` in the repo stays a manual-run reference; the
  runner's generated compose file under `/opt/wow-agent-runtime/` is what the
  fleet actually runs.
- Retired characters accumulate on the realm; cleanup is a deliberate human
  act, not a console button.
- Any of D1–D6 can change, but as a visible edit to this file with a reason,
  not silent drift.

## Alternatives considered

- **Agents stay on the wow-server VM.** Rejected: no RAM, and every deploy
  would restart or disconnect them.
- **LXC with nested Docker.** Rejected: nesting weakens isolation and breaks
  across Proxmox upgrades; a VM costs little more.
- **Reuse VM 305.** Rejected: it is the coding-CLI dev host and has no Docker.
- **Runner inside `wowmap`, or `docker.sock` mounted into wowmap.** Rejected:
  an unauthenticated LAN page would hold root on the agent host.
- **Shared NFS audit directory.** Rejected: agent audit writes would block or
  fail with the mount; pulling keeps the agents independent of the wow-server VM.
- **Runner writes into the git checkout.** Rejected: a dirty tracked file
  breaks the fast-forward auto-update silently.

## Related tickets

Milestone *console-v3-agent-control*: #134 (this ADR), #135 (provision
`wow-agents`), #136 (runner API), #138 (create/retire characters),
#137 (fleet panel), #139 (capability probe), #130 (agents never online),
#131 (stale agent image), #132 (`AGENT_API_URLS` unset), #133 (LLM gateway 503).
