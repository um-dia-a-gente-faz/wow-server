# Load-test baseline (#299)

First numbers from `python3 -m tools.loadtest` (`tools/loadtest/README.md`), the
baseline that #252 (agent pool, shared caches) and #251 (event bus) are measured against.

**Where and what.** A local dev host (Linux, Python 3.12), not the VM, 2026-10-08. Healthy
runs, no faults, `think_interval` 100 ms, agents and `tools/world-mock` in one process.
The mock sends nothing after login, so these are the floor of a *connected idle* agent: the
per-agent cost of a real world full of update objects will be higher. RSS includes the
mock's per-connection cost. The "~50 MB each" in `docs/AGENT-DIRECTION.md` is a whole
agent container; this is only the Python session and perception state.

| N | duration | RSS before -> peak | RSS per agent | threads peak (per agent) | CPU mean | tick p50 / p99 / max | reconnects | dropped pkts |
|---|---|---|---|---|---|---|---|---|
| 1 | 60 s | 30.2 -> 31.5 MB | 1296 kB (one-off costs dominate at N=1) | 6 (5.0) | 0.2 % | 100.1 / 100.1 / 100.2 ms | 0 | 0 |
| 5 | 60 s | 30.0 -> 32.0 MB | 388 kB | 18 (3.4) | 0.6 % | 100.1 / 100.2 / 100.4 ms | 0 | 0 |
| 25 | 300 s | 30.0 -> 37.6 MB | 301 kB | 78 (3.1) | 1.8 % | 100.1 / 100.2 / 105.0 ms | 0 | 0 |

- Threads per agent: recv thread plus the mock's auth and world handler threads, plus the
  agent's own thread; all back to the starting count after the run (`threads_end` = 1).
- Collections: `container_items` (sizes of the lists/dicts held by `WoWSession` and
  `WorldState`) settled at 3 per agent once logged in (63 -> 75 at N=25 during the first seconds, then flat) for the whole 300 s run. No unbounded growth was
  found, so no bug issue was filed; but with no traffic from the mock this proves little
  about `events` / `chat_inbox` / audit buffers under load.
- `docs/loadtest-baseline.json` is the full N=25 report (with per-second samples), usable
  as `--compare` input.
- The 6 GB VM budget is **not** verified here; re-run on the VM and replace these numbers.
