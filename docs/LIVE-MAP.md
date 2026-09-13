# Live map — players and where they are

Design notes for a dashboard showing **who is online and where on the game map**,
including movement. Status: researched and de-risked, not yet built.

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

## Open decisions

- **Sampling interval** — keep 90 s (cheap, coarse) or drop it to ~5 s (real movement)?
- **Map art** — extract from the client, render terrain, or use public images?
- **Home** — standalone page, or embedded in Grafana via the Dynamic Text plugin?
- **Cardinality** — per-player positions in Prometheus are fine for tens of players; at
  hundreds, keep the position store inside the app instead of Prometheus.
