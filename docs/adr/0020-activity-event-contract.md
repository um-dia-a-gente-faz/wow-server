# ADR 0020: One activity-event contract, emitted by the agent

## Status

Accepted (owner's delegate, 2026-10-16, on #390).

History: Proposed (#754, the ADR part of #390). The coordinator's decision of 2026-10-16
accepted it with its defaults, and the first slice (`level`) is built in the PR that carries
this status. #390 stays open for the further slices. Every statement about code below was read on `origin/main` at commit `ffccd083fc52`
(`git rev-parse origin/main` when this was written). There is no docker and no route to the
realm where it was written, so nothing here is a claim about live data: counts and shapes
come from the code and from the frozen fixtures, each count from the command quoted next to it.

## Context

The per-character activity feed (`GET /api/character/<name>/activity`) is built in
`tools/wowmap/wowmap/activity.py` (`wc -l`: 628 lines) from three sources. All three write
into one SQLite file through `ActivityStore.add`, which keeps the newest `KEEP_PER_CHARACTER`
(500) events per character and ignores a second event with a `uid` it already holds
(`INSERT OR IGNORE` on a `UNIQUE` column).

- **`db`**: `DbPoller.poll_once` reads every online character (`read_snapshot`, three queries
  in one read-only snapshot) every 5 s and `diff_snapshots` turns two snapshots into events.
  uid `db:<guid>:<t:.3f>:<kind>:<key>`.
- **`chat`**: `ChatFollower` reads chat-feed's SSE stream (`/api/chat/stream`) and
  `chat_event` keeps `say`, `yell` and `channel` (`PUBLIC_CHAT_KINDS`). uid `chat:<id>`, the
  SSE id. `chat_event` drops a sender who is an agent (`skip_senders`), because "their audit
  log already has it".
- **`audit`**: `AuditTailer.scan_once` reads the pulled `<agent>/<day>.jsonl[.N]` files from the
  byte offset it last saw and `audit_event` turns each decision into one event. uid
  `audit:<agent>:<cycle>:<ts>`.

`Activity.feed` marks a character as an agent when its name is a directory of the audit
directory (`Activity.agent_names`, from `AuditTailer.agents`). Nothing else tells the two
kinds of character apart.

### Inventory (read from `main`, not from #390)

Commands behind the counts: `grep -o 'emit(guid, "[a-z_]*"' tools/wowmap/wowmap/activity.py |
sort | uniq -c` (13 call sites, 10 kinds, all inside `diff_snapshots`); `len(activity.ACTION_KINDS)`
(39 action names mapped onto 13 kinds, every other action is kind `action`);
`grep -rn '\.record_event("' agent/handlers | wc -l` (40 packet events with a literal kind).

"Agent knows" means the agent process holds the fact exactly, at the handler or action named.
"Human" is what happens for a character that runs no agent: the owner's character is the main
one, and no contract emitted by agents can cover it.

| Kind | Source today | `inferred` | uid form | Agent knows it exactly? | For a human |
|---|---|---|---|---|---|
| `session` (Logged in, Logged out) | `db`, `diff_snapshots` | no | `db:<guid>:<t>:session:` | Partly: the connection state machine writes every transition to `<agent>/lifecycle/<day>.jsonl` (`build_lifecycle` in `agent/root.py`, `AuditLogger.record_lifecycle`). That file is not read by `AuditTailer` and a transition is not the same word as "logged in" | `db` only |
| `level` | `db` | no | `db:<guid>:<t>:level:` | Yes: `handle_levelup_info` in `agent/handlers/spells.py`, event `levelup`, field `level` (`parse_levelup_info`, `SMSG_LEVELUP_INFO`) | `db` only |
| `zone` | `db` | no | `db:<guid>:<t>:zone:` | No handler found: `grep -n zone agent/handlers/*.py` prints nothing | `db` only |
| `quest`, "Turned in quest" | `db` | no | `db:<guid>:<t>:quest:<quest id>` | Yes: `handle_questgiver_quest_complete` in `agent/handlers/quests.py`, event `quest_turned_in`, fields `quest_id`, `xp_reward`, `money_reward` | `db` only |
| `quest`, "Handed in <item>" | `db` | **yes** | `db:<guid>:<t>:quest:-<item>` | Not as an item event: the turn-in is known, which items it took is not carried by the packet | `db` only |
| `item`, "Got <item>" | `db` | no | `db:<guid>:<t>:item:+<item>` | Yes: `handle_item_push_result` in `agent/handlers/loot.py`, event `item_received`: `entry`, `count`, `received`, `created`, `player_guid` | `db` only |
| `item_lost`, "Used or destroyed" | `db` | **yes** | `db:<guid>:<t>:item_lost:-<item>` | Only a destroy it asked for (`destroy_item` action). A used or consumed item has no handler | `db` only |
| `sell` | `db` | **yes** | `db:<guid>:<t>:sell:-<item>` | Partly: `SellItemAction.execute` in `agent/actions/vendor.py` returns ok when no `sell_failed` arrives; the server sends `SMSG_SELL_ITEM` only on failure (`parse_sell_item` in `agent/npc.py`). The price is the difference of `session.coinage` (`PLAYER_FIELD_COINAGE`) | `db` only |
| `buy` | `db` | **yes** | `db:<guid>:<t>:buy:+<item>` | Partly, the same way: `BuyItemAction`, no `SMSG_BUY_FAILED`; `SMSG_BUY_ITEM` is a broadcast of the vendor's stock, not an acknowledgement (`handle_buy_item` in `agent/handlers/npc.py`) | `db` only |
| `trade`, "Gave", "Received" | `db` | **yes** | `db:<guid>:<t>:trade:-<item>` or `+<item>` | The agent sees its own trade steps (`handle_trade_status` in `agent/handlers/trade.py`, event `trade_status`). Which items changed hands at completion was not checked for this ADR | `db` only, and only when both traders are online and the same item and count leave one and reach the other in one poll |
| `money` | `db` | no | `db:<guid>:<t>:money:` | Yes: the total in `session.coinage`, and the cause for looted money (`handle_loot_money_notify`, event `loot_money`, `money`) and quest money (`money_reward`) | `db` only |
| `chat` (say, yell, channel) | `chat`, from chat-feed | no | `chat:<id>`; the id is `<inode>:<offset>` for a `Server.log` line and `relay:<n>` for an event an agent POSTed | Yes, and this path is already agent-emitted: `handle_messagechat` in `agent/handlers/chat.py` feeds `ChatRelay` (`agent/chat_relay.py`), which POSTs to chat-feed's `/api/chat/ingest` | Through the `Server.log` path if it carries chat, or through the relay if an agent is in earshot. Which of the two is live is not verified here: `docs/AGENT-DIRECTION.md` says the log is empty, `docker-compose.yml` and the chat-feed README say it was verified live |
| `combat`, `loot`, `sell`, `buy`, `trade`, `chat`, `whisper`, `quest`, `item`, `item_lost`, `mail`, `move`, `group`, `action` (13 mapped kinds plus the default) | `audit`, `audit_event` | no | `audit:<agent>:<cycle>:<ts>` | These are the agent's own decisions: the action name, its arguments and `result.ok`. They record an intent and whether the action returned ok, not what happened in the game (a `loot` line does not say what dropped) | None |

Kinds that no source produces today but the agent knows: `kill` (`handle_party_kill_log`,
`party_kill`: killer and victim guid; `handle_log_xp_gain`, `xp_gain`, `is_kill`), `death`
(`check_death_transition` in `agent/handlers/death.py`; its `killer_guid` is itself a guess,
`infer_killer_guid`), `quest_progress`. For a human none of these exists in the feed, today or
after this ADR.

What stays reconstructed, for a human and for the owner's character: every row marked "`db` only"
above. The DB poller stays for them, unchanged, and its inferred events stay flagged.

### What #390 says that is no longer true, or never was

- **"`tools/wowmap/` `activity.py` (639 lines)."** The file is `tools/wowmap/wowmap/activity.py`
  and has 628 lines. `activity_schema.py` (40 lines) holds the schema and `migrate`.
- **"`diff_snapshots` infers loot, money, kills."** It emits none of loot or kill. Its ten kinds
  are the ones in the table; `money` is a fact (`dm = cur["money"] - prev["money"]`), not inferred,
  and the inferred ones are `trade`, `sell`, `buy`, `item_lost` and the quest hand-in. Loot and
  combat exist in the feed only as agent decisions (`audit`).
- **"`ChatFollower` ... which itself regex-parses `Server.log` (`SAY_RE`, ...)."** The parser
  exists (`parse_chat_line` in `tools/chat-feed/app.py`) and both paths are wired and started: the
  `tail_log` thread reads `Server.log`, and `/api/chat/ingest` takes the agents' relay. The repo
  contradicts itself on which one carries chat: `docker-compose.yml` (the `TC_WORLD__Logger__chat__log`
  line, "Verified on the live image") and `tools/chat-feed/README.md` say the log path was verified
  live, and the frozen fixture's `chat_last_id` is a log-form id; `docs/AGENT-DIRECTION.md` (known
  findings) and `agent/chat_relay.py` say the log is empty. **Which one is live is not verified
  here.** What is certain is that the relay is agent-emitted code, so chat is the one kind that
  can already be agent-emitted.
- **"`AuditTailer`: tails agent audit files."** True, and it reads decision lines only: it lists
  day files by `_DAY_FILE_RE` in `<base>/<agent>/`, so `lifecycle/` is not seen.
- **"Define the shape in `agent/api_schema.json` next to the HTTP API."** That file describes the
  responses of `agent/http_api.py` (ADR 0010: `api_version`, `endpoints`, `errors`). The audit
  line is a different artefact with a different rule (ADR 0018: a versioned record log, additive
  only), and nothing in `api_schema.json` mentions it. The event shape belongs next to
  `AuditRecord` in `agent/audit_schema.py`.
- **"The shape is (kind, character, t, text, detail, source)."** Of these the agent should send
  `kind`, `t` and `detail`. The character is the audit directory, which `audit_event` already
  uses as the character name. `source` is how wowmap got it. `text` is prose; keeping prose in one
  place (wowmap) means the two sources of one fact render the same sentence.
- **"HTTP pull with cursor, or the runner's SSE."** Weighed below. The pull that already exists
  is the audit directory (ADR 0002 D4).
- **"Reuse the typed audit record work in #277."** Done and closed: `agent/audit_schema.py`
  (`SCHEMA_VERSION = 2`). ADR 0018 then settled that the log changes by addition only.
- **"chat-feed: golden-line tests and a staleness metric."** Out of this ADR. `tools/chat-feed/`
  is a game-stack path (`stacks_for`), and which path is live is not verified (above). It is a
  separate issue if anyone wants it.

### The transport that exists

- The agent host owns the audit directory; the wow-server VM pulls it every 30 s
  (`scripts/wow-agents/pull-audit.sh`: `rsync -a`, read-only key, never `--delete`, mirror pruned
  by `find "$dest" -type f -name '*.jsonl*' -mtime +N`). ADR 0002 D4.
- A line has `schema` (`SCHEMA_VERSION = 2`, `agent/audit_schema.py`). `AuditRecord.to_json` writes
  every field it knows plus the unknown keys it kept in `extra`. `iter_records` in `agent/metrics.py`
  keeps the stored `schema` and offers an `on_newer` callback for lines newer than it knows; no
  caller outside the tests passes it (`grep -rn on_newer --include=*.py . | grep -v /tests/` shows
  only `agent/metrics.py`), so nothing counts newer lines today. The readers and what each does with
  an `events` key: `iter_records` yields the dict whole; `load_records` (`tools/agent_dev/replay.py`)
  and `tools/agent_dev/capture_scenario.py` go through `AuditRecord.from_json`, which keeps it in
  `extra`; three readers parse the line themselves with `.get` and ignore it: `audit_event`,
  `Fleet._last_record` and `tools/fleet-limits/fleet_limits.py` (`_read_file`, `hour_sums`).
- Every line in a day file is read as one think cycle by `iter_records` users and by
  `AuditTailer`; that is why the lifecycle lines live in a subdirectory
  (`AuditLogger.record_lifecycle` says so). A new record kind in the day file would be counted as a
  cycle by the metrics.
- Events happen on the receive thread (`ctx.state.record_event` in the handlers, a bounded deque
  with a monotonic `t`, `agent/state.py`). The audit line is written by the think thread once per
  cycle (`record` in `agent/think.py`, `AuditLogger.record`). The two meet nowhere today.
- An agent's HTTP API already serves `GET /events` as SSE (`agent/http_api.py`,
  `_stream_events`), `DECISIONS_MAXLEN = 50` decisions in memory, and wowmap knows each agent's
  URL (`AGENT_API_URLS`, `tools/wowmap/wowmap/agents.py`). It is a live view, gone with the process.

### Deploy facts (`stacks_for` in `scripts/deploy.sh`, read only)

`docker-compose.yml`, `tdb/*` and `tools/chat-feed/*` recreate the **game** stack (players
disconnect; the deploy defers while anyone is online). `tools/common/*` both. `monitoring/*`,
`exporters/*`, `tools/wowmap/*` and `tools/dbc/*` the **monitoring** stack, which does not
disconnect players. `agent/*` is in neither list: merging it deploys nothing. It reaches the
agents when someone runs `scripts/wow-agents/bootstrap-guest.sh` on `wow-agents` and recreates
the containers (ADR 0018, Context). So a change to both `agent/` and `tools/wowmap/` takes
effect in two steps, in that order of merge but the other order of use: wowmap deploys by itself
within five minutes, the agent change waits for a manual image roll.

## Options considered

| Option | Cost | What it breaks | How it is undone |
|---|---|---|---|
| **A. Do nothing** | Nothing. | A level, a quest, a loot and a sale stay three guesses per fact in the DB path, and an agent that levels up shows once as an inferred-or-not DB row and once as a decision. Console v4 builds on that. | Nothing to undo. |
| **B. A field on the decision line** (`events`, a list), written by the think thread from a bounded buffer the receive thread fills (chosen) | A buffer and a drain in the agent, one more key in `AuditRecord`, `SCHEMA_VERSION` 3, a golden line, one loop in `scan_once`. No new file, no new writer, no new connection. | Nothing for readers: older code ignores the key (the additive rule). Events reach the feed with the cycle's lag, and a crash loses the undrained buffer. | Turn the setting off, or revert; lines already written keep an `events` key that every reader ignores. |
| **C. A new record kind in its own file**, `<agent>/events/<day>.jsonl`, next to `lifecycle/` | A new writer (`AuditLogger.record_event`), a row in ADR 0018's table and in `INVENTORY` of `scripts/tests/test_persisted_state_inventory.py` (a `scripts/` path), `clean_retention` extended to the directory, a writer that does not block the receive thread, a reader in `AuditTailer`. The same 30 s pull carries it (`rsync -a` of the tree; `find -name '*.jsonl*'` prunes it). Exact event time, no dependence on the think cycle. | Nothing: `_files` and `agent_names` ignore a subdirectory, as they do for `lifecycle/`. | Remove the writer; the files age out with retention. |
| **D. HTTP pull with a cursor** from each agent (`GET /events?after=`), wowmap polls 25 agents | A new endpoint, an `api_version` rule (ADR 0010), a cursor per agent persisted in wowmap, a poller. | History is lost for any interval wowmap or the agent is down, and for an agent that restarts (the buffer is in memory). The file is the history; the endpoint is not. A new request path to 25 containers. | Remove the poller; the endpoint can stay. |
| **E. Push**: generalise `ChatRelay` into an activity relay to an ingest endpoint | A new inbound writer on wowmap with a token, or on chat-feed (a game-stack path: owner's go-ahead, recreates the game stack). A queue that drops when full, as `ChatRelay` does. | A dead sink loses events silently (the relay counts them); a new credential in 25 containers. | Unset the URL. |
| **F. Keep inferring, harden the three paths** (golden lines for the chat parser, a staleness metric, tighter diffs) | Small, no contract. | Nothing, and nothing is fixed: the agent still knows more than the DB shows (no `item_received` cause, no `kill`), and the three paths stay coupled to the DB schema, the log format and the audit layout. | Nothing to undo. |

## Decision

**B. The agent emits activity events as a list on the decision line it already writes. wowmap
reads that list in the pass in which it already reads the line. The DB poller stays, for every
character, and an agent's event replaces the DB event for the same fact.**

1. **The line.** `AuditRecord` gains `events: list`, written only when it is not empty (as
   `snapshot` is), and `SCHEMA_VERSION` becomes 3 ("3: events"). Each element:

   ```json
   {"kind": "level", "t": 1760000000.123, "detail": {"to": 12}}
   ```

   `kind` is a string, `t` is wall-clock epoch seconds (`time.time()` at the handler; the
   receive thread's `t` is monotonic and not usable on the VM), `detail` is an object.
   There is no `character` (the directory of the file), no `source` (wowmap sets `agent`), no
   `text` (wowmap renders it with the sentence the DB path uses today: `Reached level 12`), no
   `inferred` (an agent event is never inferred; a kind the agent cannot know exactly does not
   enter the contract).
2. **Where the contract lives.** In `agent/audit_schema.py`, next to `AuditRecord`: a table of
   kinds and their required `detail` keys, with a golden line under `agent/tests/fixtures/audit/`
   (`v3.jsonl`), as ADR 0018 asks for every version. Not in `api_schema.json`: that file is the
   HTTP contract (ADR 0010) and its tests validate HTTP bodies. `tools/wowmap` cannot import
   `agent/` (its image is built from `tools/`, ADR 0014); the wowmap test reads the same golden
   line by relative path from the repo root, as ADR 0010 does for `api_schema.json`: CI runs from a
   checkout where both exist, and the image never needs the file.
3. **Versioning, the additive rule of ADR 0018.** A kind is never renamed and its `detail` keys
   never change meaning or type; a new fact is a new key or a new kind. Neither needs a new
   `schema`; only the `events` key itself did.
   - *Older wowmap, newer line*: `audit_event` uses `.get`, so it ignores `events` and `schema`.
     The feed shows the decision and the DB rows, as today. Nothing breaks, nothing is gained.
   - *Newer wowmap, older line*: no `events` key, nothing to read.
   - *Newer line, kind wowmap does not know*: skipped and counted (never raised); the rest of
     the line is processed.
   - *Known kind, `detail` of the wrong type*: that event is dropped and counted; the decision
     event of the same line is still stored.
   - *Older agent code, newer line* (after a rollback): `AuditRecord.from_json` puts `events`
     in `extra` and `to_json` writes it back; `iter_records` would offer it to `on_newer`, which nobody passes, so it is not counted.
     An agent that rolled back appends lines without `events` to the same file: legal, the version
     belongs to the line.
   - *Order of deploy*: ADR 0018's "guard before bump" holds only in part (the
     reader that can count newer lines, #635, is on `main`, but no caller uses it); ADR 0010's "consumers first" applies: wowmap merges and deploys first.
4. **Identity and idempotence.** The uid is derived from the event and the character, never from a
   counter: `agent:<agent>:<kind>:<t:.3f>:<key>`, where `<key>` is the kind's natural key
   (`level`: the `to` value). Reading the same line twice, after a wowmap restart (when
   `AuditTailer._since` is rebuilt from `latest_t`), or after a rotation, adds nothing.
5. **Loss, stated.** The buffer is a bounded deque (20), oldest dropped, and the drain happens in
   `think.record` before `AuditLogger.record`. Events are lost in these cases, and for `level` the
   feed then shows the `db` row (about 10 s after the fact: `PlayerSaveInterval=5000` and the 5 s
   poll) and never an `agent` row:
   - the write raises `OSError` after the drain;
   - the process dies, or stops cleanly: `run_think_loop` (`agent/runloop.py`) returns without a
     last `record`. **There is no final flush in the first slice**; slice 5 decides again;
   - the session reconnects: the buffer is an attribute of the `WoWSession`, and `connect_and_login`
     builds a new one per login;
   - more than 20 events fall between two lines (the period is the larger of
     `AGENT_THINK_INTERVAL_S`, default 3.0, and the cycle, which includes the brain call and the
     action).

   This is a feed, not a ledger. For `kill` and `death` no `db` row exists behind it.
6. **Which kinds, in order.** Only facts the agent holds exactly, one per slice: `level`, `quest`
   (turn-in), `item` (from `item_received`), `money` (from `loot_money` and quest rewards), `kill`,
   `death`. Not in the contract: `session` (lifecycle has its own file and its own ADR 0015
   states), `zone` (no handler), `sell`, `buy`, `trade`, `item_lost` (the agent has only the
   absence of an error, or no handler): they stay as the DB path infers them, flagged, even for an
   agent. Chat stays on the relay path (see Consequences). The `audit` decision events stay as
   they are: they say what the agent decided, not what happened.
7. **De-duplication during the migration.** Both sources run for every character. A fact has a
   key; the store keeps one row per key, and the agent's row wins whatever arrives first:
   - an agent event is inserted, then every `source = 'db'` row of the same character, kind and
     key is deleted;
   - a `db` event whose key matches an `agent` row already stored is not inserted;
   - both under the lock `ActivityStore.add` already holds, so the two orders give one row.

   The key of `level` is `(character, "level", detail.to)`. A character reaches a level once, so no
   time window is needed. A DB event can arrive first by arithmetic alone: the poller runs every
   5 s, the agent's line waits for the end of a think cycle and for the 30 s pull. Matching is on
   the existing columns (`character`, `kind`, `source`, `detail_json`, compared in Python over at
   most 500 rows), so the first slice needs no schema change. A kind without a natural key (a
   `money` delta can repeat) needs a key column and a migration; that is slice 3's decision.
   When no agent event exists, nothing changes: a character without an agent, or with the setting
   off, is fed by the DB path exactly as today.
8. **Off by default.** A setting `AGENT_ACTIVITY_EVENTS` (bool, default off) switches the buffer and
   the key. Off, a line is byte-identical to today's except `schema`.

Why the others lose: A leaves the three-way guess. C is the right shape if the delay of B ever
matters, and it needs a new writer, an edit to a `scripts/` test and a retention change for
timestamps B already has; it is the escape, not the start. D loses history by design and adds a
request path where the existing one is a file. E adds a credential and an inbound port, and the
chat-feed variant recreates the game stack. F fixes none of what #390 complains about.

## Consequences

- An agent's level, and later its quests and items, show once, exact, as `agent`-source rows with
  `inferred` false, and the DB rows for the same facts disappear. The feed stops showing the same
  level-up twice with two `uid` forms.
- Delay: an event reaches the feed after the next think cycle, up to 5 s for the tailer and up to
  30 s for the pull, so about 38 s plus any cycle longer than the 3.0 s default of
  `AGENT_THINK_INTERVAL_S`. The `db` row shows first (about 10 s) and is replaced, so the feed is not
  slower than today. `on_record` also hands the line to `AgentObserver.record_decision`, so `events`
  appears in `GET /brain` decisions and the SSE stream.
- The audit line grows by one key when there is an event. `AuditLogger` rotates at 10 MiB
  (`DEFAULT_MAX_BYTES`); an event is under 100 bytes.
- The humans' feed is unchanged and stays inferred. That is the price of not asking the server
  for more than the DB.
- Chat is not moved. The relay deduplicates across hearers by `dedupe_key` in chat-feed
  (`publish_relay_event`), which the audit path could not do: ten hearers would write ten lines.
- `chat:relay:<n>` is unique only for the life of one chat-feed process: `_sequence` starts at 0
  in `ChatFeed.__init__` (`tools/chat-feed/app.py`) and `snapshot_after` replays the whole buffer when a
  `Last-Event-ID` is not found. After a chat-feed restart a new event can carry the uid of an old
  one and `INSERT OR IGNORE` drops it. Not fixed here (a game-stack path); see Open questions.
- A new source value, `agent`, reaches the UI. `SOURCES` in `tools/wowmap/wowmap/static/activity.js`
  falls back to the raw value, so it works unlabelled.
- A fixture under `tools/wowmap/tests/fixtures/activity_db/` is needed only when the stored schema
  changes (ADR 0018 slice 7, #739). Slice 1 adds rows, not columns; the existing fixtures keep
  opening and reading back (`test_activity_migration.py` runs unchanged and is a Done-when item).

## First slice (ready issue outline)

**Title:** `feat(agent,wowmap): the level-up as an activity event on the audit line, one kind end to end`

Off by default, so merging it changes nothing on the realm or on the fleet. Stacks: `tools/wowmap/*`
recreates the **monitoring** stack when it merges (players stay connected); `agent/*` and the two
compose-file lines recreate nothing, and take effect when the owner rolls the agent image on
`wow-agents` and sets `AGENT_ACTIVITY_EVENTS=1` for one agent. No game-stack path, no `scripts/`,
no `tools/chat-feed/`, no `tools/common/`, no `docker-compose.yml`.

**Files**

- `agent/audit_schema.py` (`events`, `SCHEMA_VERSION = 3`, the kind table with `level` and its
  required key `to`), `agent/audit.py` (`record` takes `events`), `agent/think.py` (`record` drains
  the buffer), `agent/state.py` (the bounded buffer, the flag and the two `GameState` methods),
  `agent/ports.py` (the two methods join the `EventLog` protocol that `think` types the session
  with), `agent/login.py` (`connect_and_login` sets the flag on the session from `cfg`, as it
  sets `sess.chat_relay`), `agent/handlers/spells.py` (`handle_levelup_info` calls
  `ctx.state.emit_activity`), `agent/config.py` (`AGENT_ACTIVITY_EVENTS`; with `compose=True`
  the config tests require `docker-compose.agents.yml` and `.env.example` to carry it, as the header
  of `config.py` says)
- `tools/wowmap/wowmap/activity.py` (`agent_events`, the loop in `AuditTailer.scan_once`, the
  rules in `ActivityStore.add`), `tools/wowmap/wowmap/static/activity.js` (the label)
- New: `v3.jsonl` in `agent/tests/fixtures/audit/`, `test_audit_events.py` in `agent/tests/`,
  `test_activity_agent_events.py` in `tools/wowmap/tests/`; edited: `agent/tests/test_audit_schema.py`
  (the writer contract test lists the new field), `tools/wowmap/tests/js/` (a new test of the label)
- `docs/ARCHITECTURE.md`, `docs/adr/0018-persisted-state-versioning.md` (the audit row: `schema` 3)

**Exported names**

- `agent/audit_schema.py`: `ACTIVITY_KINDS: dict[str, tuple[str, ...]]`, kind to required `detail`
  keys (`{"level": ("to",)}`); `AuditRecord.events: list`
- `agent/state.py`: `GameState.activity_events: bool = False` (an attribute, set by
  `agent/login.py`), `GameState.emit_activity(kind: str, **detail) -> None` and
  `GameState.drain_activity() -> list[dict]`. They are methods, not module functions: handlers reach
  them as `ctx.state.emit_activity` and `think` as `c.session.drain_activity()` (typed by
  `ports.EventLog`). No new `LAYERS` edge in `agent/tests/test_architecture.py` is needed:
  `state` stays `{changes, perception}` and does not read the config (the caller sets the flag),
  `handlers` do not import `state`, `login` already imports `config` and `session`, `think` already
  imports `ports`
- `tools/wowmap/wowmap/activity.py`: `agent_events(rec: dict, agent: str) -> list[dict]` (feed
  events, never raises), `fact_key(e: dict) -> tuple | None`

**Behaviour**

1. `emit_activity` does nothing while `activity_events` is false. When true, it appends
   `{"kind", "t": time.time(), "detail"}` to a deque of at most 20 (the oldest falls off).
   `connect_and_login` sets `sess.activity_events = cfg.activity_events` before `login_character`.
2. `handle_levelup_info` calls `emit_activity("level", to=data["level"])` after `record_event`.
3. `drain_activity` is called on the think thread while the receive thread appends. It pops from
   the left until the deque is empty (`popleft` until `IndexError`); it never copies and then
   clears, which would lose an event appended between the two. `think.record` calls it and passes the list to `AuditLogger.record`; `to_json`
   writes `events` only when the list is not empty; `AuditRecord.from_json` reads it.
4. `agent_events` returns one feed event per element whose `kind` is in wowmap's table and whose
   required `detail` keys are present with the right type (`to` an int, not a bool). `character` is
   the directory name, `source` is `agent`, `inferred` false, text `Reached level <to>`, uid
   `agent:<agent>:level:<t:.3f>:<to>`, detail kept whole. Anything else is skipped and counted in
   `Activity.status()["agent"]`; `events` that is not a list is skipped.
5. `scan_once` calls `agent_events` for every record it reads, including those `audit_event`
   returns None for (an `idle` decision can carry an event), and stores both lists in one `add`.
6. `ActivityStore.add`, under its lock: for an event with a `fact_key`, a `db` event whose key
   matches a stored `agent` row is skipped; an `agent` event deletes the `db` rows with its key
   after inserting. `fact_key` is `(character.lower(), "level", detail["to"])` for `level` and
   `None` for everything else. `added` counts inserted rows only.
7. Nothing else changes: the DB poller emits as today, the decision events are stored as today.

**Done when**

| Item | Test or command |
|---|---|
| With the setting off nothing is buffered and a recorded line has no `events` key | `python3 -m unittest agent.tests.test_audit_events -v` |
| A drain that runs while another thread appends loses and duplicates nothing (one thread appends numbered events while the other drains in a loop; the union of the drains is all of them, in order) | same |
| With it on, `SMSG_LEVELUP_INFO` produces one `{"kind": "level", "detail": {"to": N}}` on the next line, with a wall-clock `t`; the 21st event drops the oldest | same |
| The writer emits exactly the fields of `AuditRecord` and the golden line `v3.jsonl` round-trips; `v0`, `v1` and `v2` still load with `events` empty | `python3 -m unittest agent.tests.test_audit_schema -v` |
| The wowmap reader parses the same `v3.jsonl` line (read by relative path) into the `level` event described above | `python3 -m unittest discover -s tools/wowmap/tests -p 'test_activity_agent_events.py' -v` |
| Unknown kind, `to` as a string or a bool, `events` not a list, a non-dict element: no exception, skipped and counted, the decision event of the line still stored | same |
| Agent event first, then the DB event: one row, `source = agent`. DB event first, then the agent event: one row, `source = agent`. Another character, another level, another kind, an `item` DB event: untouched | same |
| Reading the same file twice, and after rebuilding `AuditTailer`, adds no row | same |
| The existing stores still open, migrate and read back (no schema change, no new fixture) | `python3 -m unittest discover -s tools/wowmap/tests -p 'test_activity_migration.py' -v` |
| The label for `agent` shows in the feed | `node --test tools/wowmap/tests/js/*.test.js` |
| The settings are in the compose file and `.env.example` | `python3 -m unittest agent.tests.test_config_schema -v` |
| `scripts/check.sh all` passes | `scripts/check.sh all` |

Human steps, left unticked in the PR: roll the agent image on `wow-agents`, set
`AGENT_ACTIVITY_EVENTS=1` for one agent, level it, and see one `agent` row and no `db` row for that
level in the feed. `SMSG_LEVELUP_INFO` goes to the leveller only: TrinityCore 3.3.5 at `92127cd`, `Player::GiveLevel`,
`src/server/game/Entities/Player/Player.cpp:2437`, `SendDirectMessage` at `:2468`.

**Further slices**, in dependency order, one issue each:

2. `quest` (turn-in) from `handle_questgiver_quest_complete`: key `quest_id`. Same files, no migration.
3. A key column for kinds without a natural key (`money`, `item`): migration step 2 in
   `activity_schema.py` (`ALTER TABLE events ADD COLUMN fact_key TEXT` and an index), `v2.sql`,
   `PINS[2]`, `ROWS` and `META` entries in `test_activity_migration.py`. Monitoring stack only.
4. `item` from `handle_item_push_result` (`player_guid` must be the agent's own) and `money` from
   `loot_money` and quest rewards; both need slice 3.
5. `kill` and `death`: new kinds in the feed, no DB equivalent, so no de-duplication. `death` carries
   `killer_guid` only as the agent's guess and names it so. Neither has a `db` row behind it, so
   the buffer size and a final flush on a clean stop are decided again here.
6. Turn the setting on for the whole fleet, an owner decision (open question 1).
7. When no `db` row has been superseded for N days for agent characters, stop the DB poller's covered
   kinds for characters in `Activity.agent_names`. Not before.
8. If the delay of B matters: move the list to its own file (option C), a new ADR 0018 row.

## Found outside this ADR's scope

- ADR 0018's inventory is stale in two rows: `AuditLogger.record_lifecycle` has a caller
  (`build_lifecycle`, `agent/root.py:129`), and the audit row does not list `tools/fleet-limits`
  (`fleet_limits.py`) as a reader of the audit files. Not edited here.
- `chat:relay:<n>` can collide after a chat-feed restart (Consequences). Tracked in its own issue.

## Open questions for the owner

1. **Who turns it on?** Default: one agent first, then the fleet by editing `.env` once slice 4 is live.
2. **Is a feed that loses a few events on a crash acceptable?** Default: yes. The DB path covers the
   same facts while the character stays. The alternative is option C.
3. **Key column or Python matching** for kinds with no natural key. Default: Python matching until
   slice 3, then a column, because the match gets slower than one query at 500 rows per character.
4. **Chat.** Default: stays on the relay. Moving it needs a cross-hearer dedupe the audit path
   lacks.
5. **`chat:relay:<n>` after a chat-feed restart.** Default: out of this ADR, tracked in its own issue
   (a game-stack path, owner's go-ahead) that makes the id unique across restarts.
6. **The text of an agent event.** Default: wowmap renders it from `kind` and `detail`, in the
   language it uses now. The alternative, carrying text in the line, makes the agent the owner of
   wording.

## Related

#390 (the problem, stays open), #754 (this ADR), #277 and #600 (audit record, ADR 0018), #739
(frozen fixtures), #635 (newer-line counting), #209, #323, #326, #329, ADR 0002 (the audit pull),
ADR 0009 (threads), ADR 0010 (HTTP contract), ADR 0014, ADR 0015 (lifecycle).
