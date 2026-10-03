# Live map — players and where they are

Status: **deployed and working** — live at http://192.168.1.64:9400, with 78 zone maps
extracted from the client, a per-player marker page, and a Grafana dashboard linking to
it. Alignment within zones is approximate (see § Alignment below).

## What is already proven

**The coordinate transform works.** `tools/wowmap/transform.py` converts a player's
`characters.position_x/y` into a normalised (0..1) position on that zone's world-map
image, using the DBCs TrinityCore's `mapextractor` already produced.

Output of `python3 tools/wowmap/transform.py <dbc dir>` (norm x is horizontal, left to
right; norm y is vertical, top to bottom; ×100 gives the in-game map coordinates):

| position | area | norm x | norm y |
|---|---|---|---|
| Blood Elf start (`playercreateinfo` race 10) | 3430 | 0.3797 | 0.2108 |
| character position (`characters.position_*`) | 3430 | 0.3780 | 0.2121 |
| Draenei start (`playercreateinfo` race 11) | 3524 | 0.8429 | 0.4302 |
| creature spawn min (`world.creature`) | 3524 | 0.4142 | 0.7339 |
| creature spawn max (`world.creature`) | 3524 | 0.2621 | 0.6160 |

The non-obvious part: the zone map is the `WorldMapArea.dbc` rect **turned on its
side**. The horizontal axis is world **Y** (fields 4/5) and the vertical axis is world
**X** (fields 6/7), as the client computes it:

    norm x = (field4 - Y) / (field4 - field5)
    norm y = (field6 - X) / (field6 - field7)

Before #109 the transform used world X for the horizontal axis. Every marker was then
transposed, e.g. Rubens at (0.215, 0.380) in the sea west of Sunstrider Isle instead of
on it at (0.380, 0.215). Checked after the fix by drawing known NPC spawns on the
extracted art: Innkeeper Farley in Goldshire (Elwynn Forest), Innkeeper Grosk in Razor
Hill (Durotar), Nazgrel in Thrallmar (Hellfire Peninsula), Innkeeper Keldamyr in
Dolanaar (Teldrassil) and Megelon in Ammen Vale (Azuremyst Isle). Their ×100 values match
the coordinates the game shows for them.

## Data sources

| What | Where | Freshness |
|---|---|---|
| position | `characters.characters.position_x/y/z`, `orientation` | `PlayerSaveInterval`, configured to **5000 ms (5 s)** |
| current map / zone | same row — `map`, `zone` | as above |
| instance | `instance_id` (non-zero = in a dungeon/raid) | as above |
| online flag | `characters.characters.online`, `auth.account.online` | on login/logout |
| zone names | `AreaTable.dbc` — **not in the DB**, the TDB has no `areatable` table | static |
| zone rects | `WorldMapArea.dbc` | static |

**Sampling resolution is the main knob.** With the configured 5 s interval, movement
samples are fresh enough for usable traces. Trade-off: one DB write per online player per
interval; 5 s is fine for a small realm, while 1 s would be wasteful.

## Rendering options

### A. Grafana native — partially possible

| Panel | Verdict |
|---|---|
| **XY Chart** | ✅ Works today. Scatter with x/y per series, one series per player. **No background map**, so it is "where in zone coordinates", not a map. |
| **Canvas** | ❌ Has a background-image element, but element positions are static/constrained — you cannot plot N markers at data-driven coordinates. |
| **Geomap** | ⚠️ Possible but heavy: fake lat/lon via a linear transform and serve the WoW map as a custom XYZ tile layer. Needs a tile pyramid and fighting Mercator. |
| **Text + HTML** | ❌ Interpolation is per-value, not per-row, so it cannot iterate players. |
| **Dynamic Text plugin** | ✅ The one genuinely viable Grafana-native path: renders data-driven HTML with iteration, so absolutely-positioned markers over a background image works. Grafana is 13.2.1 and the container has internet, so it can be installed from the catalog. |

### B. Purpose-built mini web app — recommended

A single page served from the wow-server VM:

- pick a map/zone, show the zone image as background
- online players as class-coloured markers, named labels on hover
- movement trails (last N samples)
- side list of who is online, their level/class/zone
- players in instances listed separately (`instance_id != 0` — they are not on the world map)

Then embed it in Grafana (Dynamic Text iframe or a link) so everything stays in one place,
or just use it standalone — it is a better UX for a live map than any panel.

**Why B over A:** the map wants live refresh, trails, per-player colour and labels, and
zone switching. Grafana's native panels fight all four; only the Dynamic Text plugin gets
close, and it still needs a background image service. A small app is less total work and
far more capable.

## The missing asset: map images

The transform is solved; the *pictures* are not. Options, easiest first:

1. **Extract from the client MPQs.** The client is on the VM
   (`/opt/wow-server/client/Data/*.MPQ`) and contains `Interface\WorldMap\**.blp`.
   `mpyq` (pure-Python MPQ reader) is available, so extraction is feasible; the BLP→PNG
   step is the work (BLP2 DXT payloads can be re-wrapped as DDS and decoded by Pillow).
   Fully self-contained, no third-party assets.
2. **Render terrain from the extracted `maps/*.map`.** No external asset at all — but it
   means writing a renderer for TrinityCore's binary map format, and the output is
   terrain, not the hand-drawn map art.
3. **Fetch public/community zone map images.** Fastest, but third-party and
   availability/licensing is on you.

Recommendation: start with (1). If BLP decoding turns painful, fall back to (3) to ship
the feature and revisit.

## Proposed phases

1. **Positions as metrics** (small). Expose `wow_player_pos_x{character,map,zone}` /
   `wow_player_pos_y` for online players only, from the existing exporter. Gives a working
   Grafana XY Chart immediately and starts accumulating history.
2. **Live map app** (the real feature). Map images + `/api/players` JSON + the page.
3. **Polish.** Trails, per-zone population over time, heatmap of where time is spent,
   embed into Grafana.

## What was built (actual deployment)

### Services

The live map runs as the `wowmap` container in the `monitoring` compose on the
wow-server VM (192.168.1.64), alongside node-exporter, cadvisor, and wow-exporter.

```
/opt/monitoring/docker-compose.yml    ← monitoring compose (in the repo: monitoring/)
  node-exporter   :9100 (host net)
  cadvisor        :8080
  wow-exporter    :9300 (game metrics)
  wowmap          :9400 (map page + API + art)    ← built from /opt/wowmap/
```

The `wowmap` service joins the `wow-server_default` network (external) so it can
reach `trinitycore-db` by container name — MySQL does not publish a host port.

### Map art extraction

The zone map art comes from the **locale MPQ** (`<locale>/locale-<locale>.MPQ`),
NOT from the base/patch MPQs, and it is NOT in the `(listfile)` — enumeration is
not possible. The filenames follow a discoverable pattern:

    Interface\WorldMap\<AreaName>\<AreaName><N>.blp

Where `<AreaName>` is the space-free name straight out of `WorldMapArea.dbc`
(e.g. `EversongWoods`), and `<N>` is a 1-based tile index. Every zone map is
**12 tiles of 256×256 = 4 columns × 3 rows = 1024×768**, assembled
left-to-right, top-to-bottom.

BLP2/DXT payloads are decoded by wrapping them in a minimal DDS header and
letting Pillow decompress that — no custom DXT decoder needed.

The extraction script is `tools/wowmap/extract_maps.py`. It requires:
- `mpyq` (pip-installable pure-Python MPQ reader — reads the `(hash table)` but needs filenames as input since the listfile lacks worldmap paths)
- `Pillow` (for DDS-in-Pillow decode + PNG output)
- The client MPQs from `/opt/wow-server/client/Data/<locale>/`

Run it on the VM:
```
/opt/wowmap-venv/bin/python /opt/extract_maps.py \
  --client /opt/wow-server/client --dbc /opt/wowmap-data/dbc \
  --out /opt/wowmap-data/maps
```

Extracted 78 zone maps; 27 zones had no art in the locale MPQ.

### The DBC field order (bug that cost hours)

`WorldMapArea.dbc` has four float fields. The natural guess is `(left, right,
top, bottom)` with left/right in world X. It is **not**. World X grows north and
world Y grows west, so the map's horizontal axis is world Y:

    Field 4 = left edge   (world Y, west, the larger value)
    Field 5 = right edge  (world Y, east)
    Field 6 = top edge    (world X, north, the larger value)
    Field 7 = bottom edge (world X, south)

`transform.py` keeps these in `rects[area] = (f6, f7, f4, f5)` under the historical
names `(left, right, top, bottom)`. Mind the names: there, `left`/`right` are the
**vertical** extent. Use the raw values as-is; `min()`/`max()` drops the orientation
and mirrors every marker. Until #109 the marker transform also put world X on the
horizontal axis, which transposed every marker.

Pass `WorldMapArea.dbc`, `AreaTable.dbc` (for zone names — the TDB has **no**
`areatable` table, so zone names cannot come from the game database), and
`Map.dbc` (for map names) to the constructor.

### Alignment

The rect from `WorldMapArea.dbc` covers the game's **1002×668** map frame
(FrameXML `WorldMapDetailFrame`), not the full 1024×768 tile sheet: the 4×3 tiles
overflow the frame at the right and bottom, and that strip is blank. So a normalised
position is multiplied by 1002×668 to get a pixel on the extracted PNG
(`transform.MAP_FRAME_W/H`, and `px()` in the page). Scaling by 1024×768 put markers up
to 100 px too low near the bottom of a zone (fixed with #109).

With the axes and the frame right, markers sit where the game shows them, and
`calibration.json` (per-zone pixel offsets, empty on the VM and in the repo when #109
was fixed) should not be needed. Keep it for art that really is offset.

Three earlier calibration attempts, from before the axis fix, are in `tools/wowmap/`:
- `overlay_test.py` — draw specific points on a map image
- `scatter_test.py` — dense scatter of creature spawns
- `calibrate.py` + `fit_transform.py` — texture-based auto-fit (overfit)

### API

| Endpoint | Returns |
|---|---|
| `GET /` | the map HTML page |
| `GET /api/players` | JSON: online players with name, level, class, race, zone, map, x/y/z, normalized coords |
| `GET /api/areas?map=<id>` | JSON: zone tiles with rect, name, whether an image exists |
| `GET /api/summary` | `{online, in_world, in_instance, zones}` |
| `GET /maps/<area_id>.png` | the zone map image (static, cached 24 h) |
| `GET /healthz` | `{"ok": true}` |

### Grafana

Dashboard `wow-live-map` in the `WoW Server` Grafana folder:
- Text panel linking to the live map page
- Stat panels: players online, accounts online, in instances
- XY scatter of `wow_player_position_x/y` (Grafana-native, no background art)

### Decisions made

- **Sampling interval** — set to 5 s with `TC_WORLD__PlayerSaveInterval=5000` for
  movement-trail and heatmap observability.
- **Map art** — extracted from the client MPQs (option 1, no third-party assets).
- **Home** — standalone page at :9400, **not** embedded in Grafana (Grafana
  HTML panels with sanitisation disabled could embed it, but the page is richer
  standalone).
- **Cardinality** — per-player positions in Prometheus are fine for the current
  population.
