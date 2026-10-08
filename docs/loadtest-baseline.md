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

- **Not the real agent.** The harness calls `_run_think_loop` directly with
  `brain=None`, not `_run_loop`. Absent per agent: the follow and rest reflex threads,
  the observer HTTP server, the audit logger and the chat relay. A real agent has at
  least two more threads of its own and these subsystems' memory.
- **Threads per agent** = agent thread + recv thread + **one mock handler thread per
  agent** (the mock's, not the agent's; included in the count and the 3.1). It is not the
  thread count #252 will see.
- **Tick period** without a brain is the interval plus scheduler/GIL wake-up jitter,
  by construction (`_run_think_loop` sleeps to a deadline). It shows that 25 idle
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
