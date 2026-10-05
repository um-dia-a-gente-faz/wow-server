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
  (temp file + rename) to a `.prom` file.
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
| `wow_agent_jev_calls_total` | counter | `agent` | Successful Jev Decisions requests |
| `wow_agent_jev_errors_total` | counter | `agent`, `status` | Failed requests (`http_4xx`, `http_5xx`, `error`) |
| `wow_agent_jev_fallback_total` | counter | `agent` | Cycles where Jev was configured but another brain decided |
| `wow_agent_jev_prompt_tokens_total` / `wow_agent_jev_completion_tokens_total` | counter | `agent` | Jev input/output tokens |
| `wow_agent_jev_cost_usd_total` | counter | `agent` | Cumulative provider reported USD cost in retained audit records |
| `wow_agent_jev_cost_usd_24h` | gauge | `agent` | Rolling 24-hour reported spend |
| `wow_agent_jev_confidence` | gauge | `agent` | Latest recorded Jev confidence |
| `wow_agent_jev_low_confidence_total` | counter | `agent` | Cycles where the confidence policy substituted the safe candidate (`confidence_rule=low_confidence_safe_fallback`) |
| `wow_agent_jev_latency_ms` | histogram | `agent` | Decisions API latency |

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
