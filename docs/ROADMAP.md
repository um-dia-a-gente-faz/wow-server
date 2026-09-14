# Roadmap — WoW Server Observability

Ideas discussed, ordered by readiness (most doable first).

## 1. Character inspect panel

A page or Grafana dashboard showing a single character's full state.

**Data available (already in the exporter or DB):**
- `wow_character_level`, `wow_character_playtime_seconds` — exporters/metrics
- `characters.characters`: level, race, class, gender, zone, map, position, money, totaltime, logout_time — accessible via the game DB

**Data missing (would need new queries):**
- Inventory (`character_inventory` → `item_instance` → `item_template` for names)
- Talents (`character_talent` / `character_action`)
- Reputation (`character_reputation`)
- Achievements

**Effort:** medium. New exporter metrics or a small `/api/character/<name>` endpoint on the wowmap service. Could also be a Grafana table panel if metrics have low cardinality, but per-character data is better as an API.

**Dependencies:** none — the DB schemas exist, the exporter has MySQL access.

## 2. Movement trails / heatmap

Show where a player has been over time.

**Data available:**
- `wow_player_position_x/y{character}` — already exported to Prometheus as time series (15 s scrape interval)
- `characters.characters.position_x/y` — updated every `PlayerSaveInterval` (90 s default, tunable)

**How:**
- Trails: Grafana timeseries with the position metrics. A scatter plot of `position_x` vs `position_y` over a time range shows the path.
- Heatmap: a Grafana heatmap panel on `wow_player_position_x` bucketed by zone, or a custom exporter metric counting position occurence per grid cell.

**Effort:** low for trails (already works with XY scatter + time range). Medium for heatmap (needs a new exporter metric bucketing positions into a coarse grid).

**Prerequisite:** set `TC_WORLD__PlayerSaveInterval=5000` for finer movement (5 s saves instead of 90 s).

## 3. Agent behaviour panel

A dashboard tracking what autonomous agents/players are doing.

**Data available:**
- `wow_online_player_info` — 1 per online player, with map/zone/instance/level/class labels
- `wow_character_level`, `wow_character_playtime_seconds`
- Position, zone, map per player (already in Prometheus)

**Possible panels:**
- Player online status over time (from `wow_players_online` — already working)
- Playtime per character (bar gauge, already in wow-players dashboard)
- Zone changes (rate of `wow_player_pos_x` changing zone label — PromQL `count by (zone)`)
- Level progression (when a `wow_character_level` increases)

**Missing:**
- Quest completion tracking — would need `character_queststatus` table metrics
- Chat activity — not in any DB table (TrinityCore chat is in-memory; could log to a file and tail it)
- Aggression / combat — no built-in counter; could come from server logs

**Effort:** low-moderate. Most of the data already flows; the panel is UI work.

## 4. Global chat feed

A live feed of `/say`, `/yell`, `/world`, `/guild` chat messages.

**Data source:** TrinityCore's worldserver stdout includes chat lines when certain log levels are enabled. `/world` chat is a custom channel handled by a C++ script. The worldserver console socket.io (`worldserver_state`) already streams server output.

**Possible approach:**
- A dedicated log-watcher process that tails the container's worldserver stdout, parses chat patterns, and exposes them as an SSE (Server-Sent Events) endpoint or WebSocket.
- Or: expose a `/stream` endpoint on the wowmap service that the frontend subscribes to.

**Effort:** high. Requires parsing unstructured server output and building a streaming endpoint.

**Alternative:** the client-side addon approach (an in-game addon writes chat to a shared channel/file). Even harder — needs server-side mod.

## 5. Live map — alignment calibration

The WorldMapArea rect does not linearly cover the full 1024×768 tile sheet (the art has decorative borders and sea beyond the rect). Positions are in the right zone but may be off by a constant translation per zone.

**How to fix:**
- **Calibration scrim:** add a "calibrate" mode to the map page where you can drag the marker to the correct spot and the offset is saved per zone (a small JSON file).
- **Reference points:** use known in-game positions (your own `.gps` output, or world.creature spawns) and dial in the rect offset.

**Effort:** low. The map page already has the coordinate pipeline; an offset is just `px += delta`.

**Alternative:** render terrain from `maps/*.map` files instead of Blizzard art — guaranteed alignment but a larger build (needs a terrain renderer for TrinityCore's binary map format).

## How to pick what to build next

1. Set `TC_WORLD__PlayerSaveInterval=5000` (5 s saves) — immediately improves all observability.
2. Character inspect API — the most "missing" piece, enables agent tools.
3. Agent panel dashboard — visualises what the exporter already has.
4. Movement trails — enabled by step 1 + position metrics already flowing.
5. Chat feed — biggest effort, save for later.