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

## Now: AI agent perception (Phase 1 completion)

This is the actual point of the repo (`docs/AI-AGENT-SPEC.md`) and the
highest-leverage next task — everything else is scaffolding around it. The
protocol client (`agent/`) already does SRP6 auth, world handshake, login,
keepalive, chat, and target actions end to end against the live server. The
one missing piece is **perception**: `agent/perception.py::WorldState`
currently only records that a GUID exists, with no position/health/name data.

**Task:** parse `SMSG_UPDATE_OBJECT` / `SMSG_COMPRESSED_UPDATE_OBJECT` in
`agent/session.py::_parse_update_object()` — block types, packed GUIDs, the
update-mask bitfield, and the movement block for `UPDATEFLAG_LIVING`. Fully
speced byte-by-byte, including which `UNIT_FIELD_*` indices to extract, in
`docs/NEXT-AGENT-HANDOFF.md` (still accurate; only its "checkout
feat/agent-client-protocol" setup line is stale — that branch is merged, work
straight from `main`).

**Definition of done** (from the handoff doc):
1. `python3 -m agent --dry-run` logs "perception: N objects tracked" with N > 0
2. `session.player_position` updates when the character moves (teleport via
   `.go xyz` from the web UI console and confirm the change)
3. Nearby NPCs show up in the object list with entry IDs

**Effort:** medium — the hard crypto/protocol plumbing is already done; this
is careful binary parsing against a documented wire format.

## Next: Phase 2 — basic actions

Once perception works, `agent/actions.py` currently only has chat, party
invite, and target. In spec order (`docs/AI-AGENT-SPEC.md` Phase 2):

1. **Movement** — pathfind and walk (`move_to`); the server has mmaps built
   already (see `docs/DEPLOYMENT.md`), so navigation should route through
   them rather than straight-line walking into walls.
2. **NPC interaction** — gossip, vendor buy/sell, quest accept/turn-in.
3. **Combat** — auto-attack, cast_spell, loot.
4. **Inventory** — equip/unequip, use_item.

**Dependency:** all of these need perception (to know where NPCs/mobs are,
what quests are offered, what's lootable) — build perception first.

## Then: Phase 3 — think loop + LLM integration

`agent/__main__.py` has a `--once` mode described as "one full think cycle
(perceive → act)" but no LLM call yet — the decision step is still a stub or
hardcoded. Once Phase 1+2 land: wire an actual LLM call (system prompt +
state + perception → next action), start with a single agent leveling 1→10
per the spec's Phase 3 exit criterion, before touching memory or multi-agent.

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

1. Perception parsing (`agent/session.py`) — single highest-leverage task,
   fully speced, no dependencies.
2. Movement + pathfinding — first action that needs perception to be useful.
3. NPC interaction + combat — round out Phase 2.
4. LLM think loop — the first genuinely autonomous agent.
5. Observability polish — pick up opportunistically, none of it blocks 1-4.
