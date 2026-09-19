# Agent run: level 1→N unattended (UM-55)

Evaluation runbook for the Phase 3 exit criterion: **one agent levels a
fresh character from 1 to a target level, fully unattended (zero human
intervention).**

The target level is **10** per the original exit criterion, but the same
run works at a lower target (e.g. **3** or **5**) as a cheaper, faster first
pass — fewer zones/quest chains to cover, less wall-clock time and token
spend, while still proving the same thing: the agent perceives, decides and
acts entirely on its own for the whole run. Record the target level you used
for each attempt in the results log; don't silently change it mid-run.

This doc only covers the run itself. It does not include a live run's
results yet — see "Results log" at the bottom, filled in after each attempt.

## Test character

Use a fresh character, not Luaprata (the existing dev character — keep it
for manual poking around). Create it via `agent.session.WoWSession.
create_character()` (CMSG_CHAR_CREATE, added for this issue) — there is no
GM command fallback: TrinityCore 3.3.5a's `.character` subcommands
(`customize`, `changefaction`, `changerace`, `changeaccount`, `deleted`,
`erase`, `level`, `rename`, `reputation`, `titles`) don't include `create`,
and it isn't in `docs/GM-COMMANDS.md` either. If the protocol path is broken,
create the character the normal way instead: log a real client into
character select and create it there.

Record here once created:

| Field | Value |
|---|---|
| Account | |
| Character name | |
| Race / Class | |
| Start map / position | |
| Created (date, method) | |

## Run config

1. Pick one agent profile in `docker-compose.agents.yml`, point its
   `WOW_ACCOUNT` / `WOW_CHARACTER` at the fresh character above.
2. Set `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` in
   `.env` — record the exact model name in the results log, settings don't
   transfer between runs.
3. Set `AGENT_MAX_TOKENS_PER_HOUR` to a real budget (not `0`/unlimited).
4. Confirm the audit log volume (`/opt/wow-server-metrics/audit`, UM-51) is
   mounted — it's the only record of what the agent actually decided.
5. **Ask the human before starting.** A 1→10 run is hours of wall-clock time
   and real token spend. This doc does not authorize starting one; UM-55
   itself gates that decision.

## How to start a run

```sh
docker compose -f docker-compose.agents.yml up -d agent-<name>
```

Note the start time (wall clock) in the results log the moment the
container comes up.

## How to watch a run

All of these already exist; no new tooling needed for this issue.

- **`tools/wowmap`** (`:9400`) — live position/trail for the character.
- **Grafana** — "agent behaviour" and "movement trails" dashboards: level,
  zone, playtime over the run.
- **`wow-exporter` metrics** — `level`, `wow_character_quests_completed_total`.
- **Audit log replay** — tail failures live or review after the fact:
  ```sh
  python3 -m agent.tools.replay /opt/wow-server-metrics/audit/<agent>-<date>.jsonl --failures
  ```

## How to stop a run

```sh
docker compose -f docker-compose.agents.yml stop agent-<name>
```

Record the stop time and reason (success / failure / manual pause) in the
results log. Pausing to look at something without touching the character
is fine; anything that touches the character or its session counts as an
intervention (see below) and ends the run as a failure.

## Definition of "human intervention"

Any of the following, during a counted run, means the run failed the moment
it happened — write it down and stop, don't keep going with a compromised
run:

- Any GM command run against the test character or its surroundings
  (`.character`, `.tele`, `.additem`, `.revive`, `.aoe *`, etc.) — see
  `docs/GM-COMMANDS.md` for the full command surface.
- Manually restarting the agent container, or the world server, because the
  agent got stuck.
- Manually editing the character's state in the database.
- Manually sending any packet or chat command on the agent's behalf.

Watching dashboards, tailing logs, or replaying the audit log with
`agent.tools.replay` is **not** an intervention — read-only observation
doesn't touch the character.

## Success criteria

A run counts as successful only if **all** of:

- [ ] Character reaches the target level chosen for this attempt.
- [ ] Zero human interventions (see above) for the entire run.
- [ ] Wall-clock time recorded (start → target level).
- [ ] Token usage and $ cost recorded (from the LLM provider's own
      accounting, not an estimate).
- [ ] Deaths, stuck events (no progress for N minutes), and invalid-LLM-call
      rate recorded from the audit log.

**"Fully autonomous" is more than just reaching the level.** Skimming the
audit log (`agent.tools.replay`) for the whole run should also show:

- [ ] Every cycle has a `tool_call` the agent chose itself — no cycles where
      the LLM call failed and the reflex/idle path carried the run
      (`valid: false` or `tool_call.name: null` should be rare, not the norm;
      see `docs/AI-AGENT-SPEC.md`'s >= 90% valid-tool-call bar from UM-61).
- [ ] `goal`/`persona` and the tool calls made are plausibly connected to
      quest/XP progress — not looping the same no-op action for many cycles
      in a row (goal drift).
- [ ] No cycle required a hardcoded reflex (follow-leader) to make progress;
      this run is one agent alone, reflexes should be off or idle.

## Failure catalog

One row per failed run. `Cycle range` is the audit log's cycle numbers
(`agent.tools.replay --from <cycle>` to jump straight there). `Fix card` is
the Linear issue filed once the root cause is understood — file it in the
Wow Server project, label `agent`, milestone M3, with the audit-log evidence
attached.

| Symptom | Cycle range | Root cause | Fix card |
|---|---|---|---|
| _(none logged yet)_ | | | |

Expected categories (fill in as they occur, don't force a fit):

- **Perception gaps** — agent can't see something it needs to (an item, an
  NPC, a corpse) because `agent/perception.py` doesn't surface it.
- **Movement stuck** — pathing fails against terrain (evidence toward
  UM-49's navmesh/pathfinding sidecar).
- **Quest logic** — wrong NPC targeted, objective missed or misread.
- **Combat** — pulls too many mobs, doesn't use spells, bad target
  selection.
- **Resource** — no mana/rage regen handling, runs dry mid-fight.
- **LLM** — looping on the same action, invalid tool calls, goal drift
  (wandering off-quest with no recovery).

## Prompt version comparison

Each iteration of the system prompt gets its own file under
`agent/brain/prompts/` (`v1.md`, `v2.md`, …; that directory doesn't exist
yet — create it with the first version tried). Compare versions here, not
just the latest:

| Version | XP/hour | Intervention-free duration | Notes |
|---|---|---|---|
| _(none run yet)_ | | | |

## Results log

One entry per attempt, successful or not.

### Attempt 1 — _(date)_

- Character: _(name, race/class)_
- Target level: _(e.g. 5)_
- Model: _(provider/model, settings)_
- Start: _(timestamp)_ · Stop: _(timestamp)_ · Outcome: _(success/failure)_
- Token usage / cost: _(from provider dashboard)_
- Deaths: _ · Stuck events: _ · Invalid-call rate: _
- Failure catalog rows added: _(links)_
