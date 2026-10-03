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
| `DBC_DIR` | `/dbc` | directory containing `WorldMapArea.dbc`, `AreaTable.dbc`, and `Map.dbc` (plus `WorldMapOverlay.dbc` for subzone names, `Spell.dbc`, `Talent.dbc`, `TalentTab.dbc`, `Faction.dbc`, `Achievement.dbc` for names, and for item tooltips `SpellDuration.dbc`, `SpellRadius.dbc`, `ItemSet.dbc`, `ItemRandomProperties.dbc`, `ItemRandomSuffix.dbc`, `SpellItemEnchantment.dbc`, `GemProperties.dbc`, `SkillLine.dbc`, `RandPropPoints.dbc`; a missing file is logged and only its tooltip lines are left out) |
| `MAPS_DIR` | `/maps` | directory containing extracted `<area_id>.png` map art (and `<area_id>_base.png`) |
| `ICONS_DIR` | `/icons` | directory containing extracted item icon PNGs (see *Item icons*); `ItemDisplayInfo.dbc` is read from `DBC_DIR` |
| `GRID_MAPS_DIR` | `/server-maps` | the worldserver's extracted `maps/*.map` (read-only), for subzones; without it `subzone` is `null` |
| `LISTEN_PORT` | `9400` | HTTP listen port |
| `CALIBRATION_FILE` | `tools/wowmap/calibration.json` | persisted per-zone pixel offsets |
| `CHAT_FEED_URL` | derived from the page's own hostname at `:9500` | override if chat-feed isn't reachable on the same host as wowmap |
| `ACTIVITY_DB` | `/data/activity.sqlite3` | activity feed store; empty disables the feed |
| `ACTIVITY_CHAT_FEED_URL` | `http://chat-feed:9500` | chat-feed as seen from the wowmap process; empty = no chat events |
| `AUDIT_DIR` | `/audit` | agents' UM-51 decision logs (`<agent>/<day>.jsonl`); empty = no agent events |

Tests (need the `requirements.txt` packages):

```bash
python3 -m unittest discover -s tools/wowmap/tests
```

## Map art and subzones

`extract_maps.py` writes two images per zone: `<area_id>_base.png`, the game's
unexplored parchment, and `<area_id>.png`, the same art with every
`WorldMapOverlay` explored-area texture composited on top (the zone fully
explored, as the in-game map shows it). It also writes each overlay's own
texture to `overlays/<overlay_id>.png`. The page shows `<area_id>.png` unless a
character is open (see fog of war below). See `overlays.py` for the DBC layout
and `docs/LIVE-MAP.md` for the pipeline and how to re-run it.

`GET /api/areas` gives each zone a `subzones` list read from
`WorldMapOverlay.dbc` at startup:

```json
{"id": 1127, "area_ids": [3431, 3432], "name": "Sunstrider Isle",
 "names": ["Sunstrider Isle", "Shrine of Dath'Remar"],
 "art": [195, 5, 512, 512], "hit": [226, 27, 176, 161], "label": [314, 108]}
```

`art` and `hit` are `[x, y, w, h]` in image pixels on the 1024x768 canvas (no
world-coordinate transform, so calibration offsets don't apply). `hit` is the
DBC's hit rect, `null` for 21 overlays, which then use `art`; `label` is the
centre of `hit`, else of `art`. Hovering the map shows the name of the smallest
rect under the cursor; **Labels** toggles every subzone name.

## Continent maps

The zone selector lists the four continents above the zones. A continent is shown
on the same stage as a zone, and navigation works like the game's world map:

- **Left-click a zone** on the continent to open it. Hovering shows its name and
  outlines its `WorldMapArea` rect; **Labels** shows every zone name.
- **Right-click** the map, or **Continent** in the toolbar, to go back out from a
  zone to its continent.
- Players are drawn on both levels. On a continent a marker click opens the
  inspect drawer and stays on the continent. With **Follow** on, the stage opens
  the selected character's zone when it leaves the one shown.

`GET /api/areas` returns the continents next to `areas`, as
`continents: [{"area_id": "c1", "continent_view": true, "map": 1, "name": "Kalimdor",
"image": "continent_1.png", "has_image", "zones": [{"area_id", "name", "box"}]}]`.
`box` is the zone's rect in the continent's map-frame pixels, `[x, y, w, h]`.
`/api/players` gives each player `continent` (`"c1"`), `cont_x` and `cont_y`
(fractions of the 1002x668 frame), or `null` in an instance.

How it works and what it does not do:

- The continents are the four `WorldMapArea.dbc` rows with AreaID 0; they use the
  same transform as a zone, with the continent's rect (`transform.py`).
- Zone rects are bounding boxes and overlap heavily, so the zone under the pointer
  is chosen by its land: a zone counts when one of its own explored-area rects
  (`subzones`) is under the pointer, and the nearest box centre breaks ties. That
  picked the right zone for 37 towns tried against the client DBCs; it can still
  be wrong near a border.
- **Seven zones have no box**: Eversong Woods, Ghostlands, Silvermoon City, Isle of
  Quel'Danas, Azuremyst Isle, The Exodar and Bloodmyst Isle live on map 530 but
  are drawn on Eastern Kingdoms or Kalimdor, and their rect does not land on that
  art. They are reached from the selector, right-click from them goes to the right
  continent, and players in them are not drawn on any continent. Hrothgar's
  Landing (outside the Northrend frame) and Dalaran (an empty rect) are the same.
- Continents have no fog of war (the client has no overlays for them), no
  calibration and no trails. There is no level above the continents.

**After deploying this, re-run `extract_maps.py`**: it now also writes
`continent_<map_id>.png`. Until then the continents are left out of the selector
and the page behaves as before.

## The map stage (Leaflet)

The stage is a [Leaflet](https://leafletjs.com) map in `L.CRS.Simple`, with the
zone art as an `L.imageOverlay`. Map units are zone-art pixels with y pointing
down, so image point `(x, y)` is `LatLng(-y, x)` (`ll()` in the page).

- **Zoom and pan:** mouse wheel or pinch, drag, double-click, and the +/−
  buttons. The range is one level out to three levels in (8×) around the level
  that fits the zone. **Fit zone** returns to the fitted view.
- The 5 s refresh moves markers in place and never changes the view. Switching
  zones fits the new zone. Resizing the window (or the sidebar, or opening the
  inspect drawer) re-fits only if the view was still the fitted one.
- **Markers** are Leaflet markers with a `divIcon`; clicking one opens the
  inspect drawer. A plain click on the map closes the drawer; dragging does not.
- **Trail** draws each character's path as a polyline in its class colour. The
  server keeps no position history, so a trail holds what this page has seen
  since it was opened (at most 720 points per character, one zone at a time).
- **Follow** pans to the selected character on every refresh.

Leaflet 1.9.4 is vendored in `static/` (`leaflet.js`, `leaflet.css`, unmodified
npm `dist` files, BSD-2-Clause, licence in `static/leaflet-LICENSE`) and served
by `app.py` under `/static/`. The realm is LAN-only, so the page loads nothing
from a CDN, and there is still no build step. `tests/test_static.py` pins the
files' SHA-256; update the hashes when upgrading Leaflet.

## Fog of war

With a character open in the inspect drawer, the zone art shows only what that
character has explored, the way its in-game map does: unexplored areas stay
parchment. Closing the drawer returns to the fully explored art. **Fog of war:
on/off** in the toolbar turns it off without closing the drawer.

- `GET /api/character/<name>/explored` returns
  `{"name", "explored_bits", "zones": {"<area_id>": [overlay ids]}}`: per zone,
  the `WorldMapOverlay` rows the character has revealed. `404` for an unknown
  character. Zones with nothing revealed are left out.
- `GET /maps/<area_id>.png?explored=<overlay ids, comma-separated>` returns the
  zone's base art with those overlays stacked on it. IDs that are not overlays of
  that zone are ignored; a malformed list is a `404`.
- `/api/areas` gives each zone a `fog` flag: true when its base art and every
  overlay texture are in `MAPS_DIR`. The page only fogs zones where it is true,
  so cities (no overlays) and maps extracted before this feature keep the fully
  explored art.

How it is decoded (`fog.py`): `characters.exploredZones` is 128 space-separated
uint32 values; an area's bit is `AreaTable.dbc` field 3; an overlay is revealed
when any of its areas' bits is set. The first two come from TrinityCore
(`Player::SaveToDB`, `Player::CheckAreaExploreAndOutdoor`); the last is the
client's rule and is the part to check against the in-game map.

The art is composed with Pillow on request and cached (24 composites in memory;
the browser caches each URL for a day). An online character's explored areas
reach the database on its next save, so the map follows within a save interval
plus the 5 s refresh.

**After deploying this, re-run `extract_maps.py`** (see `docs/LIVE-MAP.md`): the
`overlays/` directory and, on older extractions, `<area_id>_base.png` do not
exist until then, and fog of war stays off for every zone.

## Calibrating map art

WorldMapArea rects do not always line up exactly with the visible map art. The
map page therefore supports a small, per-zone translation after the normalised
world-coordinate transform has been converted to image pixels.

1. Select the affected zone and click **Calibrate: off** to enter calibration mode.
2. Click a known point on the map (for example, one identified with `.gps` or a
   creature spawn) and drag the reference crosshair until the player markers line up.
   The preview moves the markers immediately.
   Dragging the map is off while calibrating; zooming still works, so zoom in
   first for a finer offset.
3. Click **Save calibration**. This writes that zone's `{dx, dy}` pixel delta to
   `calibration.json`; future page loads use it automatically.

The offset is in zone-art pixels, which are the map's units, so it means the
same at every zoom level and files saved before the Leaflet stage still apply.

The file is intentionally a tiny operator-maintained JSON dictionary keyed by
area ID. It is safe to edit while the service is stopped. `calibrate.py` remains
the offline helper for validating the underlying DBC transform against known
spawn data; the UI stores only the final visual translation.

## Item icons

Inventory rows in the inspect drawer show the item's icon. The chain is
`world.item_template.displayid` → `ItemDisplayInfo.dbc` record (field 5,
`InventoryIcon`; layout in `item_icons.py`) → `Interface\Icons\<name>.blp` in the
client MPQs → `<ICONS_DIR>/<name lowercased>.png` → `GET /icons/<file>.png`.

The icons come from the user-supplied client, so like the map art they are
extracted on the VM and never committed. Run once (and again only after a client
change), on the VM, with `mpyq` and `Pillow` available:

```bash
cd /opt/wow-server/tools/wowmap
python3 extract_icons.py --client /opt/wow-server/client \
    --dbc /opt/wowmap-data/dbc --out /opt/wowmap-data/icons
```

It writes every icon ItemDisplayInfo.dbc names (about 4,700 PNGs, ~36 MB, about a
minute); existing files are skipped unless `--force`. About 50 icon names in the DBC
have no texture in the 3.3.5a client; those items show the placeholder, as does
everything until the script has been run. `monitoring/docker-compose.yml` mounts
`/opt/wowmap-data/icons` read-only at `/icons`; the files are served with a 30-day
`Cache-Control`.

If the host has no pip packages, a throwaway container works and writes nothing
outside the output directory:

```bash
docker run --rm -v /opt/wow-server:/src:ro -v /opt/wowmap-data:/data \
  --entrypoint sh monitoring-wowmap:latest -c \
  "pip install -q mpyq Pillow && cd /src/tools/wowmap && python extract_icons.py \
   --client /src/client --dbc /data/dbc --out /data/icons"
```

## Inspect drawer

Click a name in the sidebar list or a player marker on the map to open the
inspect drawer on the right. It shows:

- **Header**: name, online/offline, level, race, class (class colour), zone.
- **Status**: current health and the powers the class uses (warrior rage, rogue
  energy, death knight runic power, druid mana/rage/energy, everyone else mana),
  gold as `g s c`, playtime, last logout, and map/x/y/z.
- **Equipped**: a paper doll laid out like the game's character window (head,
  neck, shoulder, back, chest, shirt, tabard, wrist on the left; hands, waist,
  legs, feet, rings, trinkets on the right; main hand, off hand, ranged below).
- **Bags**: the bag bar (bag slots 19-22), then the backpack (slots 23-38) and
  each equipped bag as a grid of squares, four wide and sized to the bag's
  `container_slots`. Like the game's bag windows the slots fill from the bottom
  right, so a bag whose size isn't a multiple of four has its gap top left.
  Bag windows wrap onto a new row when the drawer is narrow.
- Every item is a 36 px square with its icon (a `?` placeholder until
  `extract_icons.py` has been run), a border in the item's quality colour and a
  stack count badge. Empty slots are dimmed squares named after the slot.
  Hovering (or focusing, or tapping) an item shows an in-game style tooltip built
  by `item_tooltip.py`; it is kept inside the viewport.
- Collapsible **Bank** (28 squares, seven wide, plus bank bags), **Keyring**, **Currency** (only when non-empty), and
  **Talents** (grouped by spec and tree, with rank), **Reputation** (sorted by
  value, with tier), and **Achievements** (title, points, date). Names come from
  the client DBCs; an unknown id falls back to the raw id.

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

## Recent activity (UM-76)

`GET /api/character/<name>/activity?limit=50` (max 500) returns the newest events
for one character, and the inspect drawer shows them under **Recent activity**.
`activity.py` fills a SQLite store (`ACTIVITY_DB`, the newest 500 events per
character, so history survives a restart) from three background sources:

| Source | What | How |
|---|---|---|
| `db` | login/logout, level, zone, quests turned in, items gained/lost, money | every 5 s, one read-only consistent snapshot of the **online** characters: `characters` (money, level, zone, online), `character_inventory` ⨝ `item_instance` summed per `itemEntry`, `character_queststatus_rewarded`; diffed against the previous snapshot in memory. Names come from `world.item_template` / `world.quest_template` only for entries that changed (cached). |
| `chat` | public chat (`say`, `yell`, `channel`) | chat-feed's SSE stream, resumed with `Last-Event-ID`. Anything else (whisper, party, guild, …) is dropped here even if chat-feed ever publishes it. Agents' chat comes from their audit log instead. |
| `audit` | every agent decision except `idle`, with its result (attack, loot, sell, trade, chat including whispers/party) | new lines of `AUDIT_DIR/<agent>/<day>.jsonl`, the same records PR #113's live "Agent mind" tab shows. |

The database does not record *why* something changed, so these are **inferred**
and carry an `inferred` badge: an item that disappears while money goes up is
*sold* (only items with a `SellPrice`); the same item and count leaving one
character and reaching another in the same poll is a *trade*; items that
disappear when a quest is turned in were *handed in*; any other disappearance is
*used or destroyed* (mail and bank-free trades included); an item that appears
while money goes down is *bought*. The first poll after a start is only a
baseline, and changes made while a character is offline are not reported.

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
  "inventory": [{"bag": 0, "slot": 15, "item_guid": 42, "item_entry": 23346,
                 "item_name": "Battleworn Claymore", "count": 1, "quality": 1,
                 "icon": "/icons/inv_sword_04.png", "container_slots": 0,
                 "tooltip": [{"left": "Battleworn Claymore", "color": "quality"},
                             {"left": "Two-Hand", "right": "Sword", "color": "white"},
                             {"left": "3 - 5 Damage", "right": "Speed 2.90", "color": "white"},
                             {"left": "(1.4 damage per second)", "color": "white"},
                             {"left": "Durability 25 / 25", "color": "white"},
                             {"left": "Requires Level 1", "color": "white"},
                             {"left": "Item Level 2", "color": "yellow"},
                             {"left": "Sell Price:", "money": 9, "color": "white"}]}],
  "talents": [{"spell": 12282, "spec": 0, "name": "Improved Heroic Strike",
               "tree": "Arms", "tree_order": 0, "rank": 1}],
  "reputation": [{"faction": 76, "standing": 2000, "flags": 17,
                  "faction_name": "Orgrimmar", "value": 6000, "tier": "Friendly"}],
  "reputation_panel": [{"faction": 1118, "name": "Classic", "header": true, "rep": null,
    "children": [{"faction": 67, "name": "Horde", "header": true, "rep": null,
      "children": [{"faction": 76, "name": "Orgrimmar", "header": false,
        "rep": {"value": 6000, "rank": "Friendly", "rank_id": 5, "bar_value": 3000,
                "bar_max": 6000, "at_war": false}}]}]}],
  "achievements": [{"achievement": 6, "date": 1710000000, "name": "Level 10",
                    "points": 10}]
}
```

- `container_slots` is the bag's `item_template.ContainerSlots` (0 for non-bags).
  `tooltip` is the item's tooltip as lines (`left`, optional `right`, `color` one
  of `quality`/`white`/`green`/`yellow`/`gray`/`red`, and `money` in copper for the
  sell price line, which is for the whole stack). `item_tooltip.py` builds it from
  `world.item_template` (column names as in TrinityCore's
  `ObjectMgr::LoadItemTemplates`) plus `item_instance.flags` (Soulbound),
  `.durability`, `.randomPropertyId` ("of the Bear" and its stats) and
  `.enchantments` (enchants and gems), and the client DBCs in `DBC_DIR`:
  `Use:`/`Equip:`/`Chance on hit:` lines from `spellid_N`/`spelltrigger_N` (the
  spell's `Description` with its `$` variables filled in, `tools/dbc/spelltext.py`),
  the item set block (`itemset` → `ItemSet.dbc`; equipped pieces are the
  character's bag 0 slots 0-18, piece names come from `world.item_template`),
  random suffix/property names and stats, permanent/temporary enchants, socketed
  gems, empty sockets and the socket bonus, and `Requires <skill> (<rank>)` /
  `Requires <faction> - <rank>`. Not shown yet: cooldown and charge suffixes on spell
  lines, spell triggers 4/5/6, and any spell whose description uses a `$` variable
  the resolver doesn't know (that line is left out rather than guessed; about 89% of
  the client's described spells resolve, the level-scaled ones do not). `$z` (the
  Hearthstone's bind point) reads "your home location".
- `quality` is `item_template.Quality` (0 poor … 7 heirloom). `icon` is a
  same-origin URL for the item's icon, or `null` when the display id has no
  icon or the PNG hasn't been extracted; clients draw a placeholder then.
- Names come from the client DBCs in `DBC_DIR`, read by the shared stdlib reader
  in `tools/dbc` (`wdbc.py` for the file format, `names.py` for the build-12340
  record layouts and their TrinityCore citations). They load once in a
  background thread at startup (about 1 s and 15 MB). A missing or
  wrong-build DBC only leaves its names `null`.
- `reputation[].standing` is the raw `character_reputation.standing`, which
  TrinityCore stores without the faction's starting value. `value` adds the
  race/class starting value from `Faction.dbc` the way `ReputationMgr` does, and
  `tier` is its rank (Hated … Exalted). `flags` is `character_reputation.flags`
  (ReputationMgr's `ReputationFlags`: 0x01 visible, 0x02 at war, 0x04 hidden,
  0x08 header, 0x20 inactive, 0x80 header with its own bar). This list has every
  row, hidden and header factions included.
- `reputation_panel` is the in-game reputation window (#92), built by
  `GameNames.reputation_panel`: only factions flagged visible and not hidden,
  nested under the `Faction.dbc` ParentFactionID headers ("Classic" › "Horde" ›
  "Orgrimmar"), siblings sorted by name. A header is listed when something under
  it is, or when it has its own bar (0x80, e.g. Horde Expedition) and is visible.
  Visible factions flagged inactive move to a trailing "Inactive" group. `rep` is
  null for a header without a bar; `rank_id` is the client's standingID (1 Hated …
  8 Exalted) and `bar_value`/`bar_max` the progress inside that rank (Exalted is
  out of 1000). The drawer colours bars with the game's `FACTION_BAR_COLORS`.
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
