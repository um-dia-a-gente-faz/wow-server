---
name: wowmap-dev
description: Work on tools/wowmap, the internal observability site (live map, character inspect drawer, JSON API) — run it locally against the live database, understand the coordinate transform and map art pipeline, use calibration mode, and test it. Use for any Console milestone issue or other change under tools/wowmap.
---

# Working on the observability site

`tools/wowmap/wowmap/` is the runtime (offline `pipeline/` is run by hand, never shipped). It is a FastAPI app (ADR 0012, #323): `api.py` holds the typed `/api/v2/` routes and the SSE stream, plus a catch-all that answers every other GET/POST through `routes.dispatch`; `app.py` starts `api.app` under uvicorn. `routes.py` is the route table, SQL lives in `repo/`, and the HTML/CSS/JS lives in `static/` (one file per panel, `index.html` is the template `pages.py` fills in). The page has no build step and no framework; keep it that way (the SvelteKit front end is ADR 0012's later step, not this).
The map stage is Leaflet (`L.CRS.Simple`, map units = zone-art pixels), vendored in
`tools/wowmap/wowmap/static/` and served under `/static/`; never load it from a CDN.
`tools/wowmap/README.md` documents the endpoints and the bag/slot convention;
`docs/LIVE-MAP.md` documents how the map was built.

## Run it locally

```bash
cd tools/wowmap/wowmap
pip install -r ../requirements.txt              # pymysql, Pillow, fastapi, uvicorn (hash-locked)
export MYSQL_HOST=192.168.1.64 MYSQL_USER=root MYSQL_PASSWORD=...   # from the VM's .env
export DBC_DIR=/path/to/dbc MAPS_DIR=/path/to/maps LISTEN_PORT=9401
python3 app.py                                  # uvicorn on 0.0.0.0:9401, then open http://localhost:9401
```

`app.py` is `uvicorn.run(api.app, ...)`; there is no other server. Without a reachable
MySQL the process still starts (`/healthz` answers 200), but the DB-backed routes return 500
(checked: `/api/v2/players`, `/api/players`) and the log shows the connection error.

## The API spec (`openapi.json`)

`tools/wowmap/openapi.json` is the checked-in spec of the `/api/v2/` routes (the old
routes are `include_in_schema=False` and are not in it). A running server serves the same
document at `GET /openapi.json`. `tools/wowmap/tests/test_api.py` fails when the file
differs from `api.app.openapi()`; its message prints the regeneration command. Run it from
the repo root after changing a v2 route or the `Player` model in `api.py`, then commit the file:

```bash
cd tools/wowmap/wowmap && python3 -c "import api, json; print(json.dumps(api.app.openapi(), indent=2, sort_keys=True))" > ../openapi.json
```

The command needs the runtime requirements installed (it imports `api`, so FastAPI and
pydantic must be present). Running it on an unchanged tree rewrites the file byte for byte.

### Against a working agent, no live realm (dev stack, #481)

`dev/dev.sh up` starts wowmap on http://127.0.0.1:9400 with one mock agent (Luaprata),
chat-feed on 9500 and the agent API on 9601, all on a fixture DB, with no credentials.
Use it to check an API, agent-mind or activity change end to end before the live VM.
`dev/dev.sh smoke` boots it and waits for a decision in wowmap's API; `dev/dev.sh down`
removes it. Details: `docs/DEV-STACK.md`. The stack has no client DBCs or map art, so
judge map layout and the position transform on the live site, not here.

Reading the live database is fine; never write to it. On the VM the service runs from
`monitoring/docker-compose.yml` with `/opt/wowmap-data/{dbc,maps,icons,models}` and the
directory `/opt/wowmap-data` mounted at `/app/data`, and the site is served at http://192.168.1.64:9400.

## Data sources

| What | Where from |
|---|---|
| Player positions, level, money, inventory, reputation | `characters` database (saved every 5 s) |
| Item names, stats, sell price | `world.item_template` (38k rows) |
| Zone rectangles, names, continents | client DBCs via `transform.py` (`WorldMapArea`, `AreaTable`, `Map`) |
| Zone art | `pipeline/extract_maps.py`, from the client MPQs → per-zone PNG |
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
