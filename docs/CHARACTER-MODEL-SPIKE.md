# Character 3D model spike (#171)

**Result: go.** A race/gender body model renders in the inspect drawer, offline, with a
vendored three.js and no build step. Equipment on the model is not done (out of scope for
this issue; see *Next step*).

Measured on the live VM's 3.3.5a (build 12340) client, read-only, on 2026-10-05.

## What a model needs from the MPQs

For Human male (the other 19 follow the same pattern; `Character\<Race>\<Male|Female>\`):

| File | Where it wins the MPQ search | Size |
|---|---|---|
| `Character\Human\Male\HumanMale.M2` | `patch-3.MPQ` (also in `patch-2`, `patch`, `common-2`) | 1.55 MB (1.0 to 2.2 MB across races) |
| `Character\Human\Male\HumanMale00.skin` | `patch-2.MPQ` | 72 KB (45 to 129 KB) |
| `Character\Human\Male\HumanMaleSkin00_00.blp` | `patch.MPQ` | 342 KB (BLP2, 512x512) |

The M2 is mostly animation and bone data we do not use. Geometry is split: vertices in the
M2, triangles in the `.skin` ("SKIN" magic, a separate file since 3.0). The skin texture
path is not guessed: it is `Texture1` of the `CharSections.dbc` record with race, sex,
type 0, variation 0, colour 0. Formats are in the `models.py` docstring and were checked
against these real files (5264 vertices, 61 submeshes of 48 bytes, vertex 48 bytes).

## What extraction produces

`extract_models.py` keeps the default-look geosets (geoset id = group*100 + variant; body,
the first hair style, variant 1 of every other group, so no armour, no facial hair
variants, no cloak) and writes, per race/gender, into `MODELS_DIR`:

- `<race>_<gender>.bin`: 29 to 57 KB (Human male: 888 vertices, 1024 triangles)
- `<race>_<gender>.png`: the base skin, about 240 KB

All 20 playable race/gender models extracted without error: **5.6 MB total** (0.75 MB of
meshes, 4.9 MB of skins). Nothing is committed; the files live in `/opt/wowmap-data/models`
like `dbc`, `maps` and `icons`, mounted read-only at `/models`.

## What the renderer costs

- `static/three.min.js`: three.js r159, the last release that ships a classic script build
  (no ES modules, no bundler), **668 KB**, MIT, vendored unmodified (sha256 pinned in
  `tests/test_models.py`, licence in `static/three-LICENSE`). Loaded only when a character
  with a model is opened, so the map page does not pay for it. It prints a one-line
  deprecation `console.warn` from three itself.
- `static/charview.js`: ~5 KB of our code. One shared WebGL context, drawn on demand
  (no animation loop); drag rotates, wheel zooms.
- No CDN and no request leaves the host; `/static/` serves an explicit allow-list.
- Without `MODELS_DIR` content the drawer shows "No 3D model extracted" and nothing moves.
  Without WebGL it shows "3D model unavailable".

## Rendered check

Human male, Tauren male, Draenei female (and Scourge female, Blood Elf female loaded
without error) rendered in a browser pane from the extracted files; drag and wheel
verified with synthetic events. Not yet seen in the real drawer against the live
character API, and not by the owner (see the PR's unticked steps).

## Known limits

- Skin only: the base skin atlas has no face, hair, underwear or facial-feature overlays
  (those are more `CharSections` rows composited onto the atlas), so faces are blank and
  hair uses the skin texture. Skin colour and hair style are the defaults, not the
  character's (they live in `characters.playerBytes`).
- Rigid: no bones, no animation (out of scope), and the M2 bind pose is a T-ish pose.
- Geosets are chosen by id convention, not by the batch/texture-unit tables. It looks right
  on the models checked; races with odd geosets (Tauren horns, Draenei tails, Blood Elf
  ears, Scourge) may need per-race overrides.
- Upper bound of 65,535 vertices per mesh (u16 indices); the largest model is 2.7k.

## Next step: equipment (not in this PR)

Item `displayid` -> `ItemDisplayInfo.dbc` (`ModelName`, `ModelTexture`, `GeosetGroup`,
`Texture[8]` for armour regions, `HelmetGeosetVisID`) -> for armour: swap the geosets in
groups 4 (gloves), 5 (boots), 8 (sleeves), 9, 11 (belt), 13 (trousers), 15 (cloak) and
composite the region textures (`Item\TextureComponents\ArmUpperTexture` and siblings, 8
regions) onto the 512x512 skin atlas; for helmets, shoulders and weapons: separate
`Item\ObjectComponents\...` M2 files attached at the model's attachment points (which
needs bones/attachments parsed from the M2). Compositing the atlas is doable client side
on a canvas; attachments are the deeper part.
