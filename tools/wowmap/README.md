# wowmap

Small, build-free live map for a TrinityCore 3.3.5a server. It reads online
characters from `characters.characters`, converts their world coordinates with
the client DBCs, and overlays them on extracted World Map art. It also serves
JSON data from the TrinityCore database.

## Run

The container setup supplies the usual MySQL variables, DBCs, and map PNGs. For
local use, install `requirements.txt` and run:

```bash
python3 app.py
```

Configure MySQL with `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, and
`MYSQL_PASSWORD` (plus the optional paths and port described below and in
`app.py`).

| Env | Default | Meaning |
|---|---|---|
| `DBC_DIR` | `/dbc` | directory containing `WorldMapArea.dbc`, `AreaTable.dbc`, and `Map.dbc` |
| `MAPS_DIR` | `/maps` | directory containing extracted `<area_id>.png` map art |
| `GRID_MAPS_DIR` | `/server-maps` | the worldserver's extracted `maps/*.map` (read-only), for subzones; without it `subzone` is `null` |
| `LISTEN_PORT` | `9400` | HTTP listen port |
| `CALIBRATION_FILE` | `tools/wowmap/calibration.json` | persisted per-zone pixel offsets |
| `CHAT_FEED_URL` | derived from the page's own hostname at `:9500` | override if chat-feed isn't reachable on the same host as wowmap |

Tests (need the `requirements.txt` packages):

```bash
python3 -m unittest discover -s tools/wowmap/tests
```

## Calibrating map art

WorldMapArea rects do not always line up exactly with the visible map art. The
map page therefore supports a small, per-zone translation after the normalised
world-coordinate transform has been converted to image pixels.

1. Select the affected zone and click **Calibrate: off** to enter calibration mode.
2. Click a known point on the map (for example, one identified with `.gps` or a
   creature spawn) and drag the reference crosshair until the player markers line up.
   The preview moves the markers immediately.
3. Click **Save calibration**. This writes that zone's `{dx, dy}` pixel delta to
   `calibration.json`; future page loads use it automatically.

The file is intentionally a tiny operator-maintained JSON dictionary keyed by
area ID. It is safe to edit while the service is stopped. `calibrate.py` remains
the offline helper for validating the underlying DBC transform against known
spawn data; the UI stores only the final visual translation.

## Inspect drawer

Click a name in the sidebar list or a player marker on the map to open the
inspect drawer on the right. It shows:

- **Header**: name, online/offline, level, race, class (class colour), zone.
- **Status**: current health and the powers the class uses (warrior rage, rogue
  energy, death knight runic power, druid mana/rage/energy, everyone else mana),
  gold as `g s c`, playtime, last logout, and map/x/y/z.
- **Equipped**: equipment slots 0-18 by slot name.
- **Bags**: backpack slots 23-38, then each equipped bag's contents.
- Collapsible **Bank**, **Keyring**, **Currency** (only when non-empty), and
  **Talents**, **Reputation**, and **Achievements** as raw IDs (names come later
  with the shared DBC loader).

The drawer re-fetches the character on the page's 5 s tick. It only re-renders
when the response changed, and it keeps its scroll position and open sections.
Close it with ✕, Esc, or a click outside it (the map toolbar doesn't count).
Offline characters aren't in the list, so `/#inspect=<name>` opens the drawer
directly. At widths of 600 px or less the drawer takes the full width.

Every value from the database (character, item, and zone names) is rendered
with `textContent`, never `innerHTML`. The drawer's CSS, HTML, and JS live in
`INSPECT_CSS` / `INSPECT_HTML` / `INSPECT_JS` in `app.py`. They are spliced into
`PAGE` at `@inspect-*` markers, so other panels can be added the same way.

Health is current only, and so is power. The worldserver computes maximum
health and power at runtime and never saves them, so there are no bars (see
`docs/ROADMAP.md`, Operator dashboard panel, Phase B). With the 5 s
`PlayerSaveInterval`, damage taken in game shows up within about 10 s.

## Console layout (map + player list + chat + inspect drawer)

The page is one console: left sidebar (player list, with a **Chat** tab toggled
by the `c` key or the tab buttons), center live map, right-side inspect drawer.

- **Chat tab**: connects an `EventSource` to `tools/chat-feed`'s SSE stream
  (`CHAT_FEED_URL`, default `<page hostname>:9500`) only while the tab is
  visible, so a viewer who never opens it costs nothing extra. Chat-feed adds
  `Access-Control-Allow-Origin: *` to its responses so this cross-origin
  `EventSource` call works without a reverse proxy. Every message is rendered
  with `textContent`; clicking a sender name calls the same `selectCharacter()`
  used by the list and map markers.
- **Unified selection**: clicking a character in the list, on a map marker, or
  as a chat sender highlights them everywhere and pans the map to their zone
  (`selectCharacter()` in `app.py`'s main script). It only *toggles* CSS
  classes on existing DOM nodes rather than rebuilding `#list`/`#markers` —
  rebuilding while the click event that triggered it is still bubbling detaches
  the clicked element, which makes the drawer's click-away handler misfire and
  close the drawer it just opened.
- **Search**: `/` focuses the character search box (filters the player list by
  name); `Esc` closes the drawer; `c` toggles the chat tab.
- **Sidebar**: resizable (drag the right edge) and collapsible (the `«`/`»`
  buttons). Width, collapsed state, active tab, last-viewed zone, and the
  pinned (open) character persist to `localStorage`, each access wrapped in
  `try`/`catch` so private browsing or disabled storage never breaks the page.
- **Shared polling**: one 5 s tick fetches `/api/players` and `/api/summary`
  and also drives the open drawer's refresh — the chat tab doesn't add polling
  since it's push-based (SSE).

## Agent mind (UM-50)

Agents started with `AGENT_HTTP_PORT` serve a read-only API (`agent/http_api.py`).
Point wowmap at them with `AGENT_API_URLS`, e.g.
`AGENT_API_URLS=Luaprata=http://192.168.1.80:9601,Farstrider=http://192.168.1.80:9602`.

- `GET /api/agents` lists the configured agent names (never their URLs).
- `GET /api/agent/<name>/<view>` proxies the agent's `GET /<view>` for
  `healthz`, `state`, `perception` and `brain` (`?n=` is forwarded for brain).
  Unknown agent or view: 404; agent down: 502. Nothing else is forwarded.
- Agent markers get a dashed cyan ring and an "agent" tag in the player list.
  Inspecting one shows a **Character / Agent mind** tab strip; the Agent mind
  tab polls brain + perception every 3 s while it is visible (goal, model,
  tokens, reflexes, last 5 decisions, nearby units/players/objects).

## Character inspect endpoint

`GET /api/character/<name>` returns a character's saved state. The name is URL
decoded; a missing character returns `404` with `{"error":"character not found"}`.
Inventory, talents, reputation, and achievements are optional best-effort
lookups, so an unavailable detail table produces an empty list without hiding
the character's base state.

```json
{
  "name": "Thrall",
  "level": 80,
  "race": 2,
  "race_name": "Orc",
  "class": 7,
  "class_name": "Shaman",
  "class_color": "#0070DE",
  "gender": 0,
  "zone": 1637,
  "zone_name": "Orgrimmar",
  "map": 1,
  "map_name": "Kalimdor",
  "position_x": 1502.3,
  "position_y": -4415.2,
  "position_z": 22.1,
  "orientation": 1.2,
  "continent_name": "Kalimdor",
  "subzone": null,
  "subzone_name": null,
  "map_coords": {"x": 52.4, "y": 82.5},
  "money": 1234500,
  "money_gold": 123.45,
  "totaltime": 86400,
  "logout_time": 1710000000,
  "online": true,
  "health": 4231,
  "power": {"mana": 1020, "rage": 0, "focus": 0, "energy": 100,
            "happiness": 0, "rune": 0, "runic_power": 0},
  "inventory": [{"bag": 0, "slot": 0, "item_guid": 42, "item_entry": 12345,
                 "item_name": "Example Item", "count": 1}],
  "talents": [{"spell": 12345, "spec": 0}],
  "reputation": [{"faction": 72, "standing": 42000}],
  "achievements": [{"achievement": 6, "date": 1710000000}]
}
```

- `money` is in copper; `money_gold` is the same value divided by 10000.
- Position (also on every `/api/players` entry): `map_name` is Map.dbc's directory
  name ("Expansion01"). `continent_name` is the continent the game shows the zone
  on: the map named by the zone's `WorldMapArea` DisplayMapID when set (Eversong
  Woods on map 530 is "Eastern Kingdoms", Azuremyst Isle "Kalimdor"), else Map.dbc's
  display name ("Outland", or the instance's name). `map_coords` are the in-game map coordinates (0-100) inside the
  zone's `WorldMapArea` rect, `null` when the zone has none (most instances).
  `subzone` comes from the area grid in the worldserver's `maps/*.map` (the same
  lookup as `GridMap::getArea`); it ignores the WMO override for building interiors,
  and is `null` when the grid has no subzone of the saved zone at that spot.
- `health` and `power` hold current values from `characters.health` and
  `power1`-`power7`. The `power` keys follow TrinityCore's `Powers` enum
  (`POWER_MANA = 0` … `POWER_RUNIC_POWER = 6`), so `power1` is mana and `power7`
  is runic power. Rage and runic power are stored in tenths (1000 shows as 100
  in game).

### Inventory `bag` / `slot` convention

`bag` is `0` for the character's own slots. For any other item, `bag` is the
`item_guid` of the container holding it, and `slot` is the position inside that
container. Own-slot ranges from TrinityCore 3.3.5 `Player.h`:

| `slot` (with `bag = 0`) | Meaning |
|---|---|
| 0-18 | equipped (head, neck, shoulders, shirt, chest, waist, legs, feet, wrists, hands, finger ×2, trinket ×2, back, main hand, off hand, ranged, tabard) |
| 19-22 | equipped bags (their contents use the bag's `item_guid` as `bag`) |
| 23-38 | backpack |
| 39-66 | bank |
| 67-73 | bank bags |
| 86-117 | keyring |
| 118-149 | currency tokens |
