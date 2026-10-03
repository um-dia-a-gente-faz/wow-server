# Map engine spike (UM-77)

Date: 2026-10-03. Issue: UM-77 (GitHub #80). Feeds UM-78 (click-to-zoom navigation,
GitHub #79). The owner wants the console map to behave like the in-game world map and
floated "our own map engine". This note decides the v2 approach before it is built.

Every claim is tagged:

- **[V]** verified, with how (ran it, read the DBC/MPQ, measured).
- **[E]** estimate, derived from a measurement but not itself measured.
- **[U]** unverified: read in docs or reasoned, not tried here.

## Recommendation

1. **Stay on Leaflet. Do not adopt MapLibre.** A 25-marker page needs nothing MapLibre
   adds, and it costs a 1.1 MB ESM-only bundle, a Web-Mercator coordinate trick and a
   font server for labels (section 3).
2. **In-game navigation (UM-78): do it with the extracted continent art and the zone art
   nested inside the continent frame**, in one Leaflet scene. One coordinate transform
   serves both levels, the zoom is continuous, and a prototype of it works (section 1).
   Two things need real work that the happy path hides: **zone hit-testing** (the
   `WorldMapArea` rects overlap too much to click on) and **seven zones whose rect does
   not fit their continent** (Eversong, Ghostlands, Silvermoon, Sunwell, Azuremyst,
   Exodar, Bloodmyst).
3. **"Our own engine" = our own data pipeline on top of Leaflet** (continent art,
   zone hit regions, minimap tiles, labels). No engine is written.
4. **Satellite (minimap tiles) is a cheap, separate second view, worth building after
   UM-78, not before.** Extraction is proven and fast (all of Kalimdor in 8 s, 11.7 MB as
   a tile pyramid) and it needs no per-zone data at all. It sits behind a toggle on the
   same map (section 4). It is a different product (terrain, no names), not a
   replacement for the painted map.

The owner decisions this needs are listed at the end.

## What was and was not done

- **Done, with a local 3.3.5a enUS client (read-only):** read the DBCs out of the MPQs,
  extracted continent and zone art with the repo's own `extract_maps.py` code,
  extracted all 3,636 minimap tiles of the four continents and built a Kalimdor pyramid,
  built a static prototype (`docs/map-engine-spike/prototype.html`) and a benchmark page
  (`bench.html`), and drove both in headless Chrome 154.
- **Not done:** nothing ran against the live server or database (the prototype uses
  stub players); MapLibre was only built for the benchmark, not for the painted view; no
  Linear/GitHub issues were created.
- **Whether the local client is byte-identical to the VM's** `/opt/wow-server/client` is
  **[U]**. The pipeline code is the same, so the art should be.
- **Screenshots are not committed.** This repository is public and `CLAUDE.md`/`.gitignore`
  say extracted client art never goes in it; a screenshot is a copy of that art. The
  results are written out below, and `proto_check.mjs` regenerates every screenshot
  locally. If the owner wants a few low-resolution ones in the PR for review, say so.

## How this fits the open PRs

| PR | What it does | Where this spike depends on it |
|---|---|---|
| #116 / #117 (merged) | coloured `WorldMapOverlay` zone art, marker axes fix | the zone art (`<area_id>.png`) and the transform this builds on |
| #123 (UM-72, open) | moves the stage to Leaflet `L.CRS.Simple`, one zone at a time, units = zone-art pixels (1002x668 frame in a 1024x768 sheet), vendored Leaflet 1.9.4 | **UM-78 builds on it.** The prototype uses the same conventions (frame, sheet, `CRS.Simple`, Leaflet 1.9.4) so it ports directly, but UM-78 changes the unit from "zone-art pixels" to "continent-art pixels" (section 1). That touches #123's `ll()`, `FRAME`, `fitZone()` and the calibration offsets (stored in zone-art pixels, they must be scaled by rect size / frame size). Do not start UM-78 before #123 merges. |
| #124 (UM-79, open) | fog of war: base art plus only the character's revealed overlays via `artUrl(a)` and `/maps/<id>.png?explored=` | the nested zone art in UM-78 must be fetched through `artUrl(a)`. The continent art is one finished image (no overlays exist for the four continent rows **[V]**: `WorldMapOverlay.dbc` has no rows for them), so **fog applies at zone level only**. (I believe the in-game continent map is always coloured too; not checked in the game **[U]**.) |

## 1. In-game navigation: continent art plus zone art in Leaflet

### What the client has

- **[V] Continent art exists and uses the same format as zone art.** `Interface\WorldMap\
  {Kalimdor,Azeroth,Expansion01,Northrend}\<Name><1..12>.blp`: 4x3 tiles of 256 px, a
  visible 1002x668 frame in a 1024x768 sheet. Extracted and viewed. Folder `Azeroth` is
  the **Eastern Kingdoms** art (labelled so), `Expansion01` is **Outland**.
  `build_art.py` writes them with `extract_maps.base_sheet()`, unchanged.
- **[V, by reading the code] `extract_maps.py` does not keep them today.** It keys output by
  `WorldMapArea` AreaID, and the four continent rows all have AreaID 0 ("the last row
  wins"), so at most one continent lands in `0.png`. UM-78 needs them keyed by name.
- **[V] The continent rects are in `WorldMapArea.dbc`, not `WorldMapContinent.dbc`.**
  Four rows with AreaID 0: Kalimdor (id 13, map 1), Azeroth (14, map 0), Expansion01
  (466, map 530), Northrend (485, map 571). Same field layout as the zones (fields 4/5
  world Y, 6/7 world X), e.g. Kalimdor Y 17066.6 to -19733.2, X 12799.9 to -11733.3.
  `WorldMapContinent.dbc` (4 rows, 14 fields, read from the MPQ) holds an offset, a scale
  and taxi bounds. What each column means is **[U]** and none of it is needed.
- **[V] There is no coordinate system above the continents.** `Cosmic` (Azeroth and
  Outland as two painted discs) and `World` (all continents) art both exist in the MPQ,
  but no `WorldMapArea` row exists for either, so there is no rect and no transform.
  Their click targets would have to be authored by hand. UM-78 calls this a stretch goal;
  that is right.

### Does continent art compose with zone art?

- **[V] The same transform works, with the continent's rect.** Normalised position on the
  continent is `((Y1 - Y) / (Y1 - Y2), (X1 - X) / (X1 - X2))` using the continent row;
  a zone's rect run through it gives the zone's box in the continent frame. All 21
  Kalimdor and 25 Eastern Kingdoms zone rects fall inside their continent frame
  (computed from the DBC). Stub players placed at known towns land on the right place of
  the continent art (Orgrimmar at the north tip of Durotar, Thunder Bluff in Mulgore,
  Crossroads in the Barrens, Dolanaar and Darnassus on Teldrassil), checked by eye in the
  prototype.
- **[V, by eye] Zone art nests onto continent art to within a few pixels at continent
  scale.** Teldrassil, Durotar and Mulgore overlaid at 3x: coasts and the zone shape
  line up. I tried to turn this into a number with a cross-correlation fit and it was not
  reliable even for control zones (parchment textures differ), so there is **no measured
  offset**, only the visual check.
- **[V] Seven zones do not fit.** Zones whose `WorldMapArea` field 8 (DisplayMapID) is
  set live on map 530 but are drawn on a continent: Eversong Woods, Ghostlands,
  Silvermoon City, Sunwell (Eastern Kingdoms), Azuremyst Isle, The Exodar, Bloodmyst
  Isle (Kalimdor). Their rects are in map 530 space. Run through the Kalimdor rect,
  Azuremyst and Bloodmyst land in open sea **east** of Kalimdor, while the two islands
  west of Darkshore are where the art draws them (by eye; I did not overlay their zone art
  on those islands); Eversong and Ghostlands land visibly (tens of pixels) right of their
  art. These are exactly the seven zones that fall outside the Outland frame. One more
  edge case: Hrothgar's Landing is outside the Northrend frame. These need a hand-fitted
  placement per zone. The page's calibration mode shifts *markers* per zone, not where a
  zone sits on a continent, so this needs a similar small per-zone store of its own.
- **[V] Zone rects are bounding boxes, and they overlap heavily.** Share of the covered
  continent area lying in two or more zone rects: Kalimdor 52%, Eastern Kingdoms 40%,
  Northrend 40%, Outland 35% (200x200 sample grid over the DBC rects). A "smallest rect
  wins" hit-test is therefore wrong a lot: in the prototype a click inside Mulgore
  returns `Desolace < Mulgore < Barrens` (smallest first), i.e. Desolace. The hover
  highlight draws the whole bounding box (Durotar's box covers half the Barrens). The
  existing subzone hover in wowmap already uses "smallest rect", which is fine inside
  one zone and not at continent level.

### What click-to-zoom looks like (prototype, `prototype.html`)

Driven in headless Chrome 1280x800 with `proto_check.mjs`. **[V]** for every line.

- Continent view: Kalimdor art fitted (zoom 0.26), 25 stub players as dots, hover draws a
  gold box and the zone name.
- Click Durotar: `flyToBounds` (0.9 s) to zoom 2.97, where the zone's rect fills the view.
  The page reports `level: zone Durotar`, Durotar art at opacity 1. Zoom range is 3.2
  levels (about 9x) which is where the zone art reaches 1:1.
- Crossfade is driven by zoom: opacity 0.15 / 0.55 / 0.95 / 1 at 1.2 / 0.8 / 0.4 / 0
  levels out from the fit. Only the zone under the view centre fades in.
- Right-click (or the "Continent" button) flies back out. A satellite toggle keeps the
  world position and yards-per-pixel scale (section 4).
- **Players use one transform for both levels** (continent rect only). No per-zone
  transform is needed in the page; the current `norm_x/norm_y` per-zone path would be
  retired for the painted view.
- Visible rough edges, not fixed: the zone art carries its own decorative frame, which
  shows as a ghost box at partial opacity; and the continent art, upscaled about 9x, is
  blurry around the zone. Fading the continent art out entirely at zone level, and
  cropping the frame, are the fixes **[U]**.

### What this means for UM-78

- Painted continent/zone navigation is feasible in Leaflet with no new library **[V]**.
- Hit-testing is the real work. Options:
  1. **Rect, smallest first.** Works in the prototype, wrong where rects overlap (above).
  2. **Per-zone regions from the server's own area data.** `transform.GridAreas` already
     reads `maps/*.map` AREA grids (about 33 yard cells) and `AreaTable.dbc` has each
     subzone's parent zone, so one low-resolution raster per continent could give exact
     zone borders. Cost/benefit **[U]**: not tried, there are no server map files locally.
  3. **The client's own `<Zone>Highlight.blp`.** They exist (`DurotarHighlight.blp`,
     128x128, **[V]** decoded: soft zone-shaped glows meant for additive blending), good
     for the hover glow. Where the client places them is client-internal **[U]**.
  Recommended: 2 for the click target, 3 for the hover glow if placement can be found.
- Dalaran and the instance rows (Wrath dungeons, 16 rows) have no art or are floors;
  keep them out of the continent view, as the zone extractor already does.

## 2. Satellite tiles from the client minimap

### Extraction is available and fast

- **[V] The data is in the client.** `Textures\Minimap\md5translate.trs` (patch-3.MPQ,
  1.5 MB, 19,089 lines, 63 directories that have tiles) maps `<Dir>\map<col>_<row>.blp` to
  `Textures\Minimap\<md5>.blp`, which live in `common.MPQ` (checked on a sample of 40
  Kalimdor tiles). Each tile is a 256x256 DXT1
  BLP2 (about 34 KB), so **the existing `extract_maps.blp_to_image()` decodes them with no
  change**.
- **[V] Counts.** Kalimdor 1,018 tiles, Eastern Kingdoms 687, Outland 800, Northrend 1,131
  (3,636 in all).
- **[V] Time.** 3,636 tiles read and decoded to PNG in about 40 s on a fast desktop
  (6.7 ms a tile). The full Kalimdor pyramid (below) took 8 s.
- **[V] Sizes.** Kalimdor as a JPEG q80 pyramid z0..z6: 1,420 tiles, **11.7 MB** (z6
  native 8.4 MB, 1,018 tiles; z5 2.5 MB; the rest 1 MB). PNG of the native tiles alone is
  45 MB for Kalimdor, 125 MB for the four continents. JPEG q80 is visually fine.
  **[E]** All four continents as a JPEG pyramid: about 42 MB and about 30 s (scaled by
  tile count from the Kalimdor run).
- Not extracted: instances/battlegrounds (the other 59 directories). Not needed.

### Coordinates: one global linear transform, no zone data

**[V]** `col = 32 - Y/533.3333`, `row = 32 - X/533.3333` (fractional tile coordinates;
`map<col>_<row>` is the ADT, the same `32 - v/533.33` the server's grid files use, which
`transform.GridAreas` already encodes). The first number in the file name is the
east-west index, the second the north-south one. Verified by stitching Kalimdor and
plotting six known towns: Orgrimmar, Thunder Bluff, the Crossroads, Ratchet, Dolanaar and
Darnassus each landed on their town. The known-coordinates list is from memory, so this is
"by eye on recognisable terrain", not a numeric check.

Grid: 64x64 tiles of 256 px = 16,384 px square at native zoom. A tile is one ADT
(533.33 yards), so native resolution is 0.48 px per yard **[V, arithmetic]**. For comparison
the painted art of an ordinary zone is 1002 px over 4,000 to 10,000 yards, i.e. 0.1 to 0.25
px per yard (Durotar 0.19, Barrens 0.10): satellite is 2x to 5x sharper at its deepest
zoom, and it has no frame, no per-zone rect and no calibration.

### How players and trails look on them

**[V]** Rendered in the prototype. Dots and 60-point polylines stay legible on both the
dark terrain and the bright desert tiles with a white 2 px outline. Things to know:

- It is terrain only. **No place names, no road names, no borders, no zone colouring.**
  Anything textual has to be drawn by us (section 3). `AreaTable.dbc` names and the
  subzone label points wowmap already computes can feed it **[U]**: those points are in
  zone-art pixels and would be converted back to world coordinates.
- Some tiles have black holes (city interiors the minimap does not render) and a coloured
  fog halo around coasts (a purple band off the Durotar coast). Both are in the client's
  own data.
- Tiles are uniformly lit; there is no fog of war for them. A mask from the
  explored-area rects is conceivable **[U]**.
- Pyramid parents need the sea colour as filler, not black. `build_art.py` takes it from the
  flattest native tile (RGB 0,29,40) **[V]**.
- One hairline seam showed between tiles in one screenshot at zoom 4.03 **[V, seen once]**.
  Not investigated.

## 3. Leaflet vs MapLibre

Same data in both: the Kalimdor tile pyramid as a raster source, N markers moving.
`bench.html` + `bench_run.mjs`, headless Chrome 154, 1280x800, 20-thread desktop with an
RTX 4070 SUPER. Absolute numbers are far better than a phone or old laptop will give;
use them for ordering and for where each library falls over. Each cell is a fresh page
load. "Update" is JS time to move every marker once. "Frames" are 4 s with a 6 px pan and
**every marker moved every frame**, a far worse load than the real page (an update every
5 s, idle in between). "Busy" is main-thread task time as a share of wall time.

### Positions agree

**[V]** The Mercator trick below was checked: five marker screen positions in MapLibre
(z4) and Leaflet (z5, 128 px per tile) match to within 1 px.

### Results

GPU, no throttle:

| lib / marker type | N | update p50 / p95 ms | fps | busy % |
|---|---|---|---|---|
| Leaflet, DOM `divIcon` (what wowmap uses) | 25 | 0.0 / 0.2 | 60 | 10 |
| | 500 | 0.3 / 0.8 | 60 | 46 |
| | 2000 | 1.0 / 1.6 | 60 | 94 |
| Leaflet, canvas `circleMarker` | 25 | 0.1 / 0.3 | 60 | 9 |
| | 2000 | 0.4 / 1.6 | 60 | 25 |
| MapLibre, DOM `Marker` | 25 | 0.1 / 0.6 | 60 | 26 |
| | 500 | 0.7 / 1.2 | 60 | 53 |
| | 2000 | 2.5 / 3.3 | 46 | 100 |
| MapLibre, GeoJSON circle layer (GL) | 25 | 0.0 / 0.1 | 60 | 33 |
| | 2000 | 0.1 / 0.9 | 60 | 32 |

CPU throttled 6x (CDP) to stand in for a weak laptop:

| lib / marker type | N | fps | busy % |
|---|---|---|---|
| Leaflet DOM | 25 / 500 | 60 / 55 | 17 / 99 |
| Leaflet canvas | 25 / 500 | 60 / 60 | 16 / 30 |
| MapLibre DOM | 25 / 500 | 60 / 26 | 47 / 100 |
| MapLibre GL layer | 25 / 500 | 60 / 60 | 63 / 72 |

Software GL (SwiftShader, no GPU): every 25-marker case holds 60 fps; MapLibre GL layer
500 markers also holds 60 fps; Leaflet **canvas** at 500 dropped to 37 fps (software
compositing of a viewport-size canvas).

Reading: **at 25 markers all four hold 60 fps even throttled 6x**, so performance does not
pick the engine. Leaflet is the cheapest at idle-ish loads; MapLibre's WebGL layer is the
only one that stays flat at 2,000 markers; MapLibre's DOM markers are the worst at scale.
We have about 25 players.

### The rest of the comparison

| | Leaflet 1.9.4 | MapLibre GL JS 6.11.2 |
|---|---|---|
| Bundle **[V]** (npm tarball) | `leaflet.js` 147,552 B (42 KB gzip), css 15 KB | `maplibre-gl.mjs` + `-shared.mjs` + `-worker.mjs` about 1.12 MB (about 300 KB gzip), css 83 KB |
| No build step | plain `<script>`, already the #123 plan **[V]** | 6.11.2 ships **only ES modules** (`.mjs`, no UMD file in the tarball). `<script type="module">` from static files works, worker included **[V]** (bench.html). Earlier majors had a UMD build **[U]** |
| Coordinates | `L.CRS.Simple`, pixel units, matches #123 | Web Mercator only. A square tiled image maps to the whole Mercator world; markers use `MercatorCoordinate(col/64, row/64).toLngLat()` **[V]**. Works, but every coordinate in the page goes through an indirection |
| Raster tiles | `L.tileLayer`, over-zoom past native via `maxNativeZoom` **[V]** | `raster` source; `tileSize` 256 draws a z tile at 1:1 on map zoom z+1 (512 px world), a gotcha that cost one wrong benchmark run **[V]** |
| Painted nested art | `L.imageOverlay` + `setOpacity` (about 10 lines, the prototype) **[V]** | `image` source with four lng/lat corners and zoom-interpolated `raster-opacity`, declarative and nicer **[U]**, not built |
| Labels | HTML `divIcon`/tooltip, can use any web font (the in-game Friz Quadrata look), already the wowmap approach | `symbol` layers need a `glyphs` URL serving SDF font PBFs (needs a font tool and a server route) **[U]** |
| Hover/zone polygons | needs our own hit regions (section 1) | `feature-state` makes highlighting easy, but needs polygons we do not have **[U]** |
| Many markers | fine to about 500 DOM or several thousand canvas **[V]** | flat at thousands **[V]** |
| Work already done | #123, #124 and the page's marker/drawer code assume Leaflet | rewrite of the stage |

**Verdict: Leaflet.** MapLibre's advantages (marker scale, declarative raster fades,
vector labels) are real but pay out above a population we do not have, and its costs
(bundle, Mercator, ESM-only, glyph server, rewrite of #123/#124's stage) land now.
Revisit if the realm grows past a few hundred simultaneous markers or vector zone
polygons with hover states become the main feature.

## 4. Can both views coexist behind a toggle?

**[V] Yes, on one Leaflet map in the prototype.** The page keeps a player's world position
(`x`, `y`, map) and projects it with a per-view function:

- painted: continent rect, units = continent-art pixels (1002x668 frame);
- satellite: `lng = (32 - Y/533.33) * 4`, `lat = -(32 - X/533.33) * 4` (4 units per tile,
  so 64 tiles = 256 units = z0 of the pyramid).

Toggling swaps layers, resets min/max zoom and bounds, and re-places every marker and
trail from stored world positions. The toggle also **keeps the centre world position and
the yards-per-pixel scale**: from the continent view at zoom 0.26 it opens the satellite
at zoom 2.12, and from satellite zoom 6 at Orgrimmar it returns to painted at its maximum
(3.46) on Orgrimmar. Trails stay on the right ground.

Things that must be solved in the real page, not shown here:

- Maps: painted needs a continent and zone art per `map_id`; satellite needs one pyramid
  per continent directory (`Kalimdor`, `Azeroth`, `Expansion01`, `Northrend`). Map 530
  players in Eversong/Azuremyst belong to the Expansion01 tiles in satellite but to
  Eastern Kingdoms/Kalimdor art in painted: the seven display-map zones again.
  Instance players are not on any of them (unchanged).
- Fog of war (#124) is a painted-zone-only feature. Satellite shows everything.
- **A leaflet gotcha found while building it:** `map.setMinZoom()` and `setMaxBounds()`
  both schedule an asynchronous re-zoom/pan with the *old* zoom, which overrides a
  following `setView({animate:false})`. Assign `map.options.minZoom/maxZoom` directly and
  call `setMaxBounds(null)` before `setView`, then set the real bounds. (Fixed in
  `prototype.html`.)

## Effort (estimates, working days for one author agent, all [E])

| Slice | Contents | Days |
|---|---|---|
| Continent art + scene (UM-78 core) | extractor keyed by name, `/api/areas` continent rects, nested zone art, single transform, crossfade, right-click/button out, markers on both levels, tests | 3 to 4 |
| Zone hit regions | raster from server grids (option 2) or accept rects with a priority list, hover glow | 2 to 3 (rects only: 0.5) |
| Seven display-map zones | hand-fitted placement + a way to store/edit it | 1 |
| Satellite view | pyramid script on the VM, `/tiles` route (static, cacheable), toggle, view sync | 2 to 3 |
| Satellite labels | zone/subzone names from existing data in world coordinates | 1 to 2 |
| Cosmic/World level (stretch) | hand-authored hotspots, no transform | 1 to 2 |

Painted navigation total: about 6 to 8 days; satellite about 3 to 5 more. Disk on the
VM: about 42 MB of tiles **[E]**, plus about 5 MB of continent art **[V]** (five PNGs,
5.3 MB measured). The first satellite load fetches a few dozen tiles; wowmap's
`ThreadingHTTPServer` is enough, with the same `Cache-Control` the art uses.

## Proposed follow-up issues (not created)

1. **Continent art for the console map**: extract continent PNGs by name (fix the
   AreaID-0 collision in `extract_maps.py`), expose continent rects and per-zone continent
   boxes in `/api/areas`. Depends on #123.
2. **UM-78 refined: nested continent/zone scene in Leaflet**: one frame and one transform
   per continent, zoom-driven crossfade, click zone to zoom in, right-click or button to
   zoom out, markers and the follow-a-player behaviour across levels. Depends on #123;
   zone art through `artUrl()` from #124.
3. **Zone hit regions for the continent view**: build a per-continent zone raster from the
   server's `maps/*.map` area grids plus `AreaTable` parents (rects as fallback), and use
   `<Zone>Highlight.blp` for the hover glow if its placement can be found.
4. **Place the seven display-map zones on their continents**: hand-fitted offsets for
   Eversong, Ghostlands, Silvermoon, Sunwell, Azuremyst, Exodar and Bloodmyst (and Hrothgar's
   Landing), stored like `calibration.json`.
5. **Satellite view: minimap tile pipeline and toggle**: `extract_minimap.py` (productise
   `build_art.py`'s pyramid code), `/tiles` route, Map/Satellite toggle that preserves
   position and scale, trails on both.
6. **Satellite labels**: draw zone and subzone names on the satellite view from
   `AreaTable`/`WorldMapOverlay` data converted to world coordinates.
7. **Azeroth/Cosmic navigation (stretch)**: hand-authored click targets for the `World`
   and `Cosmic` art, which have no `WorldMapArea` rows.
8. **Revisit MapLibre if the realm grows**: only if simultaneous markers pass a few
   hundred or vector zone polygons become central. No work until then.

## Decisions for the owner

- **Is the satellite view wanted at all?** It is cheap (above) but it is a different map
  from the one the game shows. If the goal is "feels like the in-game map", UM-78 alone
  delivers it.
- **Hit regions:** are rects with a priority list acceptable for a first version, or does
  clicking Mulgore have to select Mulgore (option 2, more days)?
- **Confirm Leaflet** (this note's recommendation) before UM-78 is refined.
- **Screenshots:** may low-resolution ones go in PR discussion? They are copies of
  client art in a public repository.

## Reproduce

Needs a 3.3.5a client, `mpyq` and `Pillow`, Node 22+ and Chrome. Keep every output
directory **outside the repository**.

```bash
# 1. art, DBCs, data.json and the Kalimdor tile pyramid (about 20 s)
PYTHONPATH=<dir with mpyq.py> python3 docs/map-engine-spike/build_art.py \
    --client "/path/to/World of Warcraft 3.3.5a" --out /tmp/spike-out \
    --minimap Kalimdor --zones Durotar,Mulgore,Barrens,Teldrassil,Darkshore,Ashenvale

# 2. the pages and libraries (Leaflet 1.9.4 from npm; once #123 merges, copy
#    tools/wowmap/static/leaflet.* instead; MapLibre only for bench.html)
cp docs/map-engine-spike/*.html /tmp/spike-out/ && mkdir -p /tmp/spike-out/vendor
# put leaflet.js, leaflet.css (and images/) into /tmp/spike-out/vendor;
# put maplibre-gl.mjs, maplibre-gl-shared.mjs, maplibre-gl-worker.mjs, maplibre-gl.css there too

# 3. serve and drive
(cd /tmp/spike-out && python3 -m http.server 8765 &)
cd docs/map-engine-spike
OUT=/tmp/spike-shots node proto_check.mjs       # prints state after each step, saves shots
node bench_run.mjs gpu                          # or: swiftshader | gpu <libs> <counts> <cpuThrottle>
```

Files in `docs/map-engine-spike/`: `build_art.py`, `prototype.html`, `bench.html`,
`cdp.mjs`, `proto_check.mjs`, `bench_run.mjs`. All throwaway spike tooling, not part of
wowmap and not tested by CI beyond `py_compile`.
