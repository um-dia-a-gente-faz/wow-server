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
| `LISTEN_PORT` | `9400` | HTTP listen port |
| `CALIBRATION_FILE` | `tools/wowmap/calibration.json` | persisted per-zone pixel offsets |

Tests (need the `requirements.txt` packages):

```bash
python3 -m unittest discover -s tools/wowmap/tests
```

## Calibrating map art

WorldMapArea rects do not always line up exactly with the visible map art. The
map page therefore supports a small, per-zone translation after the normalised
world-coordinate transform has been converted to image pixels.

1. Select the affected zone and click **Calibrar: off** to enter calibration mode.
2. Click a known point on the map (for example, one identified with `.gps` or a
   creature spawn) and drag the reference crosshair until the player markers line up.
   The preview moves the markers immediately.
3. Click **Salvar calibração**. This writes that zone's `{dx, dy}` pixel delta to
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
- **Equipado**: equipment slots 0-18 by slot name.
- **Bolsas**: backpack slots 23-38, then each equipped bag's contents.
- Collapsible **Banco**, **Chaveiro**, **Moedas** (only when non-empty), and
  **Talentos**, **Reputação**, and **Conquistas** as raw IDs (names come later
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
