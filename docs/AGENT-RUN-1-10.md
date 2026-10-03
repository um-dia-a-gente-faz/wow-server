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
2. Set `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` in
   `.env` (see Preflight below; `LLM_MODEL` is a pinned ordered list, `auto` last) — record the exact model name in the results log, settings don't
   transfer between runs.
3. Set `AGENT_MAX_TOKENS_PER_HOUR` to a real budget (not `0`/unlimited).
4. Confirm the audit log volume (`/opt/wow-server-metrics/audit`, UM-51) is
   mounted — it's the only record of what the agent actually decided.
5. **Ask the human before starting.** A 1→10 run is hours of wall-clock time
   and real token spend. This doc does not authorize starting one; UM-55
   itself gates that decision.

## Preflight: confirm the brain answers

Do this **before** every run, and again after any change to `LLM_BASE_URL`,
`LLM_API_KEY` or `LLM_MODEL`. An agent with a dead gateway still logs in and
perceives; it just never acts (#133: HTTP 503 `no_providers_configured`).
The gateway is the FreeLLMAPI router at `http://192.168.1.72:3001/v1`, and
the key must come from that instance.

Run on the VM (or any LAN host). Sourcing `.env` and the check below print no
key. It sends one tiny forced tool call, the same shape the agent sends
(`agent/llm.py`), against the first id of `LLM_MODEL`:

```sh
set -a; . /opt/wow-server/.env; set +a
curl -sS -m 60 "$LLM_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $LLM_API_KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"${LLM_MODEL%%,*}\",\"tool_choice\":\"required\",
       \"messages\":[{\"role\":\"user\",\"content\":\"Call the ping tool.\"}],
       \"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"ping\",
         \"description\":\"Reply to a health check\",
         \"parameters\":{\"type\":\"object\",\"properties\":{}}}}]}" \
| python3 -c 'import sys,json; r=json.load(sys.stdin); m=r["choices"][0]["message"]; print("model:", r.get("model")); print("tool_calls:", [t["function"]["name"] for t in m.get("tool_calls") or []] or "NONE")'
```

Pass only if it prints `tool_calls: ['ping']`. A traceback with `KeyError:
'choices'` means an error body (401 wrong key, 503 no provider key or no
tool-capable model enabled, 429 cooling down); drop the `| python3 ...` part
to read it. Repeat with each id in `LLM_MODEL` to see which ones work. A
single success does not prove the >= 90% bar; it only proves the gateway is
not the blocker.

After the agent has run a few cycles, the newest ones must show no 503/429
and a valid tool call:

```sh
python3 -m agent.tools.replay /opt/wow-server-metrics/audit/<agent>/<YYYY-MM-DD>.jsonl --failures
```

Working model id (a human fills this in after the preflight and one agent
cycle both pass, then commits it here and to `.env.example`):

| Field | Value |
|---|---|
| Model id(s) verified | _(not yet verified)_ |
| Router | `http://192.168.1.72:3001/v1` |
| Verified (date, by) | |

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
| Model returns a wrong GUID, so every GUID-taking action fails the same way (`interact`, `accept_quest`): `guid 0xf130003bae001ed0 is not currently perceived` | A2 cycles 2-19 (18 in a row); A3 cycles with `interact`/`accept_quest` fails (10 of 50) | The snapshot shows GUIDs as 20-digit integers (Erona = `17379391218345059262`); the model returned `17379391218345058000`, which is that number after a 64-bit float round-trip. Above 2^53 a JSON number can't survive tool-call arguments through the gateway (Gemini routes). Some `auto` cycles passed the exact value, probably when routed to a provider that keeps integers as text (the audit log records only `auto`, not the routed model, so this is unconfirmed). Blocking: it breaks every action that takes a GUID. | _(to file)_ |
| Agent re-does the same step forever: `accept_quest` 8 times and gossip `interact` 15 times for one quest | A3 cycles 4-41 | The prompt is stateless (`agent/llm.py` `build_messages`): no goal, no history, no result of the last action. The model can't know it already accepted the quest. The server ignored the repeats (one `character_queststatus` row, status 3). | _(to file)_ |
| Quest log in the snapshot is garbage: `title: "g\r"`, objective `entry: 393216, needed: 1966080`, `state: 0`, while the DB says the quest is status 3 (incomplete) | A3 (every snapshot after accepting quest 8325, first seen around cycle 10) | Live `SMSG_QUEST_QUERY_RESPONSE` / quest-log field parsing is misaligned (`393216 = 0x60000`, `1966080 = 0x1E0000` look like values shifted by two bytes). Unit tests pass, so either the fixtures don't match the live layout or the live parse takes a different path. Not yet root-caused. | _(to file)_ |
| Garbage tool arguments reach the game: `say` with message `}`, `follow` with names `}}dotspans` and `: ` | A3 cycles 42, 48 (`follow`), 49 (`say`) | No validation of free-text/name args before acting. A bare `}` was sent to public chat. | _(to file)_ |
| Free LLM tier can't sustain one agent: pinned `gemini-3.6-flash` got 502/429 within 10 s; pinned `gemini-3.1-flash-lite` got 429 after 19 calls (~4 min); `auto` ran the full 17.7 min | A1 cycles 2-7, A2 cycles 20-25 | Each cycle sends a 4-8k-token prompt (avg 6.5k) every 12-15 s (~340k prompt tokens in 17.7 min). One free route per pinned model has a low per-minute budget. `auto` spreads over routes, which is why it survives but also why GUID handling flips between providers. | _(to file)_ |
| "Chat in global" not possible | n/a | The agent has `say`/`yell`/`whisper`/`emote` only. There is no join-channel action, no channel send, and no channel handling in `send_chat_message`. | _(to file)_ |
| Follow reflex points the agent at the owner instead of the quest | A3 cycles 43-47 | The LLM chose `follow` Rubens (the only other player nearby) after its quest steps stalled. Not a bug in the reflex, but it shows the model has no objective to fall back on. | (same as goal/history row) |
| Audit log rows for failed LLM calls repeat the previous cycle's `model`, `latency_ms` and token counts | A1 cycles 2-7, A2 cycles 20-25 | Likely the client's `last_usage`/`last_latency_ms` are left over from the previous call when no call completed (not confirmed in code). Misleads cost analysis. | _(to file)_ |

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

### Attempt 1 (exploratory, not a counted run) — 2026-09-19

Three short starts, all on the same character, so the model and think interval could be changed after each failure. Restarting to change model counts as an intervention by the rules above, so this is **not** a counted run. It's a first live look at Milestone 2 with a target of level 5.

- Character: Spellweaver, Blood Elf Mage (AGENT05), Sunstrider Isle. Level 1, 0 XP, 0 copper, empty equipment and bags (agents own no starting gear).
- Target level: 5. **Reached: no. Final: level 1, 0 XP.**
- Other players online: Rubens (owner), within 5 yd of the agent in perception during the run.
- Interventions: no GM commands, no DB edits, no worldserver restart. The agent process was stopped and restarted three times to change model and interval.
- Run code: `main` at `df41e45` (includes UM-59, UM-60, UM-88 and #55).

| Attempt | Model / think interval | Length | Outcome |
|---|---|---|---|
| A1 | pinned `gemini-3.6-flash`, 8 s | ~1 min | 502, then 429 on every cycle after the first call. Stopped. |
| A2 | pinned `gemini-3.1-flash-lite`, 12 s | ~7 min | 18 identical failed `interact` calls (wrong GUID), then 429. Stopped. |
| A3 | `auto`, 15 s | 17.7 min, 50 cycles | Quest 8325 accepted for real, then repeated; no kill, no XP. Stopped. |

A3 numbers (from the audit log): 47 of 50 cycles produced a valid tool call (94%, above UM-44's 90% bar); 33 actions succeeded, 14 failed validation or execution, 3 LLM calls timed out. Successful actions: `interact` 15, `accept_quest` 8, `set_target` 2, `move_towards` 2, `follow` 2, `rest` 1, `move_to` 1, `say` 1, `face` 1. Failed: `interact` 6, `accept_quest` 4, `follow` 2, `auto_attack` 1 (target already dead), `loot` 1 (not lootable). No `cast_spell`, no `equip_item`, no `complete_quest`, no `turn_in_quest`. LLM latency median 3.5 s, max 19.3 s. Prompt tokens 337k, completion tokens 22.5k.

**What went well**

- Login, perception and packet handling on the live server were clean: 43-54 objects tracked, 0 dropped packets, and the agent logged out cleanly on interrupt every time.
- Quest acceptance works end to end. Erona's gossip opened, `accept_quest` for 8325 succeeded, and the DB has `character_queststatus` (quest 8325, status 3). The server ignored the repeated accepts.
- The UM-81 fix held: three LLM timeouts and many 429/502 responses were logged as skipped cycles, and the agent never crashed.
- Valid tool-call rate on `auto` was 94%. Actions that got a correct GUID mostly worked: `interact`, `set_target`, `move_towards`, `move_to`, `rest`, `follow`, `say`.
- The follow reflex works: the agent followed Rubens at 3 yd.
- The audit log recorded every cycle with prompt, tool call, result and snapshot, which is how the GUID and quest-log problems were diagnosed in minutes.

**What went badly** (details in the failure catalog above)

1. The GUID float-precision bug blocks every GUID action on Gemini routes.
2. The prompt has no goal, no history and no last-result feedback, so the agent loops.
3. The live quest log and quest text are garbage.
4. Garbage arguments (`say "}"`, nonsense player names) go straight to the game.
5. The free LLM tier runs out fast; pinning a model is worse than `auto` right now.
6. No path to global/channel chat.
7. Not exercised at all, so still unproven live: combat by the agent itself (`cast_spell`, `auto_attack` on a live mob), looting, equipping items, selling, turning in a quest, dying and recovering, and reaching level 2+. The agent never killed a mob: its one `auto_attack` (cycle 45) and `loot` (cycle 46) targeted a Mana Wyrm that was already dead.

**Suggested next steps, in order**

1. Stop showing 64-bit GUIDs to the model. Give each perceived object a short handle (`u1`, `p2`, ...) or pass GUIDs as strings, and map back in code.
2. Put a goal ("level up by doing quests and killing mobs near you"), the last N actions with their results, and the quest log with real titles into the prompt.
3. Add a code-level loop guard: after 2-3 identical failed or no-op calls, tell the model or apply a cooldown.
4. Fix the live quest-query parse using a captured `SMSG_QUEST_QUERY_RESPONSE` for quest 8325 as a fixture.
5. Validate tool-call arguments before acting (player names must match a nearby player; chat text must be sane).
6. Add a join-channel action and channel chat so "global" chat is possible.
7. Size the prompt down and pick a model list with enough rate limit for a 15 s think interval; add retry-after handling for 429.
8. Re-run this target-5 test after items 1-4. Start from a character that has never accepted the quest, and note that equipping can only be tested once the agent gets an item (quest reward or loot).

- Token usage / cost: free tier, no cost. See the token counts above.
- Deaths: 0 · Stuck events: 2 (A2 loop, A3 quest loop) · Invalid-call rate: 6% on `auto` (3 of 50)
- Failure catalog rows added: 8 (see the table above)
