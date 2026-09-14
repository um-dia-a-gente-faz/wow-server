# Live map — players and where they are

Status: **deployed and working** — live at http://192.168.1.64:9400, with 78 zone maps
extracted from the client, a per-player marker page, and a Grafana dashboard linking to
it. Alignment within zones is approximate (see § Alignment below).

## What is already proven

**The coordinate transform works.** `tools/wowmap/transform.py` converts a player's
`characters.position_x/y` into a normalised (0..1) position on that zone's world-map
image, using the DBCs TrinityCore's `mapextractor` already produced.

Validated against two independent datasets — 5/5 positions land inside their zone rect:

| position | area | norm x | norm y |
|---|---|---|---|
| Blood Elf start (`playercreateinfo` race 10) | 3430 | 0.7892 | 0.3797 |
| character position (`characters.position_*`) | 3430 | 0.7879 | 0.3780 |
| Draenei start (`playercreateinfo` race 11) | 3524 | 0.5698 | 0.8429 |
| creature spawn min (`world.creature`) | 3524 | 0.2661 | 0.4142 |
| creature spawn max (`world.creature`) | 3524 | 0.3840 | 0.2621 |

The non-obvious part: in `WorldMapArea.dbc` the four float fields are **not** in
`(left, right, top, bottom)` order — the **Y extent comes first** (fields 4/5), then X
(fields 6/7). Reading them the natural way puts *every* real position outside its zone.
See the module docstring for the full layout.

## Data sources

| What | Where | Freshness |
|---|---|---|
| position | `characters.characters.position_x/y/z`, `orientation` | `PlayerSaveInterval`, default **90000 ms (90 s)** |
| current map / zone | same row — `map`, `zone` | as above |
| instance | `instance_id` (non-zero = in a dungeon/raid) | as above |
| online flag | `characters.characters.online`, `auth.account.online` | on login/logout |
| zone names | `AreaTable.dbc` — **not in the DB**, the TDB has no `areatable` table | static |
| zone rects | `WorldMapArea.dbc` | static |

**Sampling resolution is the main knob.** At the 90 s default, a running player moves
~600 yards between samples — enough to see *which area* someone is in, useless as a path.
Setting `TC_WORLD__PlayerSaveInterval=5000` (5 s) in `docker-compose.yml` gives real
movement traces. Trade-off: one DB write per online player per interval; 5 s is fine for
a small realm, 1 s would work but is wasteful.

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
top, bottom)`. It is **not**. The actual layout:

    Field 4 = top    (world Y, north edge)
    Field 5 = bottom (world Y, south edge)
    Field 6 = left   (world X)
    Field 7 = right  (world X)

Crucially, **`left > right`** in this DBC — the image X axis runs opposite to
world X. Using `min()`/`max()` to normalise the range **silently mirrors every
marker horizontally**. The transform in `transform.py` uses the raw values as-is
and the formula `(world_x - left)/(right - left)` handles the inversion
naturally.

Pass `WorldMapArea.dbc`, `AreaTable.dbc` (for zone names — the TDB has **no**
`areatable` table, so zone names cannot come from the game database), and
`Map.dbc` (for map names) to the constructor.

### Alignment

The rect from `WorldMapArea.dbc` does not linearly cover the full 1024×768 tile
sheet — Blizzard's zone art includes decorative borders and sea beyond the rect.
The naive 0..1 mapping puts markers in the right zone but with an offset that
varies per map. In practice: the player shows up in the correct zone, and the
offset is not huge. A per-zone calibration step (manual or auto-fitted from
creature spawns) would tighten it.

Three calibration attempts documented in `tools/wowmap/`:
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

- **Sampling interval** — kept at 90 s default. Reduce with
  `TC_WORLD__PlayerSaveInterval=5000` when movement trails are built.
- **Map art** — extracted from the client MPQs (option 1, no third-party assets).
- **Home** — standalone page at :9400, **not** embedded in Grafana (Grafana
  HTML panels with sanitisation disabled could embed it, but the page is richer
  standalone).
- **Cardinality** — per-player positions in Prometheus are fine for the current
  population.
