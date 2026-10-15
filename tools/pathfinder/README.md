# pathfinder: movement-map reader

`navmesh.py` reads TrinityCore 3.3.5a movement maps (`mmaps/`) with the standard library only.
It answers two questions: which ground polygon is under a WoW point, and the terrain height
there. Path search is #760. The public API takes and returns WoW coordinates `(x, y, z)`.

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
- Point in polygon: `dtPointInPolygon`, `DetourCommon.cpp:238-252`, tests Detour x and z.
- Height in a triangle: `dtClosestHeightPointTriangle`, `DetourCommon.cpp:204-233`. Barycentric
  weights in x and z, height from y. `navmesh.py` uses the same weights. For a point on an edge
  it takes the triangle with the largest margin.

## Axis order: WoW (x, y, z) is Detour (y, z, x)

- WoW point to Detour: `PathGenerator.cpp:164` `float startPoint[VERTEX_SIZE] = {startPos.y, startPos.z, startPos.x};`
  and `:165` for the end point. So Detour x = WoW y, Detour y = WoW z (height), Detour z = WoW x.
- Detour back to WoW: `PathGenerator.cpp:594` `G3D::Vector3(pathPoints[i*3+2], pathPoints[i*3], pathPoints[i*3+1])`.
  This is the inverse: WoW x = Detour z, WoW y = Detour x, WoW z = Detour y.
- `:719` `float point[VERTEX_SIZE] = {p.y, p.z, p.x};` is the same mapping for the liquid check.
- In `navmesh.py`, the 2D test uses `px = y` and `pz = x`, and the height is Detour y, which is WoW z.

## Not done here

- Links are read only to skip them. Off-mesh connections are kept but not traversed.
- Each `.mmtile` of the map is read at load time, as the spec asks. A lazy loader is the upgrade if
  memory matters.
- The layout is verified against the source, not yet against a real tile. The human step (print
  the height at Sunstrider Isle start from a map 530 tile on the VM) is still open.
