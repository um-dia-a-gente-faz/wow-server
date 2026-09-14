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
  "gender": 0,
  "zone": 1637,
  "zone_name": "Orgrimmar",
  "map": 1,
  "position_x": 1502.3,
  "position_y": -4415.2,
  "position_z": 22.1,
  "orientation": 1.2,
  "money_gold": 123.45,
  "totaltime": 86400,
  "logout_time": 1710000000,
  "inventory": [{"bag": 0, "slot": 0, "item_name": "Example Item", "count": 1}],
  "talents": [{"spell": 12345, "spec": 0}],
  "reputation": [{"faction": 72, "standing": 42000}],
  "achievements": [{"achievement": 6, "date": 1710000000}]
}
```
