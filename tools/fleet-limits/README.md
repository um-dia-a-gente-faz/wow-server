# fleet-limits

The writer of the fleet limits file (ADR 0017, further slice 2, #745). One stdlib host process
for `wow-agents`. Every period it:

1. reads the audit files modified within the last hour under `FLEET_AUDIT_DIR`
   (`<dir>/<agent>/<day>.jsonl[.N]`; the `<agent>/lifecycle/` subdirectory is not read),
2. sums Jev tokens (`usage.input_tokens + usage.output_tokens`, the sum `Brain.restore_jev_spend`
   takes) and LLM tokens (`prompt_tokens + completion_tokens` of records with `brain: "llm"`)
   over records with `now - 3600 < ts <= now`,
3. blocks a brain while its sum is at or above its cap, for 30 s (two periods),
4. merges the owner's stop file, which wins for the same brain and keeps its own `until_ts`,
5. writes `FLEET_LIMITS_FILE` (`version` 1, the format at the top of `agent/fleetlimits.py`)
   through a temporary file in the same directory, `fsync` and `os.replace`, mode 0644.

Nothing reads the file until the mount of ADR 0017 further slice 1 exists; running this changes
nothing on the live fleet before then.

Agents fail open, so this process never turns its own trouble into a block. An unreadable audit
directory or file counts as no usage (one warning per distinct problem, not per cycle) and the
owner stop is still written. An unreadable or invalid owner file keeps the previous valid owner
stop for as long as the process lives (at start: no owner stop). A failed write is a warning and a
retry next period; the old file stays and its computed blocks expire by themselves.

## Settings (environment)

| Variable | Default | Meaning |
|---|---|---|
| `FLEET_AUDIT_DIR` | required | the agents' audit directory |
| `FLEET_LIMITS_FILE` | required | the file the agents mount (the directory is what is mounted, never write in place) |
| `FLEET_JEV_TOKENS_PER_HOUR` | `0` | fleet Jev cap over the rolling hour; `0` is off |
| `FLEET_LLM_TOKENS_PER_HOUR` | `0` | fleet LLM cap; `0` is off |
| `FLEET_OWNER_STOP_FILE` | `owner-stop.json` next to the limits file | the owner's stop |
| `FLEET_LIMITS_PERIOD_S` | `15` | seconds between cycles |

`fleet_limits.py --once` runs one cycle and exits (0 if the file was written). It logs the hour's
sums, which is how to compare them with the Jev dashboard:

    last hour: jev 41230 of 500000 tokens, llm 0 of off tokens; blocked: none

## The owner stop file

Same shape as `blocks`. Hand-written, rewritten whole by whoever edits it (write a temporary file
and `mv` it over, so the writer never sees half of it):

```json
{"jev": {"until_ts": 1790000000, "reason": "maintenance window, back at 22:00"}}
```

`until_ts` is a unix time; only `jev` and `llm` mean anything. An entry whose `until_ts` is not a
finite number, or whose brain is another, is dropped with one warning. An entry whose `until_ts`
has passed is not a block (it does not hide a computed one). To lift the stop, delete the file or
the entry. Reasons are cut to 200 characters.

## Sample systemd unit

Text only; nothing installs it.

```ini
[Unit]
Description=Fleet limits writer (ADR 0017)
After=network.target

[Service]
User=wow-agents
Environment=FLEET_AUDIT_DIR=/opt/wow-agents/audit
Environment=FLEET_LIMITS_FILE=/opt/wow-agents/runtime/fleet/limits.json
Environment=FLEET_JEV_TOKENS_PER_HOUR=500000
ExecStart=/usr/bin/python3 /opt/wow-server/tools/fleet-limits/fleet_limits.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

SIGTERM and Ctrl-C end the process cleanly between cycles.
