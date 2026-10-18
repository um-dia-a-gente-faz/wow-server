# pathfinder: movement-map reader

`navmesh.py` reads TrinityCore 3.3.5a movement maps (`mmaps/`) with the standard library only.
It answers three questions: which ground polygon is under a WoW point, the terrain height there,
and the walkable path between two points (`find_path`, #760). The public API takes and returns
WoW coordinates `(x, y, z)`.

Every layout fact below was read from the TrinityCore `3.3.5` branch, not from upstream Recast.
Paths are relative to the TrinityCore repository root.

## Files

| File | Holds |
|---|---|
| `mmaps/<map:03>.mmap` | one `dtNavMeshParams` (28 bytes) for the map |
| `mmaps/<map:03><gx:02><gy:02>.mmtile` | one tile: an `MmapTileHeader` (20 bytes), then `size` bytes of Detour tile data |

## Facts and where they come from

**Map file name and params**

- Name: `mmaps/%03u.mmap`, `MAP_FILE_NAME_FORMAT`, `src/common/Collision/Management/MMapManager.cpp:25`, loaded at `:75`.
- `dtNavMeshParams` is read with `fread(&params, sizeof(dtNavMeshParams), 1)`, `MMapManager.cpp:84`.
  Struct: `dep/recastnavigation/Detour/Include/DetourNavMesh.h:339`. Fields `orig[3]`, `tileWidth`,
  `tileHeight` (floats), `maxTiles`, `maxPolys` (ints): 28 bytes, little-endian.

**Tile file name and argument order**

- Name: `mmaps/%03u%02u%02u.mmtile`, `TILE_FILE_NAME_FORMAT`, `MMapManager.cpp:26`.
- Arguments are `(basePath, mapId, x, y)`, `MMapManager.cpp:131`: map, then x, then y.
- The caller passes the grid index. `Map.cpp:170` calls `loadMap(..., GetId(), gx, gy)`, and
  `Map.cpp:508-509` computes `gx = (MAX_NUMBER_OF_GRIDS - 1) - p.x_coord`, where `p` is
  `Trinity::ComputeGridCoord(x, y)` (`src/server/game/Grids/GridDefines.h:181-183`; constants
  `MAX_NUMBER_OF_GRIDS = 64` at `:36`, `SIZE_OF_GRIDS = 533.3333f` at `:38`,
  `CENTER_GRID_ID = 32` at `:39`). `navmesh.py` `_grid_index` repeats that arithmetic.
  Example: WoW x = 100 gives 31, and x = -100 gives 32.
- The `.mmtile` index is therefore taken from the WoW point, and the reader does not depend on the
  generator's own tile numbering. Lookups search the point's tile and its 8 neighbours.

**MmapTileHeader (20 bytes)**

- `src/common/Collision/Maps/MapDefines.h:25` `MMAP_MAGIC = 0x4d4d4150` ('MMAP').
- `:26` `MMAP_VERSION 15`.
- `:28-35` struct: `uint32 mmapMagic, dtVersion, mmapVersion, size; char usesLiquids; char padding[3]`.
- `:42` static_assert `sizeof == 20`.
- Checks in `MMapManager.cpp`: magic at `:141`, `mmapVersion` at `:148`, `size` against the file
  length at `:158-160`, data read at `:170`, `addTile` at `:184`.
- `dtVersion` is `DT_NAVMESH_VERSION`. `read_tile` checks it too, which TrinityCore does not.

**Detour tile data**

- Magic `DT_NAVMESH_MAGIC = 'D'<<24|'N'<<16|'A'<<8|'V'` (0x444E4156), `DetourNavMesh.h:82`.
  Version `DT_NAVMESH_VERSION = 7`, `:85`. `addTile` checks both at `DetourNavMesh.cpp:913-915`.
- `dtMeshHeader`, `DetourNavMesh.h:261-289`: 100 bytes. Six 4-byte ints (magic, version, x, y,
  layer, userId), nine ints (polyCount, vertCount, maxLinkCount, detailMeshCount,
  detailVertCount, detailTriCount, bvNodeCount, offMeshConCount, offMeshBase), three floats
  (walkableHeight, walkableRadius, walkableClimb), `bmin[3]`, `bmax[3]`, and `bvQuantFactor`.
- Sections follow the header in this order, `DetourNavMesh.cpp:971-989`: verts `float[3*vertCount]`,
  polys `dtPoly[polyCount]`, links `dtLink[maxLinkCount]`, detail meshes `dtPolyDetail[detailMeshCount]`,
  detail verts `float[3*detailVertCount]`, detail tris `uchar[4*detailTriCount]`, BV tree
  `dtBVNode[bvNodeCount]`, off-mesh `dtOffMeshConnection[offMeshConCount]`.
- Each section is `dtAlign4`-rounded (`DetourCommon.h:464`). Every struct size here is already a
  multiple of 4, so the rounding never adds bytes. The code keeps it so the layout stays exact.

**Struct sizes, C layout and the DT_POLYREF64 trap**

- `DetourNavMesh.h:28` `#define DT_POLYREF64 1`, unconditional. `:58` `dtPolyRef` is `uint64_d`, so 8 bytes.
- `dtLink`, `:214`: `dtPolyRef ref` (8), `unsigned int next` (4), `edge, side, bmin, bmax` (4 bytes).
  Size **16**, not 12 as with 32-bit refs. Links are read to advance the offset, not kept.
- `dtPoly`, `:167`: `firstLink` (4), `verts[6]` (12), `neis[6]` (12), `flags` (2), `vertCount` (1),
  `areaAndtype` (1). Size **32**.
  `areaAndtype & 0x3f` is the area, and `>> 6` is the type (`:196-199`).
- `dtPolyDetail`, `:203`: `vertBase`, `triBase` (4 each), `vertCount`, `triCount` (1 each).
  Size **12** in memory, with 2 trailing pad bytes that the reader skips (`'<IIBBxx'`).
- `dtBVNode`, `:227`: `bmin[3]`, `bmax[3]` (6 shorts), `i` (int). Size 16. Not used.
- `dtOffMeshConnection`, `:236`: `pos[6]`, `rad` (7 floats), `poly` (short), `flags`, `side`
  (bytes), `userId` (uint). Size 36. Read and kept in `Tile.off_mesh`.
- `float[3]` vertex: 12 bytes. `uchar[4]` detail triangle: 4 bytes.

**Polygon types and detail meshes**

- `DT_POLYTYPE_GROUND = 0`, `DT_POLYTYPE_OFFMESH_CONNECTION = 1`, `DetourNavMesh.h:159-161`.
  Only ground polys are used. Off-mesh polys are kept but never returned.
- Height: `dtNavMesh::getPolyHeight`, `DetourNavMesh.cpp:677-720`. The detail mesh of poly `i` is
  `detailMeshes[i]` (`:685`). A detail triangle index `t[k] < poly->vertCount` is a poly vertex,
  `tile->verts[poly->verts[t[k]]]`. Any other index is detail vertex
  `detailVerts[vertBase + (t[k] - vertCount)]` (`:705-707`). `navmesh.py` `_height_in_poly` does the same.
- Point in polygon: `dtPointInPolygon`, `DetourCommon.cpp:238-252`, tests Detour x and z. Its half-open
  rule leaves a point on a border out of both polys, so `navmesh.py` `_on_edge` also counts a point on an
  edge (within 1e-3). Without it a clamped overlap end has no ground, and `height()` returns `None`.
- Height in a triangle: `dtClosestHeightPointTriangle`, `DetourCommon.cpp:204-233`. Barycentric
  weights in x and z, height from y. `navmesh.py` uses the same weights. For a point on an edge
  it takes the triangle with the largest margin.

**Polygon links and DT_EXT_LINK**

- `DT_EXT_LINK = 0x8000`, `DetourNavMesh.h:97`. Each edge value is `dtPoly::neis[j]`, `:177`; its
  documented decoding is at `:738-755` (`neis[n] & 0xff` is the neighbour, `neis[n] & DT_EXT_LINK`
  marks a portal).
- Inside a tile, `neis[j]` is the neighbour poly index plus 1, and 0 is a wall. `connectIntLinks`
  links `neis[j] - 1` at `DetourNavMesh.cpp:543-551`, skipping `0` and the `DT_EXT_LINK` bit.
- On a portal, the low byte is the **side** of the neighbour tile, 0..7. `connectExtLinks` reads it
  as `dir = neis[j] & 0xff` (`DetourNavMesh.cpp:406`) and looks for the neighbour on that side
  (`DetourNavMesh.cpp:1036-1038`: `connectExtLinks(tile, neis[j], i)` for the tile on side `i`).
- Side steps: `getNeighbourTilesAt`, `DetourNavMesh.cpp:1069-1084`. Sides 0..7 step tile x and y by
  `(1,0) (1,1) (0,1) (-1,1) (-1,0) (-1,-1) (0,-1) (1,-1)`.
- The neighbour's matching edge has the **opposite** side, `dtOppositeTile(side) = (side + 4) & 0x7`
  (`DetourCommon.h:466`). `findConnectingPolys` (`DetourNavMesh.cpp:300-351`) searches the other
  tile for `neis == DT_EXT_LINK | opposite` (`:312`, `:324`) that lies on the same border line
  (`dtAbs(apos - bpos) > 0.01f`, `:331`) and whose extent overlaps ours (`:335-337`).
- The border test is `overlapSlabs` (`DetourNavMesh.cpp:31-65`). Extents are shrunk by 0.01 so that
  edges touching at an end do not link (`:38-40`). Edges that cross always overlap (`:56`). Otherwise
  the end heights must be within `2 * walkableClimb` (`:60-63`), where `walkableClimb` is the
  neighbour's (`findConnectingPolys` gets the target tile, `:337`). `calcSlabEndPoints`
  (`:76-112`) and `getSlabCoord` (`:67-74`) give the slab axis: x for sides 0 and 4, z for 2 and 6.
  `navmesh.py` `_ext_links` repeats this. One portal per neighbour poly: the `break` at `:347`, after `conarea` is written at `:342-343`.
- Mapping a side to a file: the mmtile name is `(map, tileY, tileX)` (`MapBuilder.cpp:886`), and the
  reader reads its two numbers as `(gx, gy)` (`MMapManager.cpp:131`). The header x and y count from
  the map's origin, so they run opposite to the file indices: `bmax = (32 - tileX) * GRID_SIZE`
  (`MapBuilder.cpp:945-946`), and the origin is the bounds of the largest tile (`:553-564`). So a step of
  `(dX, dY)` in header x and y is `(gx - dY, gy - dX)` in file indices. Side 0 from file key `(31, 31)`
  is `(31, 30)`, which holds WoW y in `[533, 1067)`, next to `(31, 31)` for y in `[0, 533)`.
- `dtTriArea2D` (`DetourCommon.h:339-345`) is the signed area used by the funnel, with x and z.

**Path search**

- `find_path` returns the corners of the path, WoW `(x, y, z)`, first point start and last point end.
  Each point's z is `height(x, y, hint)`, where the hint is the funnel point's Detour y. The hint picks
  the floor nearest that height. The funnel point's own y is kept only where `height()` returns `None`.
  A corner on a clamped overlap end is interpolated along our edge, so its y can differ from the
  neighbour's surface: the z comes from the surface, not from the interpolation.
- Corners never repeat: `appendVertex` (`DetourNavMeshQuery.cpp:1666-1673`) drops a point equal to the
  last one, and `_funnel` does the same. Without it a corner that is also the end of the next portal is
  emitted twice.
- Search is A* over polygons. A poly is entered through an edge, so the cost is the distance between
  entry-edge midpoints, and the heuristic is the straight distance to end. Both are this module's
  choice, not TrinityCore's: `dtNavMeshQuery::findPath` is the reference.
- The corners come from the simple stupid funnel, as in `dtNavMeshQuery::findStraightPath`
  (`DetourNavMeshQuery.cpp:1766-1980`; the tightening tests are `:1876` and `:1927`).
- The portal is the **overlap**, not the full edge. `connectExtLinks` clamps the overlap to the
  edge and stores it as `bmin` and `bmax` (`DetourNavMesh.cpp:432-436`, `dtClamp` to `[0, 1]`), and
  `navmesh.py` takes the same fractions along the neighbour's edge (`_at_along`), so the portal's heights
  are the neighbour's. The path steps onto that tile at the corner.
- Left and right of each portal are chosen from the geometry (the travel direction is the centre of
  the next poly minus the centre of this one, and a point is on the left when its cross product is
  positive). Detour takes `left = verts[edge]` and `right = verts[edge+1]` (`getPortalPoints`,
  `DetourNavMeshQuery.cpp:2292`), which holds for Recast's winding only. The result does not depend
  on the winding here.
- Back to WoW: `PathGenerator.cpp:594` converts Detour `(y, z, x)` to WoW `(x, y, z)`, the inverse of `:164`.

## HTTP contract

`server.py` serves `navmesh.py` over HTTP (stdlib `http.server`, #82). Run from `tools/`:
`MMAPS_DIR=<TrinityCore mmaps/> [PATHFINDER_PORT=8770] python -m pathfinder.server`.
One `NavMesh` is kept per map after its first successful load. A map with no `.mmap` is not kept.
Every response is JSON. Errors are `{"error": "<reason>"}`.

- `POST /path` with `{"map": int, "from": {"x","y","z"}, "to": {"x","y","z"}}` answers 200:
  `{"status": "ok", "points": [{"x","y","z"}, ...]}`, start first and end last (`find_path`).
  `{"status": "no_path", "points": []}` when `find_path` returns `None` (an end off the mesh, or no
  route). `{"status": "no_map", "points": []}` when the map has no `.mmap`.
- `GET /height?map=<int>&x=<num>&y=<num>[&z=<num>]` answers 200 `{"z": float | null}`. `z` is the hint
  for stacked floors (`height`). An unknown map or a point off the mesh gives `null`.
- `GET /healthz` answers 200 `{"ok": true, "maps_loaded": [<map ids>]}`.
- Bad JSON, a missing or non-numeric field (a bool or a non-finite number counts as non-numeric), or a
  bad query gives 400. An unknown path gives 404. A corrupt movement-map file gives 500.

## Axis order: WoW (x, y, z) is Detour (y, z, x)

- WoW point to Detour: `PathGenerator.cpp:164` `float startPoint[VERTEX_SIZE] = {startPos.y, startPos.z, startPos.x};`
  and `:165` for the end point. So Detour x = WoW y, Detour y = WoW z (height), Detour z = WoW x.
- Detour back to WoW: `PathGenerator.cpp:594` `G3D::Vector3(pathPoints[i*3+2], pathPoints[i*3], pathPoints[i*3+1])`.
  This is the inverse: WoW x = Detour z, WoW y = Detour x, WoW z = Detour y.
- `:719` `float point[VERTEX_SIZE] = {p.y, p.z, p.x};` is the same mapping for the liquid check.
- In `navmesh.py`, the 2D test uses `px = y` and `pz = x`, and the height is Detour y, which is WoW z.

## Not done here

- Links are read only to skip them. Off-mesh connections are kept but not traversed.
- Path search expands at most `max_polys` polys with no pruning. Expect this to be enough for tiles,
  not for whole maps. The measured limit is the 50 ms test on a 100-poly grid, not a real tile.
- Each `.mmtile` of the map is read at load time, as the spec asks. A lazy loader is the upgrade if
  memory matters.
- The layout is verified against the source, not yet against a real tile. The human steps are still
  open: print the height at Sunstrider Isle start from a map 530 tile on the VM, and find a path
  across a real tile border (this checks the overlap rule and the side-to-file mapping).
