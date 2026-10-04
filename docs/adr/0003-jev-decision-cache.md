# ADR 0003: A cache for Jev decisions

## Status

Proposed. Recommendation: **do not build yet** — see *Recommendation*. The
numbers that decide it have not been collected: no recorded run was available
to the author (see *Numbers*).

## Context

Phase 1 (ADR 0001) is uncached Jev for every think cycle: cheap
(`$0.042`/M input tokens, output free), constrained to the offered options.
The owner's framing (2026-10-03): *"if a player is with low life, he should
heal — that's clear behaviour. So we can discuss a caching system for Jev's.
First we use Jev for everything, then we can plan the caching logic."* This
ADR is the plan, plus the tooling to say whether it is worth building (#167).

Facts about the current code the design rests on:

- `agent.candidates.generate()` builds the option list from the snapshot;
  `JevClient.choose_action()` already skips the network when only one
  candidate exists (`agent/jev.py`), so single-option cycles are free today.
- Candidate ids embed GUIDs/handles (`auto_attack:guid=u3`), so two cycles
  facing two different wyrms never share raw ids. Handles are per-session
  (`agent/handles.py`).
- Every `Action.check()` runs before `execute()` (`agent/actions.py`), so a
  stale choice already fails safely; what is missing is *counting* it.
- The audit record has `candidates` (a count) and `confidence`, but not the
  option set or a situation key, and carries the full snapshot only every
  `FULL_SNAPSHOT_EVERY`'th cycle (`agent/audit.py`).

## Decision (proposed)

### 1. Key

`key = sha1(json(state, options))`, with sorted keys:

- `state.hp`, `state.mana`: band of `me.health` / `me.mana` as a fraction:
  `crit` < 0.25, `low` < 0.5, `mid` < 0.8, else `full`; `?` when unknown. The
  edges are where the obvious behaviour changes (rest, heal, flee).
- `state.combat`: number of candidates labelled as units attacking the agent,
  capped at 3 (the generator's `MAX_THREATS`).
- `state.window`: `window.kind` or null. `state.dead`: dead or ghost.
- `state.quests`: sorted `(quest_id, state_name)` of the quest log.
- `state.bag_free`: free bag slots banded `0`, `1-4`, `5+` (to be added to the
  snapshot; not in it today).
- `options`: the sorted *set* of normalised candidates. A candidate is
  `action(k=v,...)` with sorted params; a `guid`/`npc_guid` is replaced by
  `<entry>@<distance band>` (bands: ≤5 yd melee/interact, ≤15, ≤40, beyond).
  Coordinates, raw GUIDs, handles, labels and exact distances never enter the
  key. `me.level` is not in the key (it invalidates, see 4).

The reference implementation is `situation_key()` / `normalise()` in
`agent/tools/cache_probe.py`, with tests: nearby positions, a same-band health
change and a respawned mob with a new GUID all give the same key; a health band
change gives a different one.

### 2. Value

Per key: the chosen *normalised* option (never raw params — a hit re-resolves
it to the live candidate with the same normalised form, so the fresh GUID is
used), Jev's confidence, `hits`, `last_ok` / `fail_streak` (result of the last
execution of this entry), `created_ts`. An entry with `fail_streak >= 1` is
not served and is dropped; a miss then asks Jev and overwrites it. Only
entries with confidence ≥ the confidence-policy threshold (#165) are stored.

### 3. Safety

On a candidate hit: (a) the normalised option must still be in the current
candidate list, else `cache_rejected_stale`; (b) the resolved candidate goes
through the same `check()` as an uncached choice, and a `check()` failure is
also `cache_rejected_stale`, and falls through to a real Jev call in the same
cycle. The loop guard (`repeat_blocked`) applies to cached choices unchanged.

### 4. Expiry and scope

Per agent, in memory plus a persisted file (so a restart keeps it). TTL 10
minutes of wall clock. Cleared on level-up, map/zone change, death/release,
and any `JEV_MODEL` change. Not shared between agents at first: classes differ,
and the point of per-agent scope is that a bad entry hurts one character.

### 5. Toggle and observability

`JEV_CACHE=off|on`, default `off`. New audit fields per cycle: `situation_key`
(also fixes the measurement gap above), `cache` ∈ `hit | miss |
rejected_stale | refused | off`. The metrics exporter derives hit rate and
cost saved (`hit × mean prompt_tokens × price`). Without these fields the
cache is not allowed to ship.

### 6. Classes served and refused

Served only when the state says the answer is obvious:

| Served | Condition |
|---|---|
| accept / complete / turn in quest | a quest window is open on that giver |
| loot | a lootable corpse within interact range, no threats |
| accept group invite | pending invite |
| close window | window open and no offered action in it |
| rest (`idle`) | no threats, `hp` or `mana` in `crit`/`low` |
| stop following | follow active and leader gone |

Refused (always asks Jev): which mob to pull (`auto_attack`/`move_towards` on
non-threats), whether to flee or fight when `combat >= 1`, all movement
targets, gossip choices, reward-item choice with more than one option, spells,
anything with more than ~4 options of different actions, and any cycle whose
last action failed. `refused()` in the tool encodes this list.

*Note on "heal when low":* the action set has no heal or rest-heal action
(`rest` is `idle` here; #165's reflexes cover emergencies), so the owner's
example maps to the `idle` row. If a heal spell becomes a candidate it joins
the served list under the same condition.

### 7. Storage

SQLite in the agent's data volume, `/data/jev-cache/<agent>.sqlite`, stdlib
`sqlite3`, one table `(key PRIMARY KEY, option, confidence, hits, fail_streak,
created_ts)`, capped at 512 rows (evict least-recently-hit). The audit dir
(`/data/audit`) already sits on that volume; wowmap's activity feed is the
precedent for SQLite here.

### 8. Numbers

Run on a real audit directory (needs the VM; `snapshot` records only, i.e. 1
cycle in 20 until `situation_key` is logged every cycle):

    python3 -m agent.tools.cache_probe /opt/wow-server-metrics/audit/<agent> --json

It reports `repeat_rate` (cycles whose key occurred earlier in the run),
`agreement` (of those, how often the earlier choice equals the later one),
`refused_rate`, and `by_action`.

**Measured so far: nothing real.** The only recorded audit file in the repo is
the 5-line synthetic `agent/tests/fixtures/audit/luaprata_sample.jsonl` (one
snapshot, no repeats), and the A/B harness the issue mentions does not exist
in the repo. The live VM was not reachable non-interactively from the
authoring session (SSH host key unverified), so no live audit logs were read.
The unit tests exercise the arithmetic only. No claim about the real repeat
rate is made here.

## Recommendation

**Do not build the cache now.** Phase 1 costs well under $5/month at 1-5
agents; a cache adds state, a staleness failure mode, and a place for wrong
behaviour to persist, to save cents. Re-evaluate with the numbers from the
command above. Build only if, on at least ~2,000 keyed cycles across ≥2
agents, **all** hold:

- repeat rate of *served-class* keys ≥ 30% of cycles, and
- agreement on those repeats ≥ 95%, and
- spend or latency is actually a problem (monthly cost over the $5 ceiling in
  ADR 0001, or the 25-agent roster, UM-102/UM-63, makes it one).

Below that, the honest outcome is "not worth it yet". If it passes, file one
implementation ticket with sections 1-7 as its acceptance criteria, plus the
`situation_key`/`cache` audit fields, shipped `JEV_CACHE=off`.

## Consequences

- No behaviour change today. Adds `agent/tools/cache_probe.py` (measurement
  only) and its tests.
- Recommended small follow-up regardless of the verdict: log `situation_key`
  on every audit record so the numbers stop being sampled.
- Follow-up implementation issue: **not filed**, because the measurements do
  not yet support it.
