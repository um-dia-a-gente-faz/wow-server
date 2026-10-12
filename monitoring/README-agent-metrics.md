# Agent decision-audit metrics (UM-51)

The AI agent (`agent/`) writes one JSONL record per think cycle via
`agent.audit.AuditLogger` to `AGENT_AUDIT_DIR/<agent>/<YYYY-MM-DD>.jsonl`
(default `/data/audit`, size-rotated, retained for
`AGENT_AUDIT_RETENTION_DAYS` days — default 14). `docker-compose.agents.yml`
mounts the host directory `/opt/wow-server-metrics/audit` at `/data/audit`
in every agent container.

## Why textfile, not an HTTP `/metrics` server

Each agent container just runs the think loop to completion (or forever);
there's no existing UM-50 `/metrics` HTTP server to hang a collector off of
here. Rather than stand up a fifth (sixth, ...) always-on port per agent,
this follows Prometheus's [textfile collector][textfile] pattern, the same
shape `monitoring/docker-compose.yml`'s `node-exporter` already uses for
host metrics.

[textfile]: https://github.com/prometheus/node_exporter#textfile-collector

## Pieces

- `agent/metrics.py` — pure derivation: JSONL records in, `AgentMetrics`
  dict + Prometheus exposition text out. No filesystem/network access, so
  it's unit-tested directly (`agent/tests/test_metrics.py`).
- `monitoring/agent_metrics_textfile.py` — reads every
  `AGENT_AUDIT_DIR/*/*.jsonl*`, renders the text, and writes it atomically
  (temp file + rename) to a `.prom` file. A bad line (see
  `wow_agent_audit_bad_lines_total`) is skipped and counted, never fatal; every label
  value is escaped (quote, backslash, newline), and a lone surrogate is written as its
  backslash-u text (`\\ud800`), so the file always writes and distinct values stay distinct.
- `monitoring/docker-compose.yml`'s `node-exporter` mounts
  `/opt/wow-server-metrics/textfile:/textfile:ro` and runs with
  `--collector.textfile.directory=/textfile`, so any `*.prom` file dropped
  there gets scraped alongside host metrics on the existing `:9100` target
  — no new Prometheus job needed.

## Running it

On the wow-server VM (192.168.1.64), run the exporter script periodically
(cron, or a `sleep`-loop sidecar) so the `.prom` file stays fresh:

```bash
*/2 * * * * cd /opt/wow-server && python3 monitoring/agent_metrics_textfile.py \
    --audit-dir /opt/wow-server-metrics/audit \
    --out /opt/wow-server-metrics/textfile/wow_agent.prom
```

## Metrics exposed

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `wow_agent_audit_cycles_total` | counter | `agent` | Think cycles recorded |
| `wow_agent_audit_tool_calls_total` | counter | `agent`, `valid` | Tool calls split by validity |
| `wow_agent_action_total` | counter | `agent`, `action`, `outcome` (`ok`/`invalid`/`failed`) | Actions taken |
| `wow_agent_llm_prompt_tokens_total` | counter | `agent` | Prompt tokens consumed |
| `wow_agent_llm_completion_tokens_total` | counter | `agent` | Completion tokens consumed |
| `wow_agent_llm_latency_ms` | histogram | `agent` | LLM call latency |
| `wow_agent_deaths_total` | counter | `agent` | Deaths observed |
| `wow_agent_levelups_total` | counter | `agent` | Level-ups observed |
| `wow_agent_xp_per_hour` | gauge | `agent` | XP/hour over the log's time span |
| `wow_agent_decisions_by_prompt_total` | counter | `agent`, `prompt_id` | Cycles by the audit record's `prompt_id`; the 8 ids with the newest record per agent keep their label, older ones are summed under `other`, a missing/empty/non-string id is `none`. A literal id `other` or `none` is summed into that label, never overwritten. The textfile exporter is the only producer (not on the agents' `GET /metrics`); the "Decisions by prompt" panel reads it |
| `wow_agent_audit_bad_lines_total` | counter | `agent` | Audit lines the exporter skipped: not UTF-8, not JSON, or JSON that is not an object. The agent is the line's directory name (`<audit-dir>/<agent>/`). Skipped lines never stop the run |
| `wow_agent_jev_calls_total` | counter | `agent` | Successful Jev Decisions requests |
| `wow_agent_jev_errors_total` | counter | `agent`, `status` | Failed requests (`http_4xx`, `http_5xx`, `error`); `budget_exhausted` is a skip, not a failed call: the hourly Jev token budget was spent and Jev was not called (#689) |
| `wow_agent_jev_fallback_total` | counter | `agent` | Cycles where Jev was configured but another brain decided |
| `wow_agent_jev_prompt_tokens_total` / `wow_agent_jev_completion_tokens_total` | counter | `agent` | Jev input/output tokens |
| `wow_agent_jev_cost_usd_total` | counter | `agent` | Cumulative provider reported USD cost in retained audit records |
| `wow_agent_jev_cost_usd_24h` | gauge | `agent` | Rolling 24-hour reported spend |
| `wow_agent_jev_confidence` | gauge | `agent` | Latest recorded Jev confidence |
| `wow_agent_jev_low_confidence_total` | counter | `agent` | Cycles where the confidence policy substituted the safe candidate (`confidence_rule=low_confidence_safe_fallback`) |
| `wow_agent_jev_latency_ms` | histogram | `agent` | Decisions API latency |

`wow_agent_swallowed_errors_total{agent,where}` is not audit-derived: Prometheus scrapes it
from each agent's `GET /metrics` (job `wow_agent_http`, `prometheus-agents-scrape.yml`),
shown on the Jev decision-usage dashboard and alerted by `prometheus-agent-alerts.yml`.
Both files are merged by hand into the Prometheus config on VM201. `AgentMetricsScrapeDown`
fires when no agent target is up for 15m (wrong host/port, or the fleet is stopped); individual
stopped agents do not page. The scrape host `wow-agents.lan` is unconfirmed: set the real address.

`wow_agent_reconnect_attempts_total{agent,reason}` is also live, on the same `GET /metrics`. It counts
each reconnect wait the supervisor takes (#402), by `reason`: `connect_failed` (login or connect raised),
`auth_rejected` (a transient auth refusal), `disconnected` (an unexpected drop after a good connect). A
permanent refusal stops the agent and is not counted. The waits are jittered (equal jitter, between half
and the full backoff), so a fleet restarted together does not retry in lockstep. All three reasons are
exposed from 0, so `rate()` works from the first scrape. Process-wide, so it resets when the agent restarts.

The fleet limits file (ADR 0017) is visible on the same `GET /metrics` (#675), as of the agent's last think
cycle (the file is read only when a brain decides, so an agent that is not thinking shows a stale state):
`wow_agent_fleet_limits_state{agent,state}` is 1 for the current `state` among `ok`, `missing`, `invalid`
and `off` (no limits file configured) and 0 for the others; `wow_agent_fleet_limits_age_seconds{agent}` is
the seconds since the file's `written_ts`, present only when the state is `ok` and `written_ts` is a number;
`wow_agent_fleet_blocked_cycles_total{agent,brain}` (`jev`, `llm`, both from 0) counts think cycles in which
the file blocked that brain. `/metrics` only reads the state the think thread recorded; it never reads the file.

The **Jev — decision usage** dashboard is in
`monitoring/grafana-dashboard-jev-decision-usage.json`. Metrics are rebuilt
from retained audit rows on each textfile-exporter run, so every `*_total`
series (like the pre-existing LLM ones) reflects the retention window, not
lifetime. When rotation drops old rows the value decreases, which Prometheus
treats as a counter reset, so `rate()`/`increase()` can spike once at that
moment; read the cost panels as approximate across a rotation. The spend gauge
`wow_agent_jev_cost_usd_24h` is unaffected. Failed Jev calls that fall back to
the LLM keep their `jev_status` and billed usage in the audit record. Cycles
with `jev_status=skipped_single_candidate` are excluded from the successful
call count: the exporter counts only `success`, and the dashboard uses that
metric. This makes successful audit rows the actual Jev request count, so the
call rate can be calculated from the audit without treating the
single-candidate short-circuit as a request. Agents with audit cycles but no
Jev calls are highlighted on the dashboard.
The live Prometheus instance currently has no Jev samples; panel expressions
parse, and these panels need a real Jev-backed cycle after deployment to show
usage.

`prompt_tokens`/`completion_tokens`/deaths/level-ups/XP depend on the LLM
provider reporting `usage` and on `result.detail` (or the snapshot) carrying
`died`/`leveled_up`/`xp` — best-effort, `0`/absent when a provider or action
doesn't supply them.
