# Roadmap

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

### Phase 1 — perception (do this first)

This is the actual point of the repo (`docs/AI-AGENT-SPEC.md`) and the
highest-leverage next task — everything else is scaffolding around it. The
protocol client (`agent/`) already does SRP6 auth, world handshake, login,
keepalive, chat, and target actions end to end against the live server. The
one missing piece is **perception**: `agent/perception.py::WorldState`
currently only records that a GUID exists, with no position/health/name data,
and `agent/session.py::_parse_update_object()` deliberately stops after
reading each block's GUID.

Reading the current stub closely surfaces two things the handoff doc doesn't
call out, both worth fixing as part of this work, not after:

- **Byte-offset desync risk.** `_parse_update_object` never consumes the
  update-flags/mask/values/movement bytes after a VALUES/CREATE_OBJECT block,
  so `off` doesn't advance past them. With more than one block per packet
  (the common case once nearby entities exist), every block after the first
  gets parsed from the wrong offset. This has to be fixed *while* adding
  field parsing, not as a follow-up — a partially-correct version that reads
  the wrong number of bytes is worse than the current no-op stub.
- **`type 5` (NEAR_OBJECTS) block still falls through to `continue` with zero
  bytes consumed** — same desync risk. `type 4` (OUT_OF_RANGE_OBJECTS) is
  handled but wrong: the real format is `uint32 count` followed by `count`
  packed GUIDs (objects that left range), not one packed GUID guarded by a
  reused `mask` variable.
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
   listed in `docs/NEXT-AGENT-HANDOFF.md` (`OBJECT_FIELD_ENTRY`,
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

### Phase 3 — think loop + LLM integration

`agent/__main__.py::_run_loop` already has the perceive/think/act skeleton
with the think step commented as a placeholder; `agent/config.py` already has
unused `llm_base_url`/`llm_api_key`/`llm_model` fields waiting for it. Once
Phase 1 + enough of Phase 2 land to make an action loop meaningful:

1. Build the system prompt from `world_state.get_objects()` +
   `player_position` + a fixed action catalog (the subset of
   `docs/AI-AGENT-SPEC.md`'s action list actually implemented so far).
2. One LLM call per think cycle → one action, using the existing
   `think_interval` pacing (`AGENT_THINK_INTERVAL_S`, default 3s) as the
   rate limit — no need to build a separate rate limiter first.
3. Log every (perception snapshot, LLM decision, action, result) tuple —
   this doubles as the audit log the spec's Safety section asks for, and as
   debugging data for prompt iteration.
4. Exit criterion (from the spec's Phase 3): one agent can autonomously level
   1→10 unattended. Don't reach for memory (vector store, long-term DB) or a
   second concurrent agent before this works — multi-agent coordination will
   just multiply whatever's still broken in the single-agent loop.

## Observability polish (small, optional, not blocking the agent work)

- **Chat feed has no viewer.** The SSE backend (port 9500) works and is
  tested, but nobody's watching it — `tools/chat-feed/README.md`'s "Browser
  consumer" is a code snippet, not a page. A small static page (same style as
  wowmap) or a panel in the live-map page would make it actually useful.
- **README.md is stale.** The architecture diagram still shows a separate
  `/opt/monitoring/` and doesn't mention `tools/wowmap`, `tools/chat-feed`,
  `agent/`, or that the VM is now a git checkout deployed via
  `scripts/deploy.sh`. Worth a pass so a fresh reader isn't misled.
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

## How to pick up from here

1. Perception (`agent/session.py` + `agent/perception.py`), steps 1-8 above —
   single highest-leverage task, fully speced, no dependencies.
2. `face`/`set_target` → `interact` → combat basics → `loot` — each proves
   out perceive→act without needing pathfinding yet.
3. Movement v1 (straight-line) — unblocks longer-range play; defer navmesh
   v2 until v1's straight-line ceiling is actually hit in practice.
4. LLM think loop — the first genuinely autonomous agent; exit criterion is
   one agent leveling 1→10 unattended.
5. Observability polish — pick up opportunistically, none of it blocks 1-4.
