"""Stdlib reader for TrinityCore 3.3.5a movement maps (mmaps): which ground polygon is under a
point, the terrain height there, and a polygon path with funnel straightening between two points.

Every layout fact is taken from TrinityCore branch 3.3.5; tools/pathfinder/README.md gives the
source file and line for each one. The public API takes and returns WoW coordinates (x, y, z).
Detour space (y up) is internal: a WoW point (x, y, z) is the Detour point (y, z, x), as
PathGenerator does (src/server/game/Movement/PathGenerator.cpp:164).
"""
from __future__ import annotations

import heapq
import math
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path


class NavMeshError(ValueError):
    """A movement-map file that is not the 3.3.5a format this reader knows."""


# src/common/Collision/Maps/MapDefines.h:25-26
MMAP_MAGIC = 0x4D4D4150  # 'MMAP'
MMAP_VERSION = 15
# dep/recastnavigation/Detour/Include/DetourNavMesh.h:82, 85
DT_NAVMESH_MAGIC = 0x444E4156  # 'DNAV'
DT_NAVMESH_VERSION = 7
# DetourNavMesh.h:159
DT_POLYTYPE_GROUND = 0
# DetourNavMesh.h:97. A poly edge with this bit is a portal; its low byte is the side (0..7).
DT_EXT_LINK = 0x8000
# (dX, dY) step in tile x and y for each side, DetourNavMesh.cpp:1069-1084 (getNeighbourTilesAt)
_SIDE_STEP = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
# src/server/game/Grids/GridDefines.h:36-39
MAX_NUMBER_OF_GRIDS = 64
SIZE_OF_GRIDS = 533.3333
CENTER_GRID_ID = MAX_NUMBER_OF_GRIDS // 2
CENTER_GRID_OFFSET = SIZE_OF_GRIDS / 2

# Little-endian records with no C alignment. C trailing padding is spelled out as `x`.
_MMAP_HEADER = struct.Struct("<IIIIB3x")      # MmapTileHeader, MapDefines.h:28-35 (20 bytes)
_PARAMS = struct.Struct("<5f2i")              # dtNavMeshParams, DetourNavMesh.h:339 (28 bytes)
_MESH_HEADER = struct.Struct("<iiiiiI9i3f6ff")  # dtMeshHeader, DetourNavMesh.h:261 (100 bytes)
_MESH_HEADER_INTS = ("magic", "version", "x", "y", "layer", "userId", "polyCount", "vertCount",
                     "maxLinkCount", "detailMeshCount", "detailVertCount", "detailTriCount",
                     "bvNodeCount", "offMeshConCount", "offMeshBase",
                     "walkableHeight", "walkableRadius", "walkableClimb")
_VEC3 = struct.Struct("<3f")                  # float[3], verts and detailVerts
_POLY = struct.Struct("<I6H6HHBB")            # dtPoly, DetourNavMesh.h:167 (32 bytes)
_POLY_DETAIL = struct.Struct("<IIBBxx")       # dtPolyDetail, :203 (12 bytes, C pads to 4)
_LINK = struct.Struct("<QIBBBB")              # dtLink, :214 (16 bytes: dtPolyRef is 64-bit)
_BV_NODE = struct.Struct("<6Hi")              # dtBVNode, :227 (16 bytes)
_OFF_MESH = struct.Struct("<7fHBBI")          # dtOffMeshConnection, :236 (36 bytes)
_TRI = struct.Struct("<4B")                   # uchar[4] detail triangle: 3 indices and edge flags


def _align4(n: int) -> int:
    return (n + 3) & ~3                       # dtAlign4, DetourCommon.h:464


@dataclass(frozen=True)
class Poly:
    verts: tuple[int, ...]    # indices into Tile.verts, vertCount of them
    neis: tuple[int, ...]     # raw neis values for the same edges (DT_EXT_LINK bit kept)
    flags: int
    area: int
    type: int                 # DT_POLYTYPE_GROUND (0) or DT_POLYTYPE_OFFMESH_CONNECTION (1)


@dataclass(frozen=True)
class OffMeshCon:
    pos: tuple[float, ...]    # two Detour points, 6 floats
    rad: float
    poly: int
    flags: int
    side: int
    user_id: int


@dataclass(frozen=True)
class Tile:
    header: dict              # dtMeshHeader fields by Detour name; bmin and bmax are tuples
    verts: list[tuple[float, float, float]]                 # Detour space
    polys: list[Poly]
    detail_meshes: list[tuple[int, int, int, int]]          # vertBase, triBase, vertCount, triCount
    detail_verts: list[tuple[float, float, float]]
    detail_tris: list[tuple[int, int, int, int]]            # 3 indices (< vertCount: a poly vert,
                                                            # else detail vert) and edge flags
    off_mesh: list[OffMeshCon] = field(default_factory=list)


def _section(body: bytes, off: int, rec: struct.Struct, count: int) -> tuple[list[tuple], int]:
    """The `count` records at `off`, and the offset of the next section (sections are dtAlign4'd)."""
    size = rec.size * count
    if off + size > len(body):
        raise NavMeshError("tile data is shorter than its header says")
    return list(rec.iter_unpack(body[off:off + size])), off + _align4(size)


def _read_body(body: bytes) -> Tile:
    """dtMeshHeader and the sections, in the order dtNavMesh::addTile patches them
    (DetourNavMesh.cpp:971-981)."""
    if len(body) < _MESH_HEADER.size:
        raise NavMeshError("tile data is shorter than dtMeshHeader")
    values = _MESH_HEADER.unpack_from(body)
    header = dict(zip(_MESH_HEADER_INTS, values[:18], strict=True))
    header["bmin"], header["bmax"] = tuple(values[18:21]), tuple(values[21:24])
    header["bvQuantFactor"] = values[24]
    if header["magic"] != DT_NAVMESH_MAGIC:
        raise NavMeshError(f"bad dtMeshHeader magic {header['magic']:#x}")
    if header["version"] != DT_NAVMESH_VERSION:
        raise NavMeshError(f"tile is Detour v{header['version']}, expected v{DT_NAVMESH_VERSION}")

    off = _align4(_MESH_HEADER.size)
    verts, off = _section(body, off, _VEC3, header["vertCount"])
    raw_polys, off = _section(body, off, _POLY, header["polyCount"])
    _, off = _section(body, off, _LINK, header["maxLinkCount"])        # links: not used
    details, off = _section(body, off, _POLY_DETAIL, header["detailMeshCount"])
    detail_verts, off = _section(body, off, _VEC3, header["detailVertCount"])
    detail_tris, off = _section(body, off, _TRI, header["detailTriCount"])
    _, off = _section(body, off, _BV_NODE, header["bvNodeCount"])      # BV tree: not used
    off_mesh, _ = _section(body, off, _OFF_MESH, header["offMeshConCount"])

    polys = []
    for r in raw_polys:                       # r[0] firstLink, r[1:7] verts, r[7:13] neis,
        n = r[14]                             # r[13] flags, r[14] vertCount, r[15] areaAndtype
        polys.append(Poly(verts=r[1:1 + n], neis=r[7:7 + n], flags=r[13],
                          area=r[15] & 0x3F, type=r[15] >> 6))
    return Tile(
        header=header,
        verts=verts,
        polys=polys,
        detail_meshes=details,
        detail_verts=detail_verts,
        detail_tris=detail_tris,
        off_mesh=[OffMeshCon(pos=r[0:6], rad=r[6], poly=r[7], flags=r[8], side=r[9], user_id=r[10])
                  for r in off_mesh],
    )


def read_tile(data: bytes) -> Tile:
    """One .mmtile file: MmapTileHeader, then `size` bytes of Detour tile data."""
    if len(data) < _MMAP_HEADER.size:
        raise NavMeshError("mmtile is shorter than its header")
    magic, dt_version, mmap_version, size, _uses_liquids = _MMAP_HEADER.unpack_from(data)
    if magic != MMAP_MAGIC:
        raise NavMeshError(f"bad mmap magic {magic:#x}")
    if mmap_version != MMAP_VERSION:
        raise NavMeshError(f"built with mmap generator v{mmap_version}, expected v{MMAP_VERSION}")
    if dt_version != DT_NAVMESH_VERSION:
        raise NavMeshError(f"built with Detour v{dt_version}, expected v{DT_NAVMESH_VERSION}")
    body = data[_MMAP_HEADER.size:]
    if size > len(body):
        raise NavMeshError("mmtile has corrupted data size")
    return _read_body(body[:size])


def _grid_index(coord: float) -> int:
    # GridDefines.h:181-183 (ComputeGridCoord), then Map.cpp:508 flips it: gx = 63 - x_coord.
    # Same int() truncation and +0.5 rounding as Trinity::Compute.
    grid = int((coord - CENTER_GRID_OFFSET) / SIZE_OF_GRIDS + CENTER_GRID_ID + 0.5)
    return (MAX_NUMBER_OF_GRIDS - 1) - grid


def _inside(tile: Tile, poly: Poly, px: float, pz: float) -> bool:
    """Point in polygon on Detour x and z (DetourCommon.cpp:238-252, dtPointInPolygon)."""
    verts = [tile.verts[i] for i in poly.verts]
    inside = False
    j = len(verts) - 1
    for i in range(len(verts)):
        xi, zi, xj, zj = verts[i][0], verts[i][2], verts[j][0], verts[j][2]
        if (zi > pz) != (zj > pz) and px < (xj - xi) * (pz - zi) / (zj - zi) + xi:
            inside = not inside
        j = i
    return inside


def _height_in_poly(tile: Tile, index: int, px: float, pz: float) -> float | None:
    """Detail-mesh height at (px, pz), by barycentric weights in Detour x and z
    (DetourCommon.cpp:204-233, dtClosestHeightPointTriangle). The point is inside the polygon, so
    the triangle with the largest margin covers it up to float error; that one answers edges too."""
    poly = tile.polys[index]
    base, tri_base, _, tri_count = tile.detail_meshes[index]   # DetourNavMesh.cpp:677-720
    nv = len(poly.verts)

    def vert(k: int) -> tuple[float, float, float]:
        return tile.verts[poly.verts[k]] if k < nv else tile.detail_verts[base + k - nv]

    best: tuple[float, float] | None = None       # (margin, height)
    for a_i, b_i, c_i, _flags in tile.detail_tris[tri_base:tri_base + tri_count]:
        a, b, c = vert(a_i), vert(b_i), vert(c_i)
        ux, uz = c[0] - a[0], c[2] - a[2]         # v0 = c - a
        vx, vz = b[0] - a[0], b[2] - a[2]         # v1 = b - a
        wx, wz = px - a[0], pz - a[2]             # v2 = p - a
        denom = ux * vz - uz * vx
        if abs(denom) < 1e-6:
            continue
        s = (vz * wx - vx * wz) / denom           # weight of c
        t = (ux * wz - uz * wx) / denom           # weight of b
        margin = min(s, t, 1.0 - s - t)
        if best is None or margin > best[0]:
            best = (margin, a[1] + s * (c[1] - a[1]) + t * (b[1] - a[1]))
    return None if best is None else best[1]


def _neighbour_key(key: tuple[int, int], side: int) -> tuple[int, int]:
    """File key of the tile on `side` of tile `key`. Header x grows with Detour x (WoW y), and the
    file's second index is the generator's tileX, which shrinks as WoW y grows: MapBuilder.cpp:945-946
    gives bmax = (32 - tileX) * GRID_SIZE, and MapBuilder.cpp:886 writes the name as (map, tileY, tileX).
    So a step of dX in header x is -dX in gy, and a step of dY in header y is -dY in gx."""
    dx, dy = _SIDE_STEP[side]
    return key[0] - dy, key[1] - dx


def _same_xz(p: tuple, q: tuple) -> bool:
    """Two vertices of a shared edge, on Detour x and z. Vertices are identical on both sides of a
    border, so 1e-3 absorbs float noise."""
    return abs(p[0] - q[0]) <= 1e-3 and abs(p[2] - q[2]) <= 1e-3


def _ext_target(tile: Tile, a: tuple, b: tuple, side: int) -> int | None:
    """The ground poly of the neighbour `tile` whose edge points back at us (neis == DT_EXT_LINK | side,
    the opposite side, dtOppositeTile DetourCommon.h:466) and has the same two vertices as our edge a-b.
    ponytail: exact vertex match. TrinityCore matches slab overlap (findConnectingPolys,
    DetourNavMesh.cpp:300-340), so a T-junction on a border would not link here. Add overlap when real tiles need it."""
    want = DT_EXT_LINK | ((side + 4) & 7)
    for k, poly in enumerate(tile.polys):
        if poly.type != DT_POLYTYPE_GROUND:
            continue
        n = len(poly.verts)
        for m, nei in enumerate(poly.neis):
            if nei != want:
                continue
            c, d = tile.verts[poly.verts[m]], tile.verts[poly.verts[(m + 1) % n]]
            if (_same_xz(a, c) and _same_xz(b, d)) or (_same_xz(a, d) and _same_xz(b, c)):
                return k
    return None


def _area(a: tuple, b: tuple, c: tuple) -> float:
    """Signed area of a, b, c on Detour x and z (dtTriArea2D, DetourCommon.h:339)."""
    return (c[0] - a[0]) * (b[2] - a[2]) - (b[0] - a[0]) * (c[2] - a[2])


def _same_point(p: tuple, q: tuple) -> bool:
    return abs(p[0] - q[0]) < 1e-6 and abs(p[2] - q[2]) < 1e-6


def _left_right(a: tuple, b: tuple, travel: tuple[float, float]) -> tuple[tuple, tuple]:
    """(left, right) of the portal a-b crossed in the direction `travel` (Detour x, z). A point is on the
    left when cross(travel, p - mid) > 0. Decided from the geometry, so the polygon winding does not matter."""
    mx, mz = (a[0] + b[0]) / 2, (a[2] + b[2]) / 2
    if travel[0] * (a[2] - mz) - travel[1] * (a[0] - mx) > 0:
        return a, b
    return b, a


def _funnel(start: tuple, end: tuple, portals: list[tuple[tuple, tuple]]) -> list[tuple]:
    """Simple stupid funnel: the loop of dtNavMeshQuery::findStraightPath (DetourNavMeshQuery.cpp:1766-1980),
    on (left, right) portals in travel order. Returns the corners, start and end included."""
    portals = [(start, start), *portals, (end, end)]
    path = [start]
    apex = left = right = start
    ai = li = ri = 0
    i = 0
    while i < len(portals):
        pl, pr = portals[i]
        if _area(apex, right, pr) <= 0:
            if _same_point(apex, right) or _area(apex, left, pr) > 0:
                right, ri = pr, i
            else:                                  # right crossed over left: left is a corner
                path.append(left)
                apex, ai = left, li
                left = right = apex
                li = ri = ai
                i = ai + 1
                continue
        if _area(apex, left, pl) >= 0:
            if _same_point(apex, left) or _area(apex, right, pl) < 0:
                left, li = pl, i
            else:                                  # left crossed over right: right is a corner
                path.append(right)
                apex, ai = right, ri
                left = right = apex
                li = ri = ai
                i = ai + 1
                continue
        i += 1
    if not _same_point(path[-1], end):
        path.append(end)
    return path


class NavMesh:
    """Every .mmtile of one map, keyed by its (gx, gy) file index."""

    def __init__(self, params: dict, tiles: dict[tuple[int, int], Tile]):
        self.params = params
        self.tiles = tiles

    @classmethod
    def load(cls, mmaps_dir: Path, map_id: int) -> NavMesh:
        raw = (mmaps_dir / f"{map_id:03}.mmap").read_bytes()
        if len(raw) < _PARAMS.size:
            raise NavMeshError(f"{map_id:03}.mmap is shorter than dtNavMeshParams")
        v = _PARAMS.unpack_from(raw)
        params = {"orig": v[0:3], "tileWidth": v[3], "tileHeight": v[4], "maxTiles": v[5], "maxPolys": v[6]}
        # File name is mmaps/MMMXXYY.mmtile with (map, x, y), MMapManager.cpp:131 and Map.cpp:170.
        name = re.compile(rf"{map_id:03}(\d\d)(\d\d)\.mmtile")
        tiles = {}
        for path in mmaps_dir.glob(f"{map_id:03}*.mmtile"):
            m = name.fullmatch(path.name)
            if m:
                tiles[(int(m[1]), int(m[2]))] = read_tile(path.read_bytes())
        return cls(params, tiles)

    def tile_coords(self, x: float, y: float) -> tuple[int, int]:
        """The file index of the tile that holds WoW point (x, y)."""
        return _grid_index(x), _grid_index(y)

    def _candidates(self, x: float, y: float):
        """(tile key, poly index, height) for every ground poly over (x, y). Searches the tile of
        the point and its 8 neighbours: a polygon can reach across a tile edge."""
        cx, cy = self.tile_coords(x, y)
        px, pz = y, x                             # Detour (x, z) of WoW (x, y), PathGenerator.cpp:164
        for key in ((cx + i, cy + j) for i in (-1, 0, 1) for j in (-1, 0, 1)):
            tile = self.tiles.get(key)
            if tile is None:
                continue
            # ponytail: linear scan of every poly of 9 tiles per query; add a spatial index if p95 matters
            for index, poly in enumerate(tile.polys):
                if poly.type != DT_POLYTYPE_GROUND or not _inside(tile, poly, px, pz):
                    continue
                height = _height_in_poly(tile, index, px, pz)
                if height is not None:
                    yield key, index, height

    def _best(self, x: float, y: float, z: float | None) -> tuple[tuple[int, int], int, float] | None:
        hits = list(self._candidates(x, y))
        if not hits:
            return None
        if z is None:
            return max(hits, key=lambda hit: hit[2])
        return min(hits, key=lambda hit: abs(hit[2] - z))

    def find_poly(self, x: float, y: float, z: float | None = None) -> tuple[tuple[int, int], int] | None:
        """(tile key, poly index) of the ground poly over (x, y); with several stacked, the one whose
        height is nearest z (the highest when z is None). None off the mesh."""
        hit = self._best(x, y, z)
        return None if hit is None else (hit[0], hit[1])

    def height(self, x: float, y: float, z: float | None = None) -> float | None:
        """Terrain height (WoW z) at (x, y) of the poly chosen as in find_poly; None off the mesh."""
        hit = self._best(x, y, z)
        return None if hit is None else hit[2]

    def _portal_neighbours(self, ref: tuple[tuple[int, int], int]):
        """(neighbour ref, a, b) for each edge of `ref` that leads to a ground poly. a and b are the
        edge's Detour points in the order of the poly. Edges with neis 0 are walls."""
        key, index = ref
        tile = self.tiles[key]
        poly = tile.polys[index]
        n = len(poly.verts)
        for j, nei in enumerate(poly.neis):
            if nei == 0:
                continue
            a, b = tile.verts[poly.verts[j]], tile.verts[poly.verts[(j + 1) % n]]
            if nei & DT_EXT_LINK:
                side = nei & 0xFF
                nkey = _neighbour_key(key, side)
                other = self.tiles.get(nkey)
                k = None if other is None else _ext_target(other, a, b, side)
                if k is not None:
                    yield (nkey, k), a, b
            elif tile.polys[nei - 1].type == DT_POLYTYPE_GROUND:   # neis = poly index + 1, connectIntLinks DetourNavMesh.cpp:543-551
                yield (key, nei - 1), a, b

    def _centre(self, ref: tuple[tuple[int, int], int]) -> tuple[float, float]:
        tile = self.tiles[ref[0]]
        vs = [tile.verts[i] for i in tile.polys[ref[1]].verts]
        return sum(v[0] for v in vs) / len(vs), sum(v[2] for v in vs) / len(vs)

    def _corridor(self, begin, goal, s: tuple, e: tuple, max_polys: int) -> list[tuple[tuple, tuple]] | None:
        """A* over polys from begin to goal. Cost is the distance between entry-edge midpoints (the start
        point for the first poly), the heuristic the straight distance to e. Returns the portals in travel
        order, or None when goal is unreachable within max_polys expanded polys."""
        best = {begin: (0.0, None, None, s)}     # ref: (cost, parent, (a, b) entry edge, entry point)
        heap = [(math.dist(s, e), 0, begin)]
        seq, closed, expanded = 0, set(), 0
        while heap:
            _, _, cur = heapq.heappop(heap)
            if cur in closed:
                continue
            if cur == goal:
                return self._portals_to(best, goal)
            closed.add(cur)
            expanded += 1
            if expanded > max_polys:
                return None
            cost, pos = best[cur][0], best[cur][3]
            for nxt, a, b in self._portal_neighbours(cur):
                if nxt in closed:
                    continue
                mid = tuple((p + q) / 2 for p, q in zip(a, b, strict=True))
                g = cost + math.dist(pos, mid)
                if nxt not in best or g < best[nxt][0]:
                    best[nxt] = (g, cur, (a, b), mid)
                    seq += 1
                    heapq.heappush(heap, (g + math.dist(mid, e), seq, nxt))
        return None

    def _portals_to(self, best: dict, goal) -> list[tuple[tuple, tuple]]:
        portals, ref = [], goal
        while best[ref][1] is not None:
            _, parent, (a, b), _ = best[ref]
            (x0, z0), (x1, z1) = self._centre(parent), self._centre(ref)
            portals.append(_left_right(a, b, (x1 - x0, z1 - z0)))
            ref = parent
        return portals[::-1]

    def find_path(self, start: tuple[float, float, float], end: tuple[float, float, float],
                  max_polys: int = 4096) -> list[tuple[float, float, float]] | None:
        """Corners of the walkable path from start to end, WoW coordinates, start first and end last.
        Each point's z is height() at that point: start and end take the terrain height under them, and
        a corner is a mesh vertex, whose y is the height there. None when start or end is off the mesh,
        or no path exists within max_polys expanded polys."""
        begin, goal = self.find_poly(*start), self.find_poly(*end)
        if begin is None or goal is None:
            return None
        s = (start[1], self.height(*start), start[0])    # Detour (y, z, x), PathGenerator.cpp:164
        e = (end[1], self.height(*end), end[0])
        portals = self._corridor(begin, goal, s, e, max_polys)
        if portals is None:
            return None
        return [(p[2], p[0], p[1]) for p in _funnel(s, e, portals)]   # back to WoW, PathGenerator.cpp:594
