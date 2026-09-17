# Roadmap

> **Linear is the tracker of record.** Issues live in the Linear team *Um Dia a Gente
> Faz* (`UM-*`), project *Wow Server*. This file holds background and design notes.
> When scope or status changes, update Linear first, so the two don't drift.
>
> **Agent direction** (goal, milestones, model and chat rules) is in
> `docs/AGENT-DIRECTION.md` and overrides the agent plan below where they differ.

## Shipped

The original observability roadmap (character inspect, movement trails,
agent-behaviour panel, live map calibration, chat feed spike) is done and
deployed to the live VM as of 2026-09-14:

| Item | PR | Notes |
|---|---|---|
| Player save interval tuning | #2 | 5 s saves for fresher position metrics |
| Agent behaviour dashboard | #3 | Grafana: online pop, playtime, zone, level |
| Character inspect API | #4 | `GET /api/character/<name>` on wowmap |
| Movement trails + density heatmap | #5 | Grafana dashboard + `wow_player_position_bucket` |
| Quest completion + chat feed spike | #6 | `wow_character_quests_completed_total`; `docs/CHAT_FEED_SPIKE.md` |
| Live map calibration mode | #7 | Per-zone pixel offset, persisted, operator UI |
| Chat feed SSE sidecar | #8 | `tools/chat-feed`, port 9500, bounded replay |
| Deploy tooling | — | `/opt/wow-server` is now a real git checkout; `scripts/deploy.sh` |

All 8 dashboards/services are live on 192.168.1.64 and 192.168.1.60. See
`docs/DEPLOYMENT.md` → "Updating" for the redeploy flow.

## Development plan

### Phase 1 — perception — SHIPPED 2026-09-16

Landed as UM-31 (fixtures + harness), UM-32 (block framing + movement),
UM-33 (VALUES field mapping), UM-34 (WorldState wiring + snapshot API) —
PRs #16-#19. See "Definition of done" below for what was and wasn't
live-verified, and `agent/tests/fixtures/update_object/README.md` for a
documented gap (no live `OUT_OF_RANGE_OBJECTS`/standalone `MOVEMENT` capture
— same-map `.go` GM teleports don't trigger TrinityCore's live visibility
resync without a `MSG_MOVE_TELEPORT_ACK` this agent doesn't send yet; a
relogin does, and that's what these PRs used to verify position changes and
nearby-object detection instead). A teleport ack is a natural follow-up once
real character movement (Phase 2, `move_to`) needs one anyway.

### Phase 1.5 — spline movement — SHIPPED 2026-09-17

UM-64: the `SPLINE_ENABLED` create-object block, `SMSG_MONSTER_MOVE`, and
`MSG_MOVE_*` broadcasts are now all parsed — a moving NPC or player no
longer drops the whole packet it's in, and nearby objects get a simple
straight-line position interpolation between their own update-object/
`MONSTER_MOVE` ticks instead of going stale.

Original plan (kept below for context; superseded by the actual PRs above):

This is the actual point of the repo (`docs/AI-AGENT-SPEC.md`) and the
highest-leverage next task — everything else is scaffolding around it. The
protocol client (`agent/`) already does SRP6 auth, world handshake, login,
keepalive, chat, and target actions end to end against the live server. The
one missing piece is **perception**: `agent/perception.py::WorldState`
currently only records that a GUID exists, with no position/health/name data,
and `agent/session.py::_parse_update_object()` deliberately stops after
reading each block's GUID.

Reading the current stub closely surfaces a few things the handoff doc doesn't
call out, all worth fixing as part of this work, not after:

- **Byte-offset desync risk.** `_parse_update_object` never consumes the
  bytes after a block's GUID (the values update for VALUES, and objectTypeId +
  movement update + values update for CREATE_OBJECT, per
  `docs/PROTOCOL-NOTES.md`), so `off` doesn't advance past them. With more than one block per packet
  (the common case once nearby entities exist), every block after the first
  gets parsed from the wrong offset. This has to be fixed *while* adding
  field parsing, not as a follow-up — a partially-correct version that reads
  the wrong number of bytes is worse than the current no-op stub.
- **`type 5` (NEAR_OBJECTS) block still falls through to `continue` with zero
  bytes consumed** — same desync risk. `type 4` (OUT_OF_RANGE_OBJECTS) is
  handled but wrong: the real format is `uint32 count` followed by `count`
  packed GUIDs (objects that left range), not one packed GUID guarded by a
  reused `mask` variable.
- **`SMSG_UPDATE_OBJECT` opcode is wrong.** `agent/session.py` defines it as
  `0x1F7`, which is `SMSG_PLAY_SPELL_IMPACT`. The correct value is `0x0A9`
  (`Opcodes.h`). The server sends small update packets (≤ 100 bytes)
  uncompressed, so those are currently missed.
- **`self.player_guid` is declared in `WoWSession.__init__` but never
  assigned.** `login_character(self, guid)` receives the GUID and never
  stores it. Perception needs to know its own GUID (to distinguish "my
  position updated" from "a nearby player moved") — set it there and call
  `world_state.set_my_guid(guid)` at the same point.

Steps, in order:

1. **Fix `player_guid` assignment** in `login_character()` — trivial, unblocks
   testing everything else against "is this update about me."
2. **Add a movement-block parser** — new function, e.g.
   `_parse_movement_block(data, off) -> (dict, new_off)`, handling
   `UPDATEFLAG_LIVING` (movement_flags, timestamp, x/y/z/orientation, fall
   time, speeds). The movement block has conditional sub-fields (on
   transport, swimming, spline movement in progress) that multiply the
   parsing surface — for a first pass, parse the common ground-movement case
   and raise a distinguishable exception (e.g. `UnhandledMovementFlags`) for
   flag combinations not yet handled, rather than guessing wrong and silently
   corrupting the offset.
3. **Wrap each block's parse in try/except in `_parse_update_object`** — on
   any parse error (including the new `UnhandledMovementFlags`), log it once
   (rate-limited — this will be noisy at first) and **abort parsing the rest
   of this packet**, not the connection. One bad block shouldn't crash the
   recv thread or corrupt state from prior packets. This is the safety net
   that makes iterating on step 2 and step 4 tolerable.
4. **Add the update-mask + values parser** — `uint8 mask_length` (word
   count), `mask_length * 4` bytes as little-endian `uint32` words, then one
   `uint32` per set bit, in bit order. Store as a raw `{field_index: value}`
   dict on the block for now — don't hand-map every field yet.
5. **Map the raw fields dict to `ObjectInfo`** for the field indices already
   listed in `docs/PROTOCOL-NOTES.md` (`OBJECT_FIELD_ENTRY`,
   `UNIT_FIELD_HEALTH`/`MAXHEALTH`/`LEVEL`/`FACTIONTEMPLATE`,
   `UNIT_NPC_FLAGS`, `PLAYER_FLAGS`). Extend `ObjectInfo.__slots__`
   (`agent/perception.py`) with `entry_id`, `max_health`, `faction`,
   `npc_flags`, and a `raw_fields` dict for anything not mapped yet — cheaper
   to keep the raw dict around than to re-add slots every time a new field
   turns out to matter.
6. **Wire position from the movement block**, not from fields — replace
   `WorldState.record_guid()` with an `update_object(guid, update_type,
   fields, movement)` method that creates-or-merges an `ObjectInfo` and sets
   `.position` when a movement block was present. Use the same method to
   update `session.player_position` when `guid == self.player_guid`.
7. **Fix the OUT_OF_RANGE block** (`uint32 count` + `count` packed GUIDs) and
   add `WorldState.remove_guid(guid)`, called for each.
8. **Fixture-based tests**, mirroring `tools/chat-feed/tests`' pattern: run
   `--dry-run` with `VERBOSE_PACKETS=1` once against the live server, capture
   a handful of raw `SMSG_UPDATE_OBJECT` payloads (hex-dump them behind a
   debug flag), and commit them as fixtures so the parser has a regression
   suite instead of only manual live verification.

**Definition of done** (from the handoff doc — unchanged):
1. `python3 -m agent --dry-run` logs "perception: N objects tracked" with N > 0
2. `session.player_position` updates when the character moves (teleport via
   `.go xyz` from the web UI console and confirm the change)
3. Nearby NPCs show up in the object list with entry IDs

**Effort:** medium. The hard crypto/protocol plumbing (auth, session
encryption, packet framing) is already done; this is careful, defensively-
written binary parsing against a documented but conditional wire format.

### Phase 2 — basic actions

Once perception works, `agent/actions.py` currently only has chat, party
invite, and target/attack-start. Build in this order — each item both depends
on perception and is a dependency for the next:

1. **`face` / `set_target`** — almost free once perception has nearby-object
   positions; needed before combat or interact can aim at anything.
2. **`interact` (gossip/vendor/quest window)** — `CMSG_GOSSIP_HELLO` /
   `CMSG_QUESTGIVER_*` opcodes against an NPC GUID from perception. No
   movement needed if already in range; a good next milestone because it
   proves the perceive→act loop closes without needing pathfinding yet.
3. **Combat basics** — `auto_attack`/`stop_attack` (mostly wired already via
   `actions.send_attack`), then `cast_spell` (needs the spellbook, which
   isn't parsed yet either — comes from `SMSG_INITIAL_SPELLS` at login,
   similar shape of work to update-object parsing but far smaller).
4. **`loot`** — `CMSG_LOOT`/`CMSG_LOOT_ITEM` once `unit_death` / a lootable
   flag is visible in perception (from `UNIT_DYNAMIC_FLAGS`).
5. **Movement (`move_to`) — the biggest open risk in Phase 2.** The server
   has mmaps built (`docs/DEPLOYMENT.md`), but nothing in this repo parses
   TrinityCore's binary `.mmap`/`.mmtile` navmesh format — that's effectively
   embedding a Recast/Detour navmesh query, a substantial standalone effort.
   Don't block basic actions on it:
   - **v1:** straight-line `CMSG_MOVE_START_FORWARD`/`CMSG_MOVE_STOP` toward
     a target position, good enough for short hops and combat positioning
     around a single mob, will walk into obstacles on anything longer.
   - **v2 (separate task, only once v1's ceiling is actually hit):**
     real navmesh-backed pathing — likely as a small sidecar service (Python
     bindings around Detour, or a thin wrapper shelling out to a Recast/
     Detour CLI) rather than a pure-Python reimplementation.
   - **UM-36 found and fixed a prerequisite for any of this:** the agent
     must send `CMSG_SET_ACTIVE_MOVER` for its own guid once after login, or
     every `MSG_MOVE_*` it sends (including v1's `START_FORWARD`/`STOP`) is
     silently dropped server-side — see `docs/PROTOCOL-NOTES.md`. Already
     wired into `agent/session.py::login_character`.

### Phase 3 — think loop + LLM integration

**UM-44 landed the first slice**: `agent/llm.py` (an OpenAI-compatible
`/chat/completions` client over stdlib `urllib`, no extra dependency) and
`agent/think.py::think_and_act()` (perception snapshot + `agent.actions.
catalog()` as tools → one LLM call → validate the returned tool call against
`agent.actions.REGISTRY` → execute exactly one action). `agent/__main__.py::
_run_loop` calls it every think cycle when `LLM_BASE_URL`/`LLM_MODEL` are
set; the agent idles (with a warning logged once) otherwise. Unit-tested
with a mocked LLM client (`agent/tests/test_think.py`) and a mocked HTTP
layer (`agent/tests/test_llm.py`) — not yet live-verified against a real
free-model endpoint.

Still open, in order:

1. **Live-verify against a real free model** (`docs/AGENT-DIRECTION.md`'s
   FreeLLMAPI/local-model constraint) — confirm tool-call reliability is
   anywhere near the ≥90% valid-tool-call bar that doc sets, not just that
   the client parses a well-formed mocked response.
2. **Model pinning + fallback** — `auto` routing gave ~50% valid tool calls
   in the earlier prototype; pin a short list of models known to be good at
   tool calls (UM-61 benchmarks candidates), and fail over to a local model
   server if the primary is down/rate-limited.
3. **Audit log** — persist every (perception snapshot, LLM decision, action,
   result) tuple, not just the current `log.info`/`log.warning` lines; this
   doubles as the spec's Safety-section audit log and as prompt-iteration
   data.
4. Exit criterion (from the spec's Phase 3): one agent can autonomously level
   1→10 unattended. Don't reach for memory (vector store, long-term DB) or a
   second concurrent agent before this works — multi-agent coordination will
   just multiply whatever's still broken in the single-agent loop.

## Operator dashboard panel

A single console for a human watching the server: live chat, click a
character to inspect it — stats, health/power bars, inventory, and beyond.
Independent of the AI-agent work above (this is for a human operator, not the
agent's own perception) and doesn't block or get blocked by it, though Phase
2's spellbook parsing (`SMSG_INITIAL_SPELLS`) and this panel's talent/spell
name lookups would end up wanting the same spell-name data — worth sharing if
both get built.

**Recommendation: extend `tools/wowmap` rather than stand up a new service.**
It already serves the player list, the live map, and
`GET /api/character/<name>`; the console is a natural evolution of what's
there (sidebar list → click → inspect drawer) instead of a fourth port to
deploy and keep in sync.

**Baseline — what already exists:**
- `GET /api/character/<name>` (added in #4) already returns level, race,
  class, gender, zone, map, position, gold, playtime, inventory (bag/slot/
  name/count), talents (spell id/spec), reputation (faction id/standing),
  achievements (id/date) — but nothing in the frontend calls it. It's an API
  with no UI yet.
- `tools/chat-feed` (added in #8) streams chat over SSE on port 9500, tested,
  but per `tools/chat-feed/README.md` its "Browser consumer" is a code
  snippet in the docs, not a page anyone loads.
- Health/power are **not** in the inspect payload yet, and only *current*
  values exist anywhere: `characters.characters.health` and `.power1`-`.power7`
  are real columns (confirmed against the live schema), refreshed every 5s
  (the `PlayerSaveInterval` tuned in #2). Max values are computed by the
  worldserver at runtime from level + class + gear. Stock TrinityCore *can*
  persist them to `characters.character_stats`, but that is disabled by
  default and the table is empty on this realm. See Phase B and
  `docs/HP_POWER_SPIKE.md`.

### Phase A — wire up character inspect (no backend gaps)

Everything needed already exists in the API response; this is pure frontend:

1. Click a name in the existing sidebar list (`tools/wowmap/app.py`'s
   `#list`) or a map marker → open a drawer/panel that calls
   `/api/character/<name>` and renders name/level/race/class/zone/gold/
   playtime — data already there, just not displayed.
2. Inventory list: bag/slot/name/count already returned: render as
   equipped slots (0-18) separate from bags (23+, plus extra-bag GUIDs) using
   the `bag`/`slot` convention documented in `tools/wowmap/README.md`
   (from #4's review fix). No item icons yet (see Phase D) — text rows with
   count are enough for v1.
3. Auto-refresh the drawer on the same interval as the player list, so a
   pinned character stays current while you watch it.

**Effort:** low — this is the highest-value-per-effort item in the whole
dashboard plan, since the data's already flowing.

### Phase B — health/power bars (needs a decision, not just code)

Add `health, power1, power2, power3, power4, power5, power6, power7` to
`fetch_character()`'s `SELECT` — cheap, do it alongside Phase A. The hard
part is max values, since nothing persists them. Options, roughly in order
of effort:

1. **Show current value only, no bar** (e.g. "Health: 4,231") — ships with
   Phase A, zero extra work, just not as visually useful as a bar.
2. **Spike the TrinityCore RA console** (already enabled — `Ra.Enable=1`,
   port 3443, added while deploying this session) or GM commands via
   `scripts/wow_console.py` for something like `.pinfo`/`.character info` —
   check whether any built-in command actually reports max health/power for
   an arbitrary *online* character, not just your own. Unknown until tried;
   spike it the same way `docs/CHAT_FEED_SPIKE.md` de-risked the chat feed
   before building #8, and write up the findings the same way.
3. **Approximate from formulas** (base health/mana by class+level, WotLK
   tables) — doable but ignores gear entirely, so it'll be visibly wrong for
   anyone not freshly leveled. Lowest recommended priority.
4. **Piggyback on agent perception** (this doc's Phase 1) once it parses
   `UNIT_FIELD_MAXHEALTH`/`UNIT_FIELD_MAXPOWER1-7` from live
   `SMSG_UPDATE_OBJECT` data — exact numbers, but only for characters an
   agent is actually near (perception is range-limited), not arbitrary
   offline/distant characters. A good long-term source, not a v1 plan.

**Recommendation:** ship option 1 with Phase A, spike option 2 as a short,
separate task before committing to a bar UI.

**Spike result (UM-46, `docs/HP_POWER_SPIKE.md`):** option 2 is a no-go,
because no console-usable GM command prints a player's max health/power. A
fifth, config-only option works instead: set
`PlayerSave.Stats.MinLevel=1` and `PlayerSave.Stats.SaveOnlyOnLogout=0` so
the worldserver writes `maxhealth`/`maxpower1-7` to
`characters.character_stats` on every 5 s save, then `LEFT JOIN` it in
`fetch_character()`. Roughly 1 day, no core change.

### Phase C — talent / reputation / achievement names

The inspect API already returns talent spell IDs, faction IDs, and
achievement IDs — but IDs, not names, since nothing in this repo maps them
yet (unlike zones, which have a small hardcoded `ZONES` dict in
`exporters/wow-exporter/exporter.py`). Two options:

1. **Small hardcoded dicts**, same pattern as `ZONES`/`RACES`/`CLASSES` in
   the exporter — cheap, but only covers whatever's manually added, same
   maintenance burden as the existing zone dict already has.
2. **Parse the relevant client DBCs** (`Talent.dbc`, `Spell.dbc`,
   `Faction.dbc`, `Achievement.dbc`) once at wowmap startup, the same way
   `tools/wowmap/transform.py` already parses `WorldMapArea.dbc`/
   `AreaTable.dbc`/`Map.dbc` for the live map. More complete, reuses a
   pattern that already exists in this codebase, and the spell-name lookup
   would double as what the agent's future spellbook/`cast_spell` work
   (Phase 2 of the agent plan) needs too.

**Recommendation:** option 2, once Phase A/B prove the panel is worth the
investment — don't build the DBC parser before there's a UI to put names in.

### Phase D — live chat panel

Give `tools/chat-feed`'s SSE stream an actual viewer:

1. A chat panel in the same page as the map/inspect drawer (or a toggleable
   tab) consuming `EventSource('http://<host>:9500/api/chat/stream')` per
   the snippet already in `tools/chat-feed/README.md`, styled by `kind`
   (say/yell/channel), auto-scrolling, reconnect-safe (the server already
   replays from `Last-Event-ID` — no extra backend work needed for that part).
2. **Cross-origin gap, confirmed:** wowmap serves from :9400, chat-feed from
   :9500, and `tools/chat-feed/app.py` sends no CORS headers at all today —
   a page served from wowmap can't `EventSource()` chat-feed's stream as-is.
   Either add `Access-Control-Allow-Origin` to chat-feed's response headers,
   or reverse-proxy both through one origin (simpler long-term if Phase E
   consolidates onto one page anyway).

### Phase E — consolidate into one console

Once A/B/D exist as working pieces: one page — sidebar (player list + chat
toggle), center (live map, unchanged), right-side drawer (inspect panel,
opens on click, closes on click-away). This is what makes it "a dashboard
panel" rather than three separate features bolted together.

### Item icons — deliberately out of scope for now

Rendering actual item icons would need extracting icon art from the client
MPQs (same category of work as the map-art extraction in
`tools/wowmap/extract_maps.py`, applied to `Interface/ICONS/*.blp` instead of
world map tiles) plus a `displayid`→icon mapping from `item_template`/DBCs.
Real, but large enough to be its own task — text-only inventory rows (Phase A)
are the pragmatic v1.

### Pickup order for this panel

1. Phase A (inspect UI) — ships immediately, no backend unknowns, highest
   value per effort in this whole plan.
2. Phase B step 1 (current-value-only health/power) — ships alongside A.
   Spike step 2 (RA/GM console) as a short side task once A is live.
3. Phase D (chat panel) — independent of A/B, can happen in parallel.
4. Phase C (name lookups) — only once A/B prove the panel gets used; needs
   the DBC-parsing investment to be worth it.
5. Phase E (consolidation) — the finishing pass once A/B/D exist separately.

## Observability polish (small, optional, not blocking the agent work)

- ~~**README.md is stale.**~~ Done (UM-28): README, `docs/ARCHITECTURE.md` and
  the handoff docs now match the compose files and `scripts/deploy.sh`.
- **Chat feed is explicitly a prototype** (`tools/chat-feed/README.md` →
  "Prototype limitations"): no auth, no durable history, no metrics/alerts,
  rotation handling untested against every runtime. Fine for a single-viewer
  homelab; revisit if it's ever exposed beyond LAN or feeds the agent's
  perception (a bot reading its own chat feed would want reconnect
  guarantees this doesn't promise yet).

## Later: Phase 4/5 — full autonomy, multi-agent

Talent builds, profession leveling, dungeon navigation, 1→80 leveling,
multi-agent coordination and PvP (`docs/AI-AGENT-SPEC.md` Phases 4-5).
Deliberately not planned in detail yet — depends entirely on what Phase 2/3
reveal about what's hard.

## Pickup order for the agent track

1. Perception (`agent/session.py` + `agent/perception.py`), steps 1-8 above —
   single highest-leverage task, fully speced, no dependencies.
2. `face`/`set_target` → `interact` → combat basics → `loot` — each proves
   out perceive→act without needing pathfinding yet.
3. Movement v1 (straight-line) — unblocks longer-range play; defer navmesh
   v2 until v1's straight-line ceiling is actually hit in practice.
4. LLM think loop — the first genuinely autonomous agent; exit criterion is
   one agent leveling 1→10 unattended.
5. Observability polish — pick up opportunistically, none of it blocks 1-4.
