---
name: wowmap-dev
description: Work on tools/wowmap, the internal observability site (live map, character inspect drawer, JSON API) — run it locally against the live database, understand the coordinate transform and map art pipeline, use calibration mode, and test it. Use for any Console milestone issue or other change under tools/wowmap.
---

# Working on the observability site

`tools/wowmap` is a single-file stdlib `http.server` app (`app.py`, ~1000 lines) with the
HTML/CSS/JS embedded as strings — no build step, no framework. Keep it that way.
`tools/wowmap/README.md` documents the endpoints and the bag/slot convention;
`docs/LIVE-MAP.md` documents how the map was built.

## Run it locally

```bash
cd tools/wowmap
pip install -r requirements.txt                 # pymysql, Pillow
export MYSQL_HOST=192.168.1.64 MYSQL_USER=root MYSQL_PASSWORD=...   # from the VM's .env
export DBC_DIR=/path/to/dbc MAPS_DIR=/path/to/maps LISTEN_PORT=9401
python3 app.py                                  # then open http://localhost:9401
```

Reading the live database is fine; never write to it. On the VM the service runs from
`monitoring/docker-compose.yml` with `/opt/wowmap-data/{dbc,maps,calibration.json}`
mounted, and the site is served at http://192.168.1.64:9400.

## Data sources

| What | Where from |
|---|---|
| Player positions, level, money, inventory, reputation | `characters` database (saved every 5 s) |
| Item names, stats, sell price | `world.item_template` (38k rows) |
| Zone rectangles, names, continents | client DBCs via `transform.py` (`WorldMapArea`, `AreaTable`, `Map`) |
| Zone art | `extract_maps.py`, from the client MPQs → per-zone PNG |
| Public chat | `tools/chat-feed` SSE on :9500 |
| Agent decisions | the audit JSONL under `/opt/wow-server-metrics/audit` |

wowmap reads `AreaTable`, `Map`, `WorldMapArea` and `WorldMapOverlay` from
`/opt/wowmap-data/dbc`. `<area_id>.png` is the zone fully explored (base parchment +
every `WorldMapOverlay` texture); `<area_id>_base.png` is the unexplored parchment.
Subzone rects/names come from `overlays.py` and are image pixels, not world coordinates.
`overlays/<overlay_id>.png` is each overlay's own art; `fog.py` stacks the ones a
character has explored (`characters.exploredZones`) on the base art.

## Coordinates

`transform.py` turns world X/Y into a normalised position inside a zone's WorldMapArea
rect; per-zone pixel offsets live in `calibration.json` and are editable in the page's
calibration mode (drag a marker, save via `POST /api/calibrate`). If markers sit slightly
off, fix the calibration, not the transform.

## Testing

- `python3 -m unittest discover -s tools/wowmap/tests`
- `curl -s http://192.168.1.64:9400/api/character/Rubens | python3 -m json.tool` —
  Rubens is the only character with real gear; agent characters own nothing until they loot.
- `curl -s "http://192.168.1.64:9400/api/areas?map=530"` — zones, rects, art availability.
- Compare anything user-facing against the in-game window it imitates; the owner will.
- Render all user-supplied strings with `textContent`, never `innerHTML` (chat and item
  names are untrusted).
