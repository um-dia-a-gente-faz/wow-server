# Alert runbook

Every rule in `monitoring/prometheus-agent-alerts.yml` links here: its `runbook` annotation is
`docs/RUNBOOK-ALERTS.md#<alert name lowercased>`. Each section says what the alert means, what
to check first, how to tune it, and why the threshold is what it is. Thresholds are starting
values, not measurements; change them in the rules file and in the section together.

The rules file is copied by hand to Prometheus on VM201 and listed in its `rule_files`, so an
edited rule does nothing until that copy is reloaded (`docs/DEPLOYMENT.md`). Routing a firing
alert to a person (Alertmanager receiver or a Grafana contact point) is configured on VM201,
not in this repo, so no webhook or address is committed here.

Agent names: `wow-agent-<name>` is the container (`docker-compose.agents.yml`), where `<name>` is
the roster name in lower case. An agent's `/metrics` is `http://<wow-agents host>:<9600+N>/metrics`,
N being its 1-based position in `agents/roster.json`. The `wow-agents` host address is unconfirmed
in `monitoring/prometheus-agents-scrape.yml`; use the address Prometheus scrapes.

Known gaps, deliberately not alerted yet:

- No rule for the `wowmap` (:9400) or `chat-feed` (:9500) targets: their Prometheus job names live
  in `prometheus.yml` on VM201, which is not in this repo.
- No rule for the chat-feed last-parsed age.
- A single agent that never started, or a single stopped agent's `/metrics` target, is not alerted
  by its own rule. `AgentMetricsScrapeDown` fires only when no agent target is up at all.

## AgentSwallowedErrors

**What it means.** A call site caught an exception with `except Exception:` and went on. The
counter `wow_agent_swallowed_errors_total{agent,where}` is above zero for 10 minutes. The agent
usually keeps running on a fallback, so this is a bug signal, not an outage.

**Check first.**

1. `docker logs --since 30m wow-agent-<name> 2>&1 | grep 'swallowed error in'`: the line names the
   `where` and the exception type, with a traceback on the first report per interval.
2. Grafana, "Jev — decision usage", panel "Swallowed errors / sec (agent /metrics)".
3. `curl -s http://<host>:<port>/metrics | grep swallowed` on the agent to see which call sites count.

**Tune or silence.** Fix the call site rather than silencing it. To tune, change the `for:` or the
`5m` window in the rules file. Do not remove the metric to quiet it.

**Threshold.** Any sustained rate above zero for 10 minutes. Swallowed errors are meant to be rare,
so a rate that persists is worth a look; `for: 10m` drops one-off blips.

## AgentMetricsScrapeDown

**What it means.** No target of the `wow_agent_http` job is up for 15 minutes. Either the scrape
config points at the wrong host or port, or every agent is stopped. One stopped agent does not
fire this rule.

**Check first.**

1. Prometheus on VM201: the targets page for `wow_agent_http` (`/api/v1/targets`). The error column
   says connection refused, no route, or timeout.
2. On the `wow-agents` guest: `docker ps --filter name=wow-agent-` (are the containers up?).
3. The host in `monitoring/prometheus-agents-scrape.yml` against the address of the provisioned
   guest (`docs/WOW-AGENTS-PROVISIONING.md`). The file still carries the placeholder `wow-agents.lan`.

**Tune or silence.** Replace the placeholder host first; until then this alert is meaningless. The
15-minute `for:` keeps a restart or a deploy from paging.

**Threshold.** Fifteen minutes, longer than a container restart, so routine restarts do not fire.

## AgentReconnectLoop

**What it means.** One agent logged more than 5 reconnect waits in 15 minutes, for 15 minutes. The
supervisor (`agent/__main__.py`, `_supervise_connection`) waits between attempts and counts each
wait in `wow_agent_reconnect_attempts_total{agent,reason}`. The `reason` label says why:
`connect_failed` (login or connect raised), `auth_rejected` (a transient auth refusal),
`disconnected` (a drop after a good connect). A permanent refusal stops the agent and is not counted,
so a stopped container shows up in the logs, not here.

**Check first.**

1. `docker logs --since 30m wow-agent-<name> 2>&1 | grep retrying`: each line is a wait and its length.
2. `curl -s http://<host>:<port>/metrics | grep reconnect` for the mix of reasons.
3. Grafana, "WoW — Saúde do Realm", for whether the realm is up and accepting logins.

**Tune or silence.** If restarts after a deploy are normal, raise the threshold. The count is per
agent, so a fleet-wide cause is the storm alert below, not this one.

**Threshold.** The backoff starts at 5 s, doubles, and caps at 5 min. An agent that never gets back
in logs one wait per step, so more than 5 waits in 15 minutes means several minutes of continuous
failure. Waits are jittered, so the count is approximate.

## AgentReconnectStorm

**What it means.** More than 50 reconnect waits across all agents in 10 minutes, for 5 minutes. The
cause is shared: the worldserver, auth or the network. Per-agent rules stay quiet when each agent
logs only a few waits.

**Check first.**

1. Realm health: Grafana, "WoW — Saúde do Realm". Is the worldserver up and are players counted?
2. On VM201's host: `docker compose ps` in `/opt/wow-server` (worldserver and auth containers).
3. A sample agent's log, `docker logs --since 15m wow-agent-<name>`, for the error text all agents share.

**Tune or silence.** Critical, so it goes to a person. Do not restart the worldserver from an alert
response; the owner decides (`CLAUDE.md`).

**Threshold.** 50 waits in 10 minutes is about two per agent across 25 agents. It is a fleet signal,
so it sums across agents.

## AgentNoDecisions

**What it means.** The audit log gained no think cycle for this agent in 15 minutes, for 15 more.
The series is `wow_agent_audit_cycles_total`, derived from the audit rows by the textfile exporter.
Causes: the container is stopped or crash-looping, the think loop is stuck (a hung LLM call), or the
audit path is broken (the pull from `wow-agents` or the textfile cron). Because the path is shared,
**if every agent fires at once, suspect the pull or the textfile cron, not the agents.**

**Check first.**

1. On VM201, the newest row: `tail -n 3 /opt/wow-server-metrics/audit/<agent>/$(date -u +%F).jsonl`.
   If its `ts` is recent, the agent is fine and the path is at fault.
2. On the `wow-agents` guest: `docker ps -a --filter name=wow-agent-<name>` and
   `docker logs --tail 100 wow-agent-<name>`.
3. `scripts/wow-agents/audit-pull-status.sh` for the last pull, and the mtime of
   `/opt/wow-server-metrics/textfile/wow_agent.prom` (the textfile cron, `monitoring/README-agent-metrics.md`).

**Tune or silence.** The windows are the cost of the signal: a stopped agent fires after about 30
minutes, which suits a player-facing realm. Do not shorten them below the textfile cron period.

**Caveat.** An agent that never wrote a think cycle has no series, so this rule cannot fire for it.
The Jev dashboard panel "Think cycles per agent" shows which agents have records at all. Such an
agent is covered by `AgentMetricsScrapeDown` only when the whole fleet is down, so it is one of the
gaps listed above.

**Threshold.** A rate of exactly zero for 15 minutes: the counter has not moved in the window.

## AgentRouterHandlerErrors

**What it means.** A packet handler raised an exception other than `PacketTruncated`. The router
(`agent/router.py`) counts it in `wow_agent_router_handler_errors_total{agent,opcode}` and re-raises.
The `opcode` label names the packet (`agent/opcodes.py`), or shows hex for an unknown one.

**Check first.**

1. `docker logs --since 30m wow-agent-<name> 2>&1 | grep -A20 Traceback`: the traceback names the handler.
2. `curl -s http://<host>:<port>/metrics | grep handler_errors` for which opcodes.
3. The handler for that opcode in `agent/handlers/`, to read the code the traceback points at.

**Tune or silence.** Fix the handler; silencing hides a dropped packet. To tune, change the window.

**Threshold.** Any rate above zero for 10 minutes. The rate window stays above zero for 10 minutes
after one error, so a single isolated error can fire this rule.

## AgentRouterTruncatedPackets

**What it means.** A handler raised `PacketTruncated` (`agent/wire.py`): it read past the end of a
packet. The usual cause is a layout that no longer matches the server build, a drift in the
TrinityCore packet format. The `opcode` label names the packet.

**Check first.**

1. `docker logs --since 30m wow-agent-<name> 2>&1 | grep -B2 PacketTruncated` for the offset and packet.
2. `curl -s http://<host>:<port>/metrics | grep truncated` for which opcodes.
3. The verified layout in `docs/PROTOCOL-NOTES.md` against the TrinityCore source (the
   `trinity-protocol` skill).

**Tune or silence.** Do not silence; a layout drift corrupts the agent's world state silently
otherwise.

**Threshold.** Any rate above zero for 10 minutes, with the same single-error caveat as the handler rule.

## JevErrorRatioHigh

**What it means.** Failed Jev calls exceed 20% of successful calls for one agent, for 15 minutes. The
two sides are `wow_agent_jev_errors_total{agent,status}` (`http_4xx`, `http_5xx`, `error`,
`budget_exhausted`) and `wow_agent_jev_calls_total{agent}`, both rates over 15 minutes from the
audit textfile.

**When it does not fire.** With no Jev calls and no errors, both rates are zero and the ratio is
`0/0`, which is NaN, and NaN does not fire. An idle agent, or one that plays on the LLM brain,
never fires. An agent whose every Jev call fails has errors but no successes, so the ratio is
`+Inf`, which is above 0.2 and does fire.

**Check first.**

1. `grep -h -o 'jev_status[^,]*' /opt/wow-server-metrics/audit/<agent>/$(date -u +%F).jsonl | sort | uniq -c`
   for the status mix today.
2. Grafana, "Jev — decision usage", panel "Jev errors / sec".
3. For `budget_exhausted`, the Jev budget setting in `/opt/wow-server/.env`. Read the key only; never
   paste its value anywhere.

**Tune or silence.** Failed Jev calls fall back to the LLM brain (`docs/AGENT-DIRECTION.md`), so this
is a cost and quality signal, not an outage. Whether to change the brain is the owner's decision.

**Threshold.** 20% of successful calls for 15 minutes: a starting value that tolerates a short
provider blip.

## JevCostBurnHigh

**What it means.** The fleet's reported Jev cost (`wow_agent_jev_cost_usd_total`, summed over
agents) rose by more than 1 USD in the last hour, for 15 minutes. It is the provider's reported
cost in the retained audit rows, not a bill.

**Check first.**

1. Grafana, "Jev — decision usage", panels "Spend total, last 24h" and "Spend per agent, last 24h".
2. Which agent drives it: the per-agent panel, then its audit rows for today.
3. Whether a rotation happened just before the spike (below).

**Rotation caveat.** Retention drops old rows, so the counter can fall and Prometheus reads the drop
as a reset. `increase()` then counts the whole remaining window as new spend. A spike at rotation
time is not spend (`monitoring/README-agent-metrics.md`).

**Tune or silence.** The owner decides on the brain or the budget. The threshold is a starting value;
compare it with the expected daily spend before changing it.

**Threshold.** More than 1 USD in an hour across the fleet, sustained 15 minutes.

## WowExporterDown

**What it means.** `wow_exporter_up` is missing, or it is 0. Missing means the `wow-exporter`
container is down or Prometheus cannot scrape it (port 9300). 0 means the exporter's last MySQL scrape
failed. Both fire this rule. While it fires, the game metrics and the backup metrics are gone, and
`BackupStale` also fires when its metric is missing.

**Check first.**

1. On the VM, in `/opt/wow-server`: `docker compose -f monitoring/docker-compose.yml ps wow-exporter`,
   then `docker logs --tail 50 wow-exporter`.
2. If the container is up but `wow_exporter_up` is 0: is the `trinitycore-db` container up?
   (`docker compose ps` in `/opt/wow-server`.)
3. Prometheus on VM201, the target for the wow-exporter job on port 9300 (`/api/v1/targets`).

**Tune or silence.** Critical, and it should reach a person. Do not silence it while the game
metrics are missing.

**Threshold.** Five minutes: a restart of the exporter should not page.

## BackupStale

**What it means.** The newest finished dump in `/opt/wow-server-backups` is older than 26 hours, or
there is none (`wow_backup_newest_timestamp_seconds` is 0), or the metric is missing because
`wow-exporter` is down (then `WowExporterDown` fires too).

**Check first.**

1. `ls -lt /opt/wow-server-backups | head` on the VM: is there a dump from the last day?
2. `tail -n 50 /var/log/wow-backup.log`: the nightly job is `scripts/backup-db.sh`, scheduled by
   `/etc/cron.d/wow-backup` at 04:30 (`docs/DEPLOYMENT.md`).
3. Grafana, "WoW — Saúde do Realm", the "Backups" row.

**Tune or silence.** Fix the job, not the window. A restore check is in `docs/DEPLOYMENT.md`.

**Threshold.** 26 hours: the daily job at 04:30 plus two hours of slack for a late run.
