# loadtest

Soak harness (#299). Runs N real agent sessions (auth, `WoWSession`, recv thread, think
loop, reconnect supervisor) against an in-process `tools/world-mock` and records
RSS, threads, CPU, tick period, reconnects and recovery time into one JSON report.
Stdlib only; Linux (reads `/proc`).

```bash
python3 -m tools.loadtest --agents 25 --duration 300                  # the manual soak
python3 -m tools.loadtest --agents 25 --duration 60 --out run.json --compare docs/loadtest-baseline.json   # same shape as the baseline
# with faults (MOCK_FAULTS syntax, repeated for the whole run, see tools/world-mock/README.md)
python3 -m tools.loadtest --agents 25 --duration 600 \
  --faults ';;;drop_after=8,reset;;stall=20,stall_at=6;bad_crypt_from=6'
python3 -m unittest discover -s tools/loadtest/tests                   # CI: 3 agents, 5 s smoke
```

`--compare` exits 1 when `rss_per_agent_kb` or `threads_per_agent` is more than
`--tolerance` (default 20 %) above the baseline. It refuses (exit 1, message, before the soak starts) when `agents`,
`duration_s` or `think_interval_ms` differ from the baseline, or the baseline lacks those keys.
The committed baseline is N=25, 60 s, 100 ms.

**It never targets the live realm.** The harness starts its own mock and the agents
connect to it. `--host` must be loopback (`127.0.0.1`, `localhost`; the mock is IPv4-only, so `::1` is refused later with a socket error); anything else
exits unless `--allow-non-loopback` is given (the mock still runs locally).

## What the report means

- Everything runs in one process, so `rss_*`, `threads_*` and `cpu_pct_*` include the
  mock and the harness. `rss_per_agent_kb` = (peak RSS - RSS before the run) / N, and
  includes the mock's per-connection cost. It is a regression signal, not an exact bill.
- `cpu_pct_total`: whole-process CPU over wall time for the run, login burst included;
  `cpu_pct_steady`: the same after the first 5 s. Percent of one core.
- `tick_period_ms`: gap between two think cycles of one agent, read from the loop's
  `perception:` log record. Compare p50/p99 with `think_interval_ms`. Percentiles come from a fixed 0.1 ms histogram
  (upper bucket edge), so the recorder's memory does not grow with run length. With no
  brain the loop just sleeps to a deadline: this measures wake-up jitter, not think latency.
- `reconnects`: login attempts beyond the first, per agent. A `reset` that lands during
  login is one reconnect. `recovery_s`: time from "went down" to "online again"
  (includes the reconnect backoff; since #434 `logout()` skips the logout exchange
  after an unexpected disconnect, so there is no 5 s wait on a dead stream).
- `container_items`: total `len()` of every list/dict/set/deque held directly by the
  `WoWSession` and its `WorldState`, per sample. A number that keeps climbing in a long
  healthy run is an unbounded collection: file a bug. The mock sends no stream after
  login, so this only catches growth driven by the login burst and reconnects.
- `threads_end` should equal `threads_before` (no leaked threads).
- `samples`: one row per second.

## What the harness is not

It calls `_run_think_loop` with `brain=None`, not the full `_run_loop`. Absent: the follow
and rest reflex threads, the observer HTTP server, the audit logger and the chat relay.
`threads_per_agent` counts the agent thread, the recv thread and one mock handler thread
per agent; a real agent has at least two more. So the numbers are a floor for a connected
idle session, not the footprint of a real agent (see `docs/loadtest-baseline.md`).

## Not done yet

- No per-client stream of update objects, movement and chat from the mock, so the recv
  loop's packet rate and growth under traffic are not measured.
- Time is real time. An injectable clock (#351) would let faults and backoff be
  simulated instead of waited for.
- Numbers on the VM (the 6 GB budget) have not been taken.
