# ADR 0004: Jev decision latency, measured — and what actually bounds the think interval

## Status

Proposed. The measurements below are not in dispute; the pacing number that
follows from them is the owner's call. Recommendation in *Decision*.

## Context

The owner asked (2026-10-04): *"what is the frequency of Jev actions? can we
reduce it for like 3 seconds?"*

Two existing documents already answer that, without a measurement behind them:

- `docs/AGENT-DIRECTION.md` §3: *"each agent thinks every 10–30 s, not every 3 s."*
- ADR 0001: expected spend *"(1-5 agents, 10-30s think interval) is well under
  $5/month."*

Neither figure traces to anything recorded, and the live fleet is already outside
the band both assume: the generated `docker-compose.agents.yml` hardcodes
`AGENT_THINK_INTERVAL_S: 5`, i.e. a 5 s loop, not 10–30 s.

The question splits in two, and they have different answers:

- How fast **can** Jev answer? (latency — a fact, measurable today.)
- How fast **should** we ask? (cost and decision quality — a call, this ADR's job.)

This ADR records the measurement and the traps found alongside it, so the next
person does not re-derive them. It changes no code.

## Method

Real, billed calls against the production endpoint, using the production client
rather than a hand-rolled request:

- Endpoint `https://api.typesafe.ai/v1/systemone`, model `jev-latest`, via
  `agent.jev.JevClient` — the same class the agents run.
- Input: a snapshot and candidate set **replayed from the live audit logs**
  (`/opt/wow-server-metrics/audit`), so prompt size is what production actually
  sends, not a synthetic best case. Richest available: 6 candidates,
  3167 input tokens.
- 61 sequential calls, then concurrent bursts of 5, 10 and 25.
- Cross-checked against every Jev-era row in the audit log (44 real production
  calls).

Cost: 101 billed calls, ~320k input tokens, ~$0.013.

## Findings

**F1 — Latency is not the constraint, and it is not close.**

| shape | p50 | p90 | p95 | p99 | max | errors |
|---|---|---|---|---|---|---|
| 61 sequential | 252 ms | 292 ms | 305 ms | 334 ms | 352 ms | 0 |
| burst of 5 | 250 ms | 260 ms | 262 ms | 264 ms | 265 ms | 0 |
| burst of 10 | 266 ms | 279 ms | 288 ms | 295 ms | 297 ms | 0 |
| burst of 25 | 270 ms | 313 ms | 352 ms | 449 ms | 477 ms | 0 |

25 simultaneous decisions were all served in 485 ms wall clock (~3091
decisions/min). Prompt cost is constant per call: 3167 input tokens, 126 output.

Production agrees: across 44 real calls the audit reports p50 254 ms, p90 295 ms,
p95 308 ms, max 364 ms, at p50 3565 / max 4344 prompt tokens. The replay matches
production within 3% at both p50 and p95, which is the evidence that this
benchmark is measuring the real thing.

Consequence: at a **3 s beat Jev's p95 uses 10% of the window**. Sub-second would
still fit on latency grounds. So "why not 3 s?" has an answer that is not about
Jev keeping up.

**F2 — The budgeted cap is not a real one.**

`AGENT_MAX_TOKENS_PER_HOUR` is set in `.env` (500000) and passed through
`docker-compose.agents.yml`, and `docs/AGENT-RUN-1-10.md` step 3 tells the
operator to *"set `AGENT_MAX_TOKENS_PER_HOUR` to a real budget (not `0`/unlimited)"*.
No code reads it. Repo-wide it appears in exactly three places: the generated
compose file, the generator that writes it, and that doc — not even
`.env.example` mentions it. There is no enforcement anywhere, so today the only
thing between a faster loop and the bill is the operator noticing.

**F3 — The interval knob advertised in `.env` is inert.**

`AGENT_THINK_INTERVAL_S` is a hardcoded literal `5`, in both
`docker-compose.agents.yml:26` and the generator that produces it
(`scripts/gen_agents_compose.py:58`). It is **not** `${AGENT_THINK_INTERVAL_S}`,
so the value in `.env` and `.env.example` reaches nothing. And because the compose
file is generated ("Do not edit the services by hand; edit the roster or the
generator and regenerate"), a hand-edit to change it is wiped on the next regen.
Changing pacing today requires a code change to the generator.

**F4 — Most think cycles never consult Jev, and the audit says "success" anyway.**

`JevClient.choose_action()` returns a single candidate without a network call
(`agent/jev.py:199`) — nothing to decide, and Jev bills per input token. Correct
behaviour, with two consequences that matter here:

- The Jev call rate is state-dependent, not a function of the interval.
  Over the same window: Dawnrunner 16 calls in 19 cycles (84%), Jevrun 28 in 125
  (22%). Pooled across all Jev-era rows: 44 of 144 (31%).
- Jevrun's log shows the extreme: after its last real call it ran **97 cycles
  over 8.1 minutes with exactly one candidate each** — no Jev call at all — and
  every one of those rows still records `jev_status: success` with confidence
  `1.0`. `jev_status=success` does not currently mean "Jev decided". That blinds
  both the dashboard and #201.

**F5 — The sleep is additive, so the beat is interval + cycle time.**

`agent/__main__.py:418` is `time.sleep(cfg.think_interval)` after the cycle, not
a deadline. The real period is therefore the interval *plus* perception, the Jev
round trip and the action. At the measured 252 ms p50 this is a ~5% error at
`interval=5` and a ~9% error at `interval=3` — small, but it means "set it to 3"
does not produce a 3 s beat, and the error grows as the interval shrinks.

*Update (#215):* the loop now sleeps `max(0, interval - elapsed)` from the cycle's
start, so the period is the interval and a cycle that overruns it starts the next
one immediately (no negative sleep, no early start). The measured before/after gap
from a paced live run is still to be recorded here.

**F6 — The cost picture in ADR 0001 does not reproduce.**

At 3167 input tokens per decision and ADR 0001's `$0.042`/M input (output free),
one decision costs `$0.000133`:

| scenario | decisions/h | tokens/h | $/h/agent | $/month/agent |
|---|---|---|---|---|
| measured today (1 decision / 7.2 s, Dawnrunner) | ~500 | 1.58M | $0.067 | ~$48 |
| 5 s beat, 84% call rate | 605 | 1.92M | $0.081 | ~$58 |
| 3 s beat, 84% call rate | 1008 | 3.19M | $0.134 | ~$97 |
| 3 s beat, every cycle calls (worst case) | 1200 | 3.80M | $0.160 | ~$115 |

Even ADR 0001's own assumption — 1 agent, 30 s interval, 84% call rate — comes to
~101 decisions/h, ~$0.013/h, **~$10/month**, not "well under $5/month" for 1–5
agents. The prompt simply carries more tokens (p50 3565) than that estimate
assumed.

At the 25-agent target (UM-63/UM-102), a 3 s beat at the measured 3,565-token
production p50 prompt size, with every cycle billed, gives a planning ceiling
of **~$3,234 per 30-day month**: 25 agents × 1,200 calls/hour × 24 × 30 ×
3,565 input tokens × $0.042/M. It assumes one billed decision on every cycle;
single-candidate cycles currently skip the network call, so this is deliberately
conservative. This is an estimate, not an enforced spend cap.

**Price verified:** TypeSafe's [official model reference](https://docs.typesafe.ai/models)
lists Jev 1.13 (`jev-1.13.0`, the version behind `jev-latest`) at $0.042 per
million input tokens, with output tokens free. Checked 2026-10-04. The endpoint
response still has no `usage.cost`, so the published provider price is the
source; monthly totals are arithmetic using that price and the measured usage,
not account-meter totals. The F6 table's per-call arithmetic is confirmed at
that price. See ADR 0001's dated correction for the replacement of its earlier
"well under $5/month" estimate and the same M3 roster ceiling.

## Decision

Recommendation, for the owner to accept or change:

| # | Decision |
|---|---|
| D1 | **Latency is not the constraint.** p95 305 ms against a 3 s window (10%) settles "can Jev keep up" — yes, with an order of magnitude to spare. Ruled out as a reason to pace slowly. |
| D2 | **3 s is acceptable on the evidence**, and is not limited by Jev. It is limited by cost (F6) and by the cap being fictional (F2). |
| D3 | **Make the interval a live knob before tuning it.** The generator emits `${AGENT_THINK_INTERVAL_S:-5}`; the literal is removed. Until this lands, every pacing change is a code change plus a regen. |
| D4 | **Do not speed the fleet up before the spend cap is real.** Either wire `AGENT_MAX_TOKENS_PER_HOUR` (F2) or delete it and document the true ceiling. Speeding up while the only guard is dead config makes the first invoice the discovery. |
| D5 | **`jev_status` must stop reporting `success` for a cycle Jev was never asked** (F4) — the short-circuit gets its own value. Otherwise the observability of #201 and of any future pacing change is false. |
| D6 | **If a specific beat is the goal, sleep to a deadline**, not `sleep(interval)` (F5). |
| D7 | **`docs/AGENT-DIRECTION.md` §3's "every 10–30 s, not every 3 s"** is restated as a cost/pacing preference with these numbers behind it, or superseded — it currently reads as a latency limit, and F1 says it is not one. |

Order matters: D3 and D4 before D2. `AGENT-DIRECTION.md` is the higher authority,
so D7 is an edit to that document, not something this ADR overrides.

## Consequences

- Pacing becomes an `.env` change with a container restart instead of a code
  change, which is what `.env.example` already claims it is.
- A 3 s beat roughly doubles spend against today; the ADR now says by how much.
- Recording the true Jev call rate (F4) makes the audit and the Jev dashboard
  honest about when Jev is and is not deciding.
- ADR 0001's spend sentence is superseded by F6 — it needs a correction pointing
  here rather than being left to mislead the next reader.
- The 25-agent scale-out decision (UM-63/UM-102) gets a cost number to decide
  against, replacing a guess.

## Alternatives considered

- **Take the owner's "3 seconds" as a latency question and just set it.** Rejected
  as the *whole* answer: it is cheap and correct on latency (F1) but leaves the
  inert knob (F3) and the fictional cap (F2) in place, so the first 3 s fleet runs
  uncapped on a number nobody can re-tune without a code change.
- **Benchmark against a synthetic prompt instead of replayed audit snapshots.**
  Rejected: prompt size is the main driver of latency and cost, and a synthetic
  snapshot would have made both look better than they are. The replay matching
  production within 3% is the reason to trust F1's numbers at all.
- **Benchmark the OpenRouter path instead, since ADR 0001's price is for it.**
  Rejected: production runs native TypeSafe (`JEV_PATH=/v1/systemone`), so
  measuring the non-production path would answer the wrong question. The price
  mismatch is recorded as a caveat under F6 instead.
- **Raise `AGENT_THINK_INTERVAL_S` in the compose file directly.** Rejected: F3 —
  the file is generated, so the change is wiped and looks like it worked until
  then.
- **Treat the 69% of cycles with one candidate as headroom to raise the interval.**
  Rejected: F4 shows that fraction is state-dependent (22% vs 84% call rate on the
  same day), so it is not a stable capacity saving to spend.

## Related tickets

- #202 — this ADR.
- Follow-ups, one per decision item (all on milestone *M3 - Jev decision brain*):
  - #212 — D3: make `AGENT_THINK_INTERVAL_S` a runtime knob in the generator.
  - #213 — D4: wire or delete `AGENT_MAX_TOKENS_PER_HOUR`. Wired: per agent, rolling hour, input+output, breach fails the cycle (`jev_status: budget_exhausted`).
  - #214 — D5: stop recording `jev_status: success` for a cycle Jev was never asked.
  - #215 — D6: sleep to a deadline instead of the interval being additive.
  - #216 — F6: verify the native TypeSafe per-token price and correct ADR 0001's
    spend estimate.
  - #217 — D7: restate `AGENT-DIRECTION.md` §3's pacing on the measured basis.
  - #218 — D2: set the interval to 3 s. Blocked by #212 and #213; never stacked.
- #201 — the loop guard wedging the agent when the candidate set collapses to
  idle. F4 is the same failure seen from the audit side, and D5 is the
  observability half of it.
- #98 — the 1→10 unattended exit-criterion run: the run where a 3 s beat would
  actually be evaluated.
- UM-63 / UM-102 — 25-agent roster scale-out: F6 is the cost input that decision
  needs, and ADR 0001 already asks for the ceiling to be revisited at this point.
- ADR 0001 — Jev as the decision brain. F6 corrects its spend estimate.
- ADR 0003 — the Jev decision cache (Proposed, not built). F4 — `choose_action()`
  already skips single-candidate cycles for free — is a datum that ADR is missing:
  the "duplicate decision" rate it wants to cache against is partly this.

## How to reproduce

The benchmark is ad hoc (not committed; it is a one-off measurement, not a
harness): replay a snapshot from the audit log through the real client against the
live endpoint, and read `last_latency_ms` / `last_usage`.

A single production-shaped call, for a sanity check:

```bash
ssh root@192.168.1.64
cd /opt/wow-server
set -a; . .env; set +a
python3 scripts/jev_probe.py
```

Two traps worth knowing before writing your own, both hit while producing this ADR:

1. `JEV_PATH` defaults to `/decisions`, which is the OpenRouter path. A call
   against native TypeSafe without `JEV_PATH=/v1/systemone` returns
   `404 {"detail":"Not Found"}`. `.env.example` documents both providers correctly;
   ad-hoc scripts that build a client by hand must pass the path through.
2. `choose_action()` makes **no network call** when the candidate set has one
   entry. A benchmark that reuses an idle snapshot measures nothing and reports
   zero latency — check the candidate count, or the numbers are silently empty.
