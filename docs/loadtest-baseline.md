# Load-test baseline (#299)

First numbers from `python3 -m tools.loadtest` (`tools/loadtest/README.md`), the
baseline that #252 (agent pool, shared caches) and #251 (event bus) are measured against.

**Where and what.** A local dev host (Linux, Python 3.14.4), **not the VM**, 2026-10-08.
Healthy runs, no faults, `think_interval` 100 ms, one duration (60 s) for every row,
agents and `tools/world-mock` in one process. The mock sends nothing after login, so
these are the floor of a *connected idle* agent. The "~50 MB each" in
`docs/AGENT-DIRECTION.md` is a whole agent container; this is only the Python session
and perception state.

| N | RSS before -> peak | RSS per agent | threads peak (per agent) | CPU total / steady | tick p50 / p99 / max | reconnects | dropped pkts |
|---|---|---|---|---|---|---|---|
| 1 | 29.4 -> 30.7 MB | 1268 kB | 6 (5.0) | 0.1 % / 0.1 % | 100.1 / 100.2 / 100.3 ms | 0 | 0 |
| 5 | 29.4 -> 31.2 MB | 362 kB | 18 (3.4) | 0.4 % / 0.4 % | 100.1 / 100.2 / 100.9 ms | 0 | 0 |
| 25 | 29.5 -> 33.8 MB | 174 kB | 78 (3.1) | 1.6 % / 1.4 % | 100.1 / 100.2 / 101.8 ms | 0 | 0 |

RSS is flat after login (N=25: 33.7 MB at 1 s, 33.8 MB at 59 s). The tick recorder is a
fixed-size histogram allocated before the baseline RSS is read, so it no longer grows
with run length. Check: N=25 for 30 s gives 176 kB per agent and for 300 s gives
179 kB per agent. The few kB that remain are the harness's own `samples` list and the mock's `received`
list, not `agent/`.

## Reading it: fixed plus marginal

RSS per agent falls with N because the first session pays one-off costs (imports of
the session code paths, crypto tables, the mock's first handler). Fitting
`total = fixed + marginal * N` to the three rows (total = per agent x N: 1268, 1810,
4350 kB) gives roughly **1.1 MB fixed + 130 kB per additional agent**. The N=1 row is
first-session cost, not per-agent cost. So: an idle connected session costs about
0.13 MB on top of a ~29 MB interpreter, with agents as threads in one process.

## What is measured, and what is not

- **Not the real agent.** The harness calls `run_think_loop` directly with
  `brain=None`, not `run_loop`. Absent per agent: the follow and rest reflex threads,
  the observer HTTP server, the audit logger and the chat relay. A real agent has at
  least two more threads of its own and these subsystems' memory.
- **Threads per agent** = agent thread + recv thread + **one mock handler thread per
  agent** (the mock's, not the agent's; included in the count and the 3.1). It is not the
  thread count #252 will see.
- **Tick period** without a brain is the interval plus scheduler/GIL wake-up jitter,
  by construction (`run_think_loop` sleeps to a deadline). It shows that 25 idle
  sessions do not delay each other's wake-ups. It is not think-cycle latency with a
  brain, and not recv-loop latency.
- **CPU** is the whole process (mock, sampler, agents) as a share of one core.
  `cpu_pct_total` = total process CPU over wall time, login burst included;
  `cpu_pct_steady` skips the first 5 s. Idle sessions ticking at 10 Hz, no recv work.
- **RSS per agent** = (peak - RSS read after imports, before the mock starts) / N, so it
  includes the mock's per-connection cost.
- Collections: `container_items` (sizes of the lists/dicts held directly by `WoWSession`
  and `WorldState`) is 3 per agent once logged in (72 -> 75 at N=25 in the first
  seconds, then flat). The probe is one level deep: the name/item/quest/NPC-text caches
  and `HandleMap` are not counted. No unbounded growth found, but with no mock traffic
  this proves little about `events` / `chat_inbox` / audit buffers under load.

**What it can support for #252:** a floor for session memory (about 130 kB marginal,
1.1 MB first-session) and evidence that threads-as-sessions in one process wake up on
time. **What it cannot:** today's one-container-per-agent cost (that is the ~29 MB
interpreter plus this), the thread count or CPU of a real agent with reflexes and a
brain, a world that sends objects, or the cache savings #252 wants (the caches are empty
here). Treat any decision that needs those as unmeasured (#421).

`docs/loadtest-baseline.json` is the full N=25, 60 s report (per-second samples), usable
as `--compare` input for a run with `--agents 25 --duration 60 --think-interval 0.1`;
`--compare` refuses any other shape.
The 6 GB VM budget is **not** verified here; re-run on the VM and replace these numbers.

## Stream soaks (#486)

Four 600 s soaks, `--agents 25 --duration 600 --stream-hz 20`, one after another, on the same
local host as above (Linux, Python 3.14.4, **not the VM**), loopback only, commit `67fd0be`,
2026-10-11. Runs 2 and 4 use the README fault spec. "End" is the last sample taken with the
agents still connected (t ~ 600 s); the report's own `end` sample comes after teardown.

| run | stream_pool | faults | container_items 60 s / 300 s / end | RSS kB 60 s / 300 s / end (peak) | threads before -> end | reconnects | tracebacks |
|---|---|---|---|---|---|---|---|
| 1 | 50 | none | 3850 / 3850 / 3850 | 42644 / 42736 / 42880 (42880) | 1 -> 1 | 0 | 0 |
| 2 | 50 | README spec | 3234 / 3234 / 3234 | 44268 / 54348 / 117940 (121988) | 1 -> 1 | 4 | 1 |
| 3 | 0 | none | 76499 / 377666 / 751372 | 96676 / 337640 / 621896 (621896) | 1 -> 1 | 0 | 0 |
| 4 | 0 | README spec | 60130 / 313229 / 628956 | 87472 / 298604 / 583820 (590772) | 1 -> 1 | 4 | 1 |

Reading it:

- **Object table with `--stream-pool 0`** (runs 3, 4): grows one unit per tick, as designed. A
  scratch probe of one session (60 s, pool 0) gave `world_state.objects` = 600 at 30 s and 1203 at
  60 s. Tick p99 stays ~107 ms but the max reaches 332-350 ms and CPU rises to 45-57 % of one core.
- **`world_state.names._in_flight` and `_pending`** also grow with pool 0 (1202 and 605 at 60 s, one
  per tick): the chat senders' name queries are never answered by the mock, so nothing clears them.
  The default pool (50) bounds them, which is why runs 1 and 2 are flat. Whether a real server
  answers every query is not checked here.
- **RSS in run 2** (default pool, faults) rises from 44 MB at 60 s to 118 MB at the end, starting
  around 400 s. The probed collections stay flat (3234 at all three points), so the growth is not
  in them. Not attributed; the mock's own `received` list grows for the whole run and is a candidate.
  Run 1 without faults stays flat at ~42.7 MB.
- **Threads**: no leak in any run (1 before, 1 after; 78 at peak = 3 per agent plus the mock).
- **Tracebacks** (runs 2 and 4): one `ConnectionResetError` on `sendall` during login. The supervisor
  retried; four reconnects per run, recovery p50 ~0.3 s. Every agent came back.

## Stream soak run 2 after #570

Run 2 of the table above repeated on the PR branch `bugfix/gh-570-soak-rss-growth`, 2026-10-16,
same host and Python, no `tracemalloc`. Only change: the recv loop no longer buffers the body of a
packet nobody handles (see #570). "End" is the last sample with the agents still connected (t ~ 590 s).

| run | stream_pool | faults | container_items (min / max) | RSS kB 60 s / end (peak) | threads before -> end | reconnects | login errors |
|---|---|---|---|---|---|---|---|
| 2 before #570 | 50 | README spec | 3234 / 3234 | 44268 / 117940 (121988) | 1 -> 1 | 4 | 1 |
| 2 after #570 | 50 | README spec | 1285 / 3725 (flat at ~3213) | 45880 / 48084 (48096) | 1 -> 1 | 4 | 4 `connect failed` (the reset) |

The end is 4.8 % above the 60 s value (limit 20 %). Ticks p50 100.1 ms, p99 103.0 ms; recv 50 packets/s per agent.
The `before` row is from the table above, not rerun.
