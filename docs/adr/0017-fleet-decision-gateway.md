# ADR 0017: Where fleet-wide LLM/Jev limits live

## Status

Proposed (#599, the ADR part of #372). The owner accepts it later. No code, config or
compose file changes with this ADR.

The file name keeps the working title of #372 ("fleet decision gateway"). The decision
below is **not** a gateway: nothing is put between an agent and its provider.

## Context

#372 asks where fleet-wide policy for brain calls lives: a shared rate limit, a shared
cooldown, a fleet budget and a circuit breaker for up to 25 agents that share one provider
key. It recommends a gateway sidecar that every agent calls instead of the provider.

Every claim in #372 was checked against `main`. What is true today:

### Per process (one per agent container)

- **Clients.** `Brain.from_config` (`agent/brain.py`) builds one `JevClient` and/or one
  `LLMClient` per process, called from `agent/__main__.py`. Both post through
  `agent/httpjson.py::post_json` straight to the configured base URL.
- **LLM cooldown.** `LLMClient._cooldown_until` (`agent/llm.py`) is an in-memory dict, set by
  `_cool_down` from that process's own 404/429/5xx/timeout/network failures, using the
  server's `Retry-After` or "Soonest reset" hint (`httpjson.retry_after_seconds`), clamped
  to `MIN_COOLDOWN_S`..`MAX_COOLDOWN_S` (5 s..900 s). When every model is cooling,
  `choose_action` raises without sending a request.
- **Jev cooldown.** `Brain._maybe_cool_down` reads `_COOLDOWNS`: 401/402/403 stop Jev calls
  for `JEV_AUTH_COOLDOWN_S` (300 s) and 429 for `JEV_RATE_LIMIT_COOLDOWN_S` (30 s). It does
  not read `Retry-After`. A 5xx, a timeout or a network error has **no** cooldown.
- **Jev token budget.** `TokenBudget` (`agent/tokenbudget.py`): a rolling hour of
  input+output tokens against `AGENT_MAX_TOKENS_PER_HOUR`. `Brain.__init__` creates one per
  process and `Brain.restore_jev_spend` reloads it from that agent's own audit files at
  start. It meters Jev only: LLM tokens are not counted (owner decision on #505, which says
  a shared LLM budget belongs to #372).
- **Settings.** `agent/config.py::Config` is read once at start. Changing a model, a
  provider or a cap means editing `.env` and restarting the agent containers.

### Already shared

- **The keys and base URLs.** The `&env-defaults` anchor in `docker-compose.agents.yml`
  passes `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `JEV_BASE_URL`, `JEV_PATH`,
  `JEV_API_KEY`, `JEV_MODEL`, `OPENROUTER_API_KEY` and `AGENT_MAX_TOKENS_PER_HOUR` from the
  one host `.env` to every service. All 25 containers hold the same keys.
- **The LLM brain already has a gateway.** `LLM_BASE_URL` points at the FreeLLMAPI router
  (`.env.example`, `docs/DEPLOYMENT.md`), one LAN service that holds the upstream provider
  keys, routes to free models and answers a rate-limited model with 429 and a reset hint.
  Agents hold only the router key. That router is outside this repo.
- **Observation of spend.** Every cycle's `usage`, `prompt_tokens`, `completion_tokens`,
  `brain`, `jev_status` and `fallback` go to the audit log (`agent/audit_schema.py`), one
  directory per agent under one host directory (the `&audit-volume` mount).
  `agent/metrics.py::_derive_jev` turns them into `wow_agent_jev_*_total` per agent, and the
  `JevCostBurnHigh` rule in `monitoring/prometheus-agent-alerts.yml` sums cost across the
  fleet. `tools/agent-runner` reads every agent's audit directory (`Fleet.audit`,
  `tools/agent-runner/fleet.py`). So the fleet's usage is already observable in one place.
  What is missing is enforcement, not a place to read from.
- **A fleet ceiling, by multiplication.** With `AGENT_MAX_TOKENS_PER_HOUR` set, the fleet
  cannot spend more than the cap times the number of agents (`.env.example` says so). The
  default is 0, unlimited.

### Corrections to #372

| #372 says | On `main` |
|---|---|
| "There is no spend or token budget across agents" | There is a per-agent Jev budget (`TokenBudget`, #213, #454, #505) and therefore a static fleet ceiling. There is no pooled budget, no LLM budget and no switch that stops the whole fleet's brain calls. |
| "No circuit breaker: 25 independent timeouts" | True for Jev on 5xx, timeout and network errors only. The LLM client's per-model cooldown is a breaker (opens on failure, sends nothing while open, tries again when the time is up); Jev has one for 401/402/403/429. All of them are per process. |
| The gateway exposes "the same `choose_action` contract" | `choose_action` is a Python method. On the wire there are two unrelated contracts: OpenAI `POST /chat/completions` with tools (`agent/llm.py`) and the Decisions API `POST {JEV_BASE_URL}{JEV_PATH}` (`agent/jev.py`). A proxy has to carry both. |
| Shed means "fall back to the scripted/reflex brain per `brain.py`" | There is no such fallback. `BrainError` makes `think.decide` (`agent/think.py`) fail the cycle. `ScriptedDecider` (`agent/deciders.py`) is for tests and smoke runs; `AGENT_BRAIN` accepts `llm` or `jev` only. Reflexes run on their own threads (ADR 0009) and keep running when the brain does not decide. |
| A gateway "gives #327 one place to read real usage from" | #327 already specifies usage "derived from the audit records", which exist and are mirrored to the wow-server VM (ADR 0002 D4). |
| Confirm with "call rate and latency from a #299 soak run" | `tools/loadtest` runs with `brain=None` (`docs/loadtest-baseline.md`): it measures no brain call. The numbers below come from ADR 0004. |
| #278 and #252 "touch this" | #278 is closed: the `Decider` protocol is in `agent/deciders.py`. #252 is open with no decision. The router counters of #494 (`metrics.count_router_event`) count game packets, not brain calls. |

### Numbers

- **Call rate.** 25 agents at the compose default `AGENT_THINK_INTERVAL_S` of 5 s make at most
  300 brain calls a minute; at the code default of 3 s, 500. Jev is called on 22% to 84% of
  cycles (ADR 0004 F4), so 66 to 252 a minute at 5 s.
- **Jev capacity.** ADR 0004 F1: a burst of 25 simultaneous decisions, 0 errors, all served
  in 485 ms; p50 252 ms, p95 305 ms sequential. Those bursts are the only fleet-sized
  measurement in the repo and they recorded no 429. The provider's published rate limit is
  not in this repo (open question 5).
- **Cost of the retry stampede.** On a Jev 429 each agent learns it alone and waits 30 s:
  25 refused calls per 30 s, 50 a minute, where a shared cooldown would send about 2. On a
  Jev outage with fast 5xx answers nothing cools down: up to 300 failing calls a minute at
  5 s. With timeouts each think thread sits in `DEFAULT_TIMEOUT_S` (20 s) per cycle. For
  the LLM brain a rate-limited model costs each agent one request to a LAN router, then the
  cooldown holds.
- **Worst-case spend.** ADR 0004 F6: 3.80M tokens per hour per agent at a 3 s beat with every
  cycle billed, so 95M tokens per hour for 25 agents, about 26,000 tokens per second, about
  $4 per hour at $0.042 per million input tokens.
- **Room on the host.** `wow-agents` has 4 GiB (ADR 0002 D1); 25 containers at
  `mem_limit: 128m` reserve 3.2 GiB of it.

### Constraints

`agent/` is stdlib-only and has no database access. Nothing may need a worldserver restart.
`docs/AGENT-DIRECTION.md` §4 says "no hidden channel, no shared state between agent
processes" and §5 that no memory is shared: agents must not learn what other agents see or
decide. #599 adds the conservative default: the smallest new runtime dependency and nothing
hard to undo. #372 (*Scope of the ADR*) sets the failure mode: "gateway down must degrade to
current behaviour or to the scripted brain, never wedge the think loop". That is the
keep-working rule used below: with the shared component down, agents keep deciding.

## Options considered

| Option | Cost | What it breaks | How it is undone |
|---|---|---|---|
| **A. In-process**, one shared client object in an agent pool (#252) | Nothing new to run. Depends on #252, which is undecided and reverses "one process per character". | One crash takes every agent down. Not available until the pool exists. | Revert the pool. |
| **B. Gateway sidecar**: an HTTP proxy on `wow-agents` that all brain calls go through | A new always-on service in the path of every decision, carrying two wire contracts, with its own auth (#294) because it can spend the provider key. The added latency on the same host is estimated at under 1 ms against 252 ms p50, and the memory at about 30 MB (the interpreter floor in `docs/loadtest-baseline.md`). | A proxy that is down fails every agent's cycle. Keeping agents alive then needs a direct path to the provider, which needs the key in the containers, which removes the one security gain. Every snapshot and decision of every agent passes through one process. | Point `*_BASE_URL` back and restart 25 containers. |
| **C. Shared limiter in a file or DB, written by the agents** | No new process. Needs locks between 25 writers on a bind mount, or a database, which `agent/` may not use. | Fragile under a crash holding a lock; agents would read each other's state. | Remove the setting. |
| **D. Limits file, one writer, agents read** (chosen) | One small file on a read-only mount, one optional host process later. Agents add a file read per cycle. | Nothing when it is absent or unreadable: agents fall back to today's per-process policy. It cannot queue requests or react within a second. | Unset one setting, or delete the file. |
| **E. No shared component**: fix the per-process gaps only | Nothing to run. | No pooled budget, no shared LLM budget (#505), no fleet stop short of stopping containers. | Nothing to undo. |

## Decision

**D. Fleet-wide policy is decided outside the agents and enforced inside them, through a
limits file. Brain calls stay direct.**

1. **The data path does not change.** Each agent keeps calling its provider itself, with
   its own clients, cooldowns and `TokenBudget`. No proxy, no new hop, no new port.
2. **The shared state is one JSON file** in a directory mounted read-only into every agent
   container. It carries time-bounded **blocks**: "do not call brain X until T, because R".
   A fleet budget that is spent, an owner stop, a provider cooldown and an open breaker are
   all the same thing to an agent, a block with a reason and an end time.

   ```json
   {"version": 1, "written_ts": 1700000000.0,
    "blocks": {"jev": {"until_ts": 1700000030.0, "reason": "fleet Jev budget spent"},
               "llm": {"until_ts": 1700003600.0, "reason": "owner stop"}}}
   ```

   Keys of `blocks` are the brain names `jev` and `llm`. Times are epoch seconds; the
   containers and the writer share the host clock. Unknown keys are ignored. **The only
   expiry is `until_ts`.** `written_ts` is informational (for people and for the health
   signal); no agent decision reads it, and the file has no maximum age.
3. **One writer.** The file is written by one process on the agent host, by writing a
   temporary file and renaming it over the old one. Agents never write it. Until a writer
   exists, the owner may write it by hand.
4. **`Brain` is the enforcement point.** Before a Jev call and before an LLM call,
   `Brain.decide` asks whether that brain is blocked. A blocked Jev behaves as a spent
   per-agent budget does today: no call, the cycle fails, or the LLM substitutes when
   `AGENT_BRAIN_FALLBACK=llm`. A blocked LLM fails the cycle without a request. Reflexes are
   not affected.
5. **Fail open.** A missing, unreadable, oversized, malformed or wrong-version file is no
   block. A block ends by itself at `until_ts` and at no other time. Every block the writer
   computes (budget, cooldown) has `until_ts` at most two writer periods ahead, 30 s with
   the 15 s period, and is renewed while its cause lasts. So a writer that dies leaves
   blocks that lapse by themselves within 30 s, and the fleet is back on per-process policy.
   A hand-written owner stop may carry a longer `until_ts`. It outlives a dead writer, by
   design: a stop must not end because a process crashed. It is cleared by removing its
   entry from the file, or by removing the file. The per-agent `AGENT_MAX_TOKENS_PER_HOUR`
   is the only cap that does not depend on the file, and its default is 0 (off).
6. **Only limits cross the boundary.** The file holds brain names, times, reasons and
   counters. It never holds a snapshot, a candidate, a decision or a key. This is what
   keeps it inside `AGENT-DIRECTION` §4 and §5 (open question 1).
7. **Per-process gaps are fixed per process.** The missing Jev cooldown on 5xx, timeout and
   network errors, and jitter on cooldown expiry, need no shared component and are not part
   of the limits file.

### Why the others lose

- **B** fails the failure-mode rule of #372: with the proxy down, agents stop deciding. The
  variant that survives keeps the keys in the containers and so gives up B's security
  argument, and that argument is thin here anyway: all containers run on one host from one
  `.env`, so anyone who can read a container's environment can read the file. B's other
  claimed gains do not hold: the LLM brain already has a router, and #327 reads audit
  records. B buys request-level queueing and fairness, for a rate limit nobody has hit
  (ADR 0004 F1), at the price of a single point of failure where there is none today. B is
  also where a cache shared between agents would be added later; ADR 0003 section 4 keeps
  the cache per agent and `AGENT-DIRECTION` §5 keeps memory private.
- **A** is not available: it waits on #252 and makes one crash fleet-wide. If #252 is
  adopted, the shared object can read the same file, so D does not block A.
- **C** is D with 25 writers. One writer removes the locking and the cross-agent reads that
  make C fragile.
- **E** leaves two things undone that have an owner decision or money behind them: the
  shared LLM budget that #505 assigned to #372, and a fleet stop that keeps the characters
  online. E's fixes are kept as item 7.

### When to revisit

Build B only if one of these becomes true: a provider enforces a concurrency or
per-second limit the fleet actually hits (the file cannot queue); agents start calling a
paid provider directly with a key that must not sit in 25 containers; or the owner decides
in #294 that keys leave the agent containers. The file contract survives that change: a
proxy can be its writer.

## Consequences

- A fleet limit is advisory with a delay. An estimate of the overshoot of a fleet token
  cap from the writer period alone is about 0.4M tokens (15 s at 26,000 tokens per second),
  under 2 cents. It is not a worst case. It leaves out the agent's own read delay (one
  think interval), calls already in flight when the block appears (up to
  `DEFAULT_TIMEOUT_S`, 20 s) and the lag before a call's spend is in the audit log and seen
  by the writer. It holds only while the writer runs.
- **The uncapped case.** When the writer is down there is no fleet cap (an owner stop
  already in the file stays). When the file is missing, unreadable, malformed (a typo in a
  hand edit) or of another `version`, there is no fleet cap and no owner stop. With
  `AGENT_MAX_TOKENS_PER_HOUR` at its default 0 there is then no cap at all: spend is bounded
  only by the call rate, about $4 per hour by the numbers above. The only thing that
  notices today is `JevCostBurnHigh` (`monitoring/prometheus-agent-alerts.yml`: more than
  1 USD in an hour, held for 15 minutes), so at that rate about 30 minutes and about $2
  later. Each agent logs the problem once, in its own container log; nothing alerts on
  it. The *Health signal* slice below exists to close this, and open question 4 asks the
  owner to set the per-agent cap.
- The file cannot do a token bucket, per-request fairness or sub-second `Retry-After`
  propagation. Fairness between agents stays what it is: the per-agent cap.
- The provider keys stay in the agent containers.
- A block reaches an agent on its next cycle, with no restart. Caps and the owner stop move
  to the writer's side, so changing them no longer restarts 25 containers. Models and
  provider URLs still do.
- The directory must be mounted, not the file: a single-file bind mount pins the inode and
  the container would never see the renamed file.
- One more `jev_status` in the audit log, `fleet_blocked`. It is **not** a Jev error and is
  not counted in `wow_agent_jev_errors_total`: a fleet block is the limit working. Counted
  there, every blocked agent would have errors and no successful calls, the
  `JevErrorRatioHigh` ratio would be `+Inf`, and an owner stop or a spent fleet budget would
  page once per agent, up to 25 times, after 15 minutes.
- Once the mount exists, the owner can stop every Jev or LLM call in the fleet by writing
  one file, and the characters stay logged in with their reflexes.

### First slice (ready issue outline)

**Title:** `feat(agent): fleet limits file — Brain skips a brain that the file blocks`

Agent side only. Off by default, so merging it changes nothing on the live fleet.

**Files**

- New: `fleetlimits.py` in `agent/` and `test_fleetlimits.py` in `agent/tests/`
- `agent/brain.py`, `agent/config.py` (`agent/metrics.py` is read, not changed)
- `agent/tests/test_brain_scenarios.py`, `agent/tests/test_metrics.py`,
  `agent/tests/test_architecture.py` (`LAYERS`: the new module, and `fleetlimits` in
  `brain`'s allowed imports)
- `.env.example` (the standalone-only section), `docs/ARCHITECTURE.md` (the *Deciding* row)
- `monitoring/prometheus-agent-alerts.yml`: the `JevErrorRatioHigh` description lists the
  statuses that count and says `fleet_blocked` does not. The expression does not change.
- `docs/RUNBOOK-ALERTS.md`, `JevErrorRatioHigh`: the same sentence next to the status list,
  and a "check first" step for `fleet_blocked` (it does not fire this alert; look at the
  limits file on the agent host and at who wrote the block).

A change under `monitoring/` recreates the monitoring stack only (`stacks_for` in
`scripts/deploy.sh`), not the game stack.

**Exported names**

- `fleetlimits.py`: `SCHEMA_VERSION = 1`, `MAX_BYTES = 65536`, and
  `class FleetLimits(path: str, clock: Callable[[], float] = time.time)` with
  `blocked(brain: str) -> str | None`.
- `agent/config.py`: setting `AGENT_FLEET_LIMITS_FILE` (str, default `""`, `compose=False`),
  field `Config.fleet_limits_file`.
- `agent/brain.py`: `JEV_STATUS_FLEET = "fleet_blocked"`; `Brain.__init__` gains
  `fleet: FleetLimits | None = None`; `Brain.from_config` builds one when
  `cfg.fleet_limits_file` is set.

**Behaviour**

- `blocked(brain)` returns `f"{brain} blocked by fleet limits until <ISO time>: <reason>"`
  while `blocks[brain].until_ts > clock()`, else `None`. That comparison is the whole
  expiry rule: `written_ts` is not read, and an old file with a block whose `until_ts` is
  still ahead blocks. The reason is cut to 200 characters.
- It stats the file on every call and parses it again only when the modification time or
  size changed. It never raises: a missing file, a read error, more than `MAX_BYTES`,
  invalid JSON, a `version` other than `SCHEMA_VERSION`, or a block whose `until_ts` is not
  a finite number all give `None`. Each distinct problem is logged once, not once per
  cycle.
- `Brain.decide`, Jev path: the fleet check runs after `_jev_skip_reason()` and before
  `self.budget.exhausted("jev")`. When it blocks, `jev_status` is `JEV_STATUS_FLEET`, Jev is
  not called, and the rest follows the existing budget path (fail the cycle, or substitute
  the LLM when it is configured).
- `Brain.decide`, LLM path: when `blocked("llm")` returns a reason, raise `BrainError` with
  it and do not call `self.llm.choose_action`.
- `AgentMetrics.jev_errors` does **not** gain a key. `_derive_jev` (`agent/metrics.py`)
  counts a status as an error only when it is a key of `m.jev_errors` (the test is
  `status in m.jev_errors`), so a `fleet_blocked` record moves neither
  `wow_agent_jev_errors_total` nor `wow_agent_jev_calls_total`. An agent that is blocked for
  the whole window has a ratio of `0/0`, which is NaN and does not fire
  (`docs/RUNBOOK-ALERTS.md`, "When it does not fire").
- With `AGENT_FLEET_LIMITS_FILE` empty, `Brain.fleet` is `None` and no file is touched.

**Done when**

| Item | Check |
|---|---|
| An active block is reported, an expired one is not | `test_fleetlimits.py`: write a file with `until_ts` 30 s ahead of an injected clock, assert the reason; advance the clock 31 s, assert `None` |
| `until_ts` is the only expiry | `test_fleetlimits.py`: a file with `written_ts` one day behind the clock and `until_ts` 30 s ahead still blocks; the same file without `written_ts` blocks too |
| Every bad input fails open | `test_fleetlimits.py`: one case each for missing file, directory instead of file, 70 kB file, invalid JSON, `version: 2`, `until_ts: "soon"`, `blocks` a list; each returns `None` and does not raise |
| The file is read again only when it changed | `test_fleetlimits.py`: count `open` calls over 3 `blocked()` calls on an unchanged file (1), then replace the file by rename and assert the new block is seen |
| A replaced file is seen (rename, not in-place write) | same test, using `os.replace` |
| Jev is not called while blocked | `test_brain_scenarios.py`: a `ScriptedDecider` in the jev slot that fails the test if called; `decide` raises `BrainError`, `decision.jev_status == "fleet_blocked"` |
| The LLM substitutes when configured | `test_brain_scenarios.py`: jev blocked, llm slot scripted; the decision has `brain == "llm"`, `substituted is True`, `jev_status == "fleet_blocked"` |
| The LLM is not called while blocked | `test_brain_scenarios.py`: llm blocked; `BrainError`, the scripted llm was not called |
| No setting, no change | `test_brain_scenarios.py`: `Brain.from_config` with an empty `fleet_limits_file` gives `brain.fleet is None`; the existing brain tests pass unchanged |
| A fleet block is not a Jev error | `test_metrics.py`: `derive_metrics` over audit records with `jev_status: "fleet_blocked"` leaves every value of `jev_errors` and `jev_calls_total` at 0, and the rendered text has no `status="fleet_blocked"` sample |
| The alert text and the runbook say so | `grep -n fleet_blocked monitoring/prometheus-agent-alerts.yml docs/RUNBOOK-ALERTS.md` shows the description, the status list and the "check first" step; `git diff` shows the `expr:` line of `JevErrorRatioHigh` unchanged |
| Everything CI runs | `scripts/check.sh all` |

### Further slices

1. **Mount and setting in the fleet.** The compose renderer mounts
   `<runtime>/fleet:/data/fleet:ro` and passes `AGENT_FLEET_LIMITS_FILE=/data/fleet/limits.json`;
   `docs/DEPLOYMENT.md` documents the hand-written owner stop. Touches `scripts/` and the
   compose file, so it waits for the owner's go-ahead.
2. **Writer.** A new tool `fleet-limits` under `tools/`, a stdlib host process on
   `wow-agents`: tails the audit directory, sums Jev tokens (`usage`) and LLM tokens
   (`prompt_tokens`, `completion_tokens` of `brain: "llm"` records) over a rolling hour,
   blocks a brain while its fleet cap is exceeded, merges the owner's stop, and rewrites the
   file every 15 s. Every block it computes has `until_ts` at most 30 s ahead (two periods);
   only the owner's stop may be longer.
3. **Health signal.** A gauge for the limits file's state (`ok`, `missing`, `invalid`,
   `off`) and its age from `written_ts` on the agent's `GET /metrics`
   (`agent/http_api.py`), a counter of fleet-blocked cycles, and an alert when a configured
   file is not `ok` or, once the writer exists, is older than 60 s.
4. **Per-process resilience.** Jev cooldown on 5xx, timeout and network errors, reading
   `Retry-After` on a Jev 429, and jitter on cooldown expiry in both clients.
5. **Shared provider cooldown.** The writer blocks a brain when several agents report 429 in
   the same window. Build only after the audit log shows it happening (open question 5).
6. **Console.** The runner serves the file and the fleet's hourly usage for #327; an
   admin-only control sets and clears the owner stop.

## Open questions for the owner

Each has a default so the first slice is not blocked.

1. **Does `AGENT-DIRECTION` §4 ("no shared state between agent processes") allow a limits
   file?** Default: yes, because it carries limits only and nothing an agent perceives or
   decides (decision item 6). If no, option E is what remains.
2. **Fleet Jev cap.** Default: off until set. A value consistent with the existing
   `JevCostBurnHigh` alert (1 USD per hour) is about 23.8M tokens per hour.
3. **Fleet LLM cap (#505).** The LLM brain uses free models, so the cap protects a rate
   limit, not money. Default: off; when set, tokens per rolling hour, counted like Jev's.
4. **Fail open when the writer is down?** Default: yes, as the failure-mode rule of #372
   requires. The owner is asked to set `AGENT_MAX_TOKENS_PER_HOUR` above 0 on every agent:
   its default is 0, and without it a fleet with no readable limits file has no cap
   (see *Consequences*).
5. **What are the providers' rate limits?** Neither Jev's nor the free router's is recorded
   in this repo. Default: do not build slice 5 until two fleet-wide 429 events are found in
   the audit log.
6. **Do the provider keys have to leave the agent containers?** This decision leaves them
   there. Default: accept it, and decide with #294.
7. **Should an agent whose Jev is blocked by the fleet substitute the LLM** when
   `AGENT_BRAIN_FALLBACK=llm`? Default: yes, as with the per-agent budget. An owner stop
   that must silence both brains writes two blocks.
8. **Where does the writer run?** Default: its own systemd unit, not a thread in
   `tools/agent-runner`, so the process that owns the Docker socket does not grow and
   neither can take the other down.

## Related

#372 (the problem), #599 (this ADR), #505 (LLM budget belongs to #372), #252 (agent pool),
#278 (`Decider`), #294 (control-plane auth), #327 (usage in the console), ADR 0001 (brains),
ADR 0002 (agent host), ADR 0003 (cache stays per agent), ADR 0004 (measured latency and
spend), ADR 0009 (threads).
