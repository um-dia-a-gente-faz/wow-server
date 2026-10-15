"""tools/pathfinder/navmesh.py against the TrinityCore 3.3.5a movement-map layout (README.md has
the source line for each fact). build_tile writes that layout by hand, with its own format
strings, so a reader mistake shows up here as a failing test. Stdlib only.
"""
import pathlib
import random
import struct
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from pathfinder.navmesh import NavMesh, NavMeshError, read_tile  # noqa: E402

MMAP_MAGIC = 0x4D4D4150
DT_MAGIC = 0x444E4156   # 'DNAV'
DT_VERSION = 7
MMAP_VERSION = 15
DT_EXT_LINK = 0x8000    # DetourNavMesh.h:97
GRID = 533.3333         # GridDefines.h:38, one movement-map tile


def pad4(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 4)


def build_tile(polys, *, off_mesh=(), links=2, mmap_magic=MMAP_MAGIC, mmap_version=MMAP_VERSION,
               dt_version=DT_VERSION, tile_magic=DT_MAGIC, tile_version=DT_VERSION, climb=0.0):
    """One .mmtile file. polys: dicts with 'verts' (Detour points), optional 'neis' (raw neis values
    per edge, DT_EXT_LINK bit kept; default walls), 'tris' (local index triples; default fan),
    'extra' (detail verts, referenced from index len(verts) on) and 'type' (1 = off-mesh).
    off_mesh: (pos6, rad, poly index) connections."""
    verts, poly_recs, detail_recs, detail_verts, tris = [], [], [], [], []
    for p in polys:
        base, corners = len(verts), p["verts"]
        nv = len(corners)
        verts += corners
        idx = [base + i for i in range(nv)] + [0] * (6 - nv)
        neis = (list(p.get("neis", [])) + [0] * 6)[:6]
        poly_recs.append(struct.pack("<I6H6HHBB", 0, *idx, *neis, 0, nv, p.get("type", 0) << 6))
        extra = p.get("extra", [])
        local = p.get("tris", [(0, i, i + 1) for i in range(1, nv - 1)])
        detail_recs.append(struct.pack("<IIBBxx", len(detail_verts), len(tris), nv + len(extra), len(local)))
        detail_verts += extra
        tris += [(*t, 0) for t in local]
    header = struct.pack("<iiiiiI9i3f6ff", tile_magic, tile_version, 0, 0, 0, 0,
                         len(polys), len(verts), links, len(polys), len(detail_verts), len(tris),
                         0, len(off_mesh), 0, 0.0, 0.0, climb, *[0.0] * 6, 0.0)
    body = header + b"".join(pad4(s) for s in (
        b"".join(struct.pack("<3f", *v) for v in verts),
        b"".join(poly_recs),
        b"\0" * (16 * links),                       # dtLink: 8-byte ref, 4-byte next, 4 bytes
        b"".join(detail_recs),
        b"".join(struct.pack("<3f", *v) for v in detail_verts),
        b"".join(struct.pack("<4B", *t) for t in tris),
        b"",                                        # BV tree: none
        b"".join(struct.pack("<7fHBBI", *pos, rad, poly, 0, 0, 0) for pos, rad, poly in off_mesh),
    ))
    return struct.pack("<IIIIB3x", mmap_magic, dt_version, mmap_version, len(body), 1) + body


def square(x0, x1, z0, z1, y=0.0):
    """Detour corners of an axis-aligned square at height y."""
    return [(x0, y, z0), (x1, y, z0), (x1, y, z1), (x0, y, z1)]


def mesh_with(*tiles_at):
    """NavMesh holding one tile per (WoW point inside it, .mmtile bytes), keyed as the loader keys."""
    probe = NavMesh({}, {})
    return NavMesh({}, {probe.tile_coords(x, y): read_tile(data) for (x, y), data in tiles_at})


def grid(cols, rows, cell=10.0, *, missing=(), height=lambda x, z: 0.0):
    """Square polys of a cols x rows grid in Detour x and z, cell (col, row) at (col*cell, row*cell).
    Cells in `missing` are removed. Edge j runs verts[j] to verts[j+1]: 0 bottom (row-1), 1 right,
    2 top, 3 left. Neighbours link to each other through neis (index + 1), a wall is 0."""
    cells = [(c, r) for r in range(rows) for c in range(cols) if (c, r) not in missing]
    index = {rc: i for i, rc in enumerate(cells)}
    polys = []
    for c, r in cells:
        x0, z0 = c * cell, r * cell
        corners = [(x0, z0), (x0 + cell, z0), (x0 + cell, z0 + cell), (x0, z0 + cell)]
        nbrs = [(c, r - 1), (c + 1, r), (c, r + 1), (c - 1, r)]
        polys.append({"verts": [(x, height(x, z), z) for x, z in corners],
                      "neis": [index[n] + 1 if n in index else 0 for n in nbrs]})
    return polys


def wow(dx, dz):
    """WoW (x, y) of a Detour (x, z) point: Detour x is WoW y and Detour z is WoW x (PathGenerator.cpp:164)."""
    return dz, dx


def sloped(x, z):
    """Terrain height at Detour (x, z): a plane, so an interpolation error shows up."""
    return 0.25 * x + 0.5 * z + 3.0


def crosses_cell(a, b, x0, x1, z0, z1, steps=400):
    """True if the WoW segment a-b passes through the open Detour rectangle [x0, x1] x [z0, z1]."""
    for k in range(1, steps):
        t = k / steps
        dz = a[0] + (b[0] - a[0]) * t                  # WoW x is Detour z
        dx = a[1] + (b[1] - a[1]) * t                  # WoW y is Detour x
        if x0 < dx < x1 and z0 < dz < z1:
            return True
    return False


class ReadTileTests(unittest.TestCase):
    def test_mmap_magic_mismatch_raises(self):
        with self.assertRaises(NavMeshError):
            read_tile(build_tile([{"verts": square(0, 10, 0, 10)}], mmap_magic=0))

    def test_mmap_generator_version_mismatch_raises(self):
        with self.assertRaises(NavMeshError):
            read_tile(build_tile([{"verts": square(0, 10, 0, 10)}], mmap_version=MMAP_VERSION + 1))

    def test_detour_version_mismatch_raises(self):
        with self.assertRaises(NavMeshError):
            read_tile(build_tile([{"verts": square(0, 10, 0, 10)}], dt_version=DT_VERSION + 1))

    def test_dtmeshheader_magic_mismatch_raises(self):
        with self.assertRaises(NavMeshError):
            read_tile(build_tile([{"verts": square(0, 10, 0, 10)}], tile_magic=0))

    def test_size_past_end_of_file_raises(self):
        data = build_tile([{"verts": square(0, 10, 0, 10)}])
        with self.assertRaises(NavMeshError):
            read_tile(data[:-4])

    def test_sections_are_read_at_their_sizes(self):
        # 2 links of 16 bytes and a detail record of 12 bytes (2 pad bytes) must not shift what follows.
        tile = read_tile(build_tile([{"verts": square(0, 10, 0, 10)},
                                     {"verts": square(20, 30, 0, 10), "extra": [(25, 1.5, 5)]}],
                                    links=3, off_mesh=[((1, 2, 3, 4, 5, 6), 2.0, 1)]))
        self.assertEqual(len(tile.verts), 8)
        self.assertEqual(tile.polys[1].verts, (4, 5, 6, 7))
        self.assertEqual(tile.detail_meshes, [(0, 0, 4, 2), (0, 2, 5, 2)])
        self.assertEqual(tile.detail_verts, [(25, 1.5, 5)])
        self.assertEqual(tile.off_mesh[0].poly, 1)
        self.assertEqual(tile.off_mesh[0].pos, (1, 2, 3, 4, 5, 6))
        self.assertEqual(tile.header["vertCount"], 8)


class FindPolyAndHeightTests(unittest.TestCase):
    def test_find_poly_hits_inside_and_misses_outside(self):
        mesh = mesh_with(((5, 5), build_tile([{"verts": square(0, 10, 0, 10)}])))
        self.assertIsNotNone(mesh.find_poly(5, 5))
        self.assertIsNone(mesh.find_poly(20, 5))

    def test_wow_axes_are_swapped_into_detour(self):
        # Detour square x in [0, 10], z in [20, 30]. WoW (x, y) = (Detour z, Detour x) per PathGenerator.cpp:164.
        mesh = mesh_with(((25, 5), build_tile([{"verts": square(0, 10, 20, 30)}])))
        self.assertEqual(mesh.find_poly(25, 5), ((mesh.tile_coords(25, 5)), 0))
        self.assertIsNone(mesh.find_poly(5, 25))

    def test_height_on_slope_matches_the_plane(self):
        # WoW plane z = 0.5*x + 0.25*y + 3 over [0, 10]^2, built as Detour points (y, z, x).
        corners = [(y, 0.5 * x + 0.25 * y + 3, x) for x, y in ((0, 0), (10, 0), (10, 10), (0, 10))]
        mesh = mesh_with(((5, 5), build_tile([{"verts": corners}])))
        for x, y in ((2, 3), (7, 1), (4, 9)):
            self.assertAlmostEqual(mesh.height(x, y), 0.5 * x + 0.25 * y + 3, delta=1e-4)

    def test_height_uses_detail_vertex_from_vertbase(self):
        # Poly 0 owns detail vert 0, so poly 1's detail verts start at vertBase 1. Its centre vertex is
        # index 4 = poly vert count + 0, which is detail_verts[vertBase + 0].
        fan = [(0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)]
        mesh = mesh_with(((5, 5), build_tile([
            {"verts": square(20, 30, 0, 10), "extra": [(25, 7.0, 5)], "tris": fan},
            {"verts": square(0, 10, 0, 10), "extra": [(5, 2.0, 5)], "tris": fan}])))
        self.assertAlmostEqual(mesh.height(5, 5), 2.0, delta=1e-4)

    def test_stacked_floors_pick_the_floor_nearest_z(self):
        mesh = mesh_with(((5, 5), build_tile([{"verts": square(0, 10, 0, 10, y=0.0)},
                                              {"verts": square(0, 10, 0, 10, y=10.0)}])))
        (_, low), (_, high) = mesh.find_poly(5, 5, z=1.0), mesh.find_poly(5, 5, z=9.0)
        self.assertEqual((low, high), (0, 1))
        self.assertAlmostEqual(mesh.height(5, 5, z=1.0), 0.0, delta=1e-4)
        self.assertAlmostEqual(mesh.height(5, 5, z=9.0), 10.0, delta=1e-4)
        self.assertAlmostEqual(mesh.height(5, 5), 10.0, delta=1e-4)     # no z: the highest

    def test_off_mesh_polys_are_kept_but_never_hit(self):
        tile = read_tile(build_tile([{"verts": square(0, 10, 0, 10), "type": 1, "tris": []}],
                                    off_mesh=[((0, 0, 0, 0, 0, 0), 1.0, 0)]))
        self.assertEqual(tile.off_mesh[0].poly, 0)
        self.assertIsNone(NavMesh({}, {NavMesh({}, {}).tile_coords(5, 5): tile}).find_poly(5, 5))


class LoadTests(unittest.TestCase):
    def test_tile_coords_follow_the_map_grid_index(self):
        mesh = NavMesh({}, {})
        self.assertEqual(mesh.tile_coords(100, 100), (31, 31))    # ADT name floor(32 - x/533.33)
        self.assertEqual(mesh.tile_coords(-100, 600), (32, 30))

    def test_load_reads_mmap_params_and_tiles_of_that_map_only(self):
        mesh_data = build_tile([{"verts": square(0, 10, 0, 10)}])
        key = NavMesh({}, {}).tile_coords(5, 5)
        with tempfile.TemporaryDirectory() as tmp:
            mmaps = pathlib.Path(tmp)
            (mmaps / "530.mmap").write_bytes(struct.pack("<5f2i", 0, 0, 0, 533.3333, 533.3333, 64, 1024))
            (mmaps / f"530{key[0]:02}{key[1]:02}.mmtile").write_bytes(mesh_data)
            (mmaps / "001000000.mmtile").write_bytes(b"not read")   # another map: ignored
            mesh = NavMesh.load(mmaps, 530)
        self.assertEqual(mesh.params["maxPolys"], 1024)
        self.assertIsNotNone(mesh.find_poly(5, 5))


def at(dx, dz, h=0.0):
    """WoW (x, y, z) of a Detour (x, z) point; h is only the z hint for stacked floors."""
    x, y = wow(dx, dz)
    return x, y, h


class PathTests(unittest.TestCase):
    def test_open_square_is_a_straight_path_of_two_points(self):
        mesh = mesh_with(((25, 25), build_tile(grid(3, 3, 10))))
        path = mesh.find_path(at(3, 4), at(27, 21))
        self.assertEqual(path, [at(3, 4), at(27, 21)])

    def test_path_routes_around_a_wall_and_follows_the_terrain(self):
        # 5x5 cells of 10 with the centre cell removed: the hole is Detour [20, 30]^2 on a slope.
        mesh = mesh_with(((25, 25), build_tile(grid(5, 5, 10, missing=((2, 2),), height=sloped))))
        path = mesh.find_path(at(5, 25), at(45, 25))
        self.assertIsNotNone(path)
        self.assertGreater(len(path), 2)
        self.assertEqual(path[0][:2], at(5, 25)[:2])
        self.assertEqual(path[-1][:2], at(45, 25)[:2])
        for a, b in zip(path, path[1:], strict=False):
            self.assertFalse(crosses_cell(a, b, 20, 30, 20, 30))
        for x, y, z in path:
            self.assertAlmostEqual(z, mesh.height(x, y), delta=1e-3)
            self.assertAlmostEqual(z, sloped(y, x), delta=1e-3)   # Detour (x, z) = WoW (y, x)

    def test_unreachable_island_returns_none(self):
        mesh = mesh_with(((25, 25), build_tile(grid(3, 1, 10, missing=((1, 0),)))))
        self.assertIsNone(mesh.find_path(at(5, 5), at(25, 5)))

    def test_off_mesh_start_or_end_returns_none(self):
        mesh = mesh_with(((25, 25), build_tile(grid(2, 1, 10))))
        self.assertIsNone(mesh.find_path(at(50, 50), at(5, 5)))
        self.assertIsNone(mesh.find_path(at(5, 5), at(50, 50)))

    def test_start_and_end_in_one_poly_give_two_points(self):
        mesh = mesh_with(((5, 5), build_tile([{"verts": square(0, 10, 0, 10)}])))
        self.assertEqual(mesh.find_path(at(2, 3), at(7, 8)), [at(2, 3), at(7, 8)])

    def test_start_exactly_on_a_portal_edge(self):
        # Detour x = 10 is the edge between cells 0 and 1; the point belongs to cell 1 only.
        mesh = mesh_with(((5, 5), build_tile(grid(2, 1, 10))))
        self.assertEqual(mesh.find_path(at(10, 5), at(5, 5)), [at(10, 5), at(5, 5)])

    def test_end_exactly_on_a_portal_edge(self):
        mesh = mesh_with(((5, 5), build_tile(grid(2, 1, 10))))
        self.assertEqual(mesh.find_path(at(5, 5), at(10, 5)), [at(5, 5), at(10, 5)])

    def test_path_crosses_two_tiles_joined_by_ext_links(self):
        # Tile A: Detour [0, GRID]^2, its +x edge is side 0. Tile B: [GRID, 2 GRID] x [0, GRID], its -x
        # edge is side 4 (the opposite side). Tile B is keyed one step on the gy axis: (31, 30).
        a = build_tile([{"verts": square(0, GRID, 0, GRID), "neis": [0, DT_EXT_LINK | 0, 0, 0]}])
        b = build_tile([{"verts": square(GRID, 2 * GRID, 0, GRID), "neis": [0, 0, 0, DT_EXT_LINK | 4]}])
        mesh = mesh_with(((100, 100), a), ((100, 700), b))
        self.assertEqual(mesh.find_path(at(100, 100), at(700, 100)), [at(100, 100), at(700, 100)])

    def test_split_border_links_both_short_edges_of_the_neighbour(self):
        # Tile A has one long +x edge, z in [0, GRID]. Tile B has two short -x edges, z in [0, GRID/2]
        # and [GRID/2, GRID], so the border is split differently on the two sides (T-junction). The
        # path must cross into B2 at the point where the straight line meets the border.
        half = GRID / 2
        a = build_tile([{"verts": square(0, GRID, 0, GRID), "neis": [0, DT_EXT_LINK | 0, 0, 0]}])
        b1 = {"verts": square(GRID, 2 * GRID, 0, half), "neis": [0, 0, 2, DT_EXT_LINK | 4]}
        b2 = {"verts": square(GRID, 2 * GRID, half, GRID), "neis": [1, 0, 0, DT_EXT_LINK | 4]}
        b = build_tile([b1, b2])
        mesh = mesh_with(((100, 100), a), ((100, 700), b))
        start, end = at(100, 100), at(700, 400)     # Detour (100, 100) to (700, 400)
        path = mesh.find_path(start, end)
        self.assertEqual(path, [start, end])        # crossing at z = 316.7, inside B2's overlap
        crossing = 100 + (GRID - 100) * (300 / 600)  # z where x = GRID on the straight line
        self.assertTrue(half < crossing < GRID)

    def test_portal_is_the_clamped_overlap_not_the_full_edge(self):
        # One short B poly, z in [0, GRID/2], against the long A edge. The line from (100, 500) to
        # (700, 200) crosses x = GRID at z = 283, above the overlap, so the path bends at the
        # overlap's end (GRID, GRID/2). With the full edge as the portal the line would not bend.
        half = GRID / 2
        a = build_tile([{"verts": square(0, GRID, 0, GRID), "neis": [0, DT_EXT_LINK | 0, 0, 0]}])
        b = build_tile([{"verts": square(GRID, 2 * GRID, 0, half), "neis": [0, 0, 0, DT_EXT_LINK | 4]}])
        mesh = mesh_with(((100, 100), a), ((100, 700), b))
        path = mesh.find_path(at(100, 500), at(700, 200))     # Detour (100, 500) to (700, 200)
        self.assertEqual(len(path), 3)
        self.assertAlmostEqual(path[1][0], half, delta=1e-3)    # WoW x = Detour z
        self.assertAlmostEqual(path[1][1], GRID, delta=1e-3)    # WoW y = Detour x

    def test_edges_with_no_overlap_do_not_link(self):
        # A's border is z in [0, GRID/2]; B's -x edge is z in [300, GRID]. No overlap, so no link.
        a = build_tile([{"verts": square(0, GRID, 0, GRID / 2), "neis": [0, DT_EXT_LINK | 0, 0, 0]}])
        b = build_tile([{"verts": square(GRID, 2 * GRID, 300, GRID), "neis": [0, 0, 0, DT_EXT_LINK | 4]}])
        mesh = mesh_with(((100, 100), a), ((100, 700), b))
        self.assertIsNone(mesh.find_path(at(100, 100), at(700, 400)))

    def test_overlap_height_tolerance_is_the_neighbours_walkable_climb(self):
        # Border edges 5 apart in height. Detour links when the endpoint gap is within 2 * walkableClimb
        # of the neighbour tile (overlapSlabs, DetourNavMesh.cpp:31-58 with py = walkableClimb).
        a = build_tile([{"verts": square(0, GRID, 0, GRID), "neis": [0, DT_EXT_LINK | 0, 0, 0]}])
        b_low = build_tile([{"verts": square(GRID, 2 * GRID, 0, GRID, y=5.0),
                             "neis": [0, 0, 0, DT_EXT_LINK | 4]}], climb=1.0)
        b_high = build_tile([{"verts": square(GRID, 2 * GRID, 0, GRID, y=5.0),
                              "neis": [0, 0, 0, DT_EXT_LINK | 4]}], climb=3.0)
        low = mesh_with(((100, 100), a), ((100, 700), b_low))
        high = mesh_with(((100, 100), a), ((100, 700), b_high))
        self.assertIsNone(low.find_path(at(100, 100), at(700, 100)))
        self.assertIsNotNone(high.find_path(at(100, 100), at(700, 100)))

    def test_ext_link_only_reaches_the_tile_on_its_side(self):
        # The same two tiles, but A's edge says side 4 (-x): tile B is on side 0, so nothing links.
        a = build_tile([{"verts": square(0, GRID, 0, GRID), "neis": [0, DT_EXT_LINK | 4, 0, 0]}])
        b = build_tile([{"verts": square(GRID, 2 * GRID, 0, GRID), "neis": [0, 0, 0, DT_EXT_LINK | 4]}])
        mesh = mesh_with(((100, 100), a), ((100, 700), b))
        self.assertIsNone(mesh.find_path(at(100, 100), at(700, 100)))

    def test_two_tiles_joined_on_the_z_side(self):
        # Tile A: Detour [0, GRID]^2, its +z edge is side 2. Tile B sits on +z: WoW x in [GRID, 2 GRID],
        # so its key is one step on the gx axis, (30, 31). Its -z edge is side 6.
        a = build_tile([{"verts": square(0, GRID, 0, GRID), "neis": [0, 0, DT_EXT_LINK | 2, 0]}])
        b = build_tile([{"verts": square(0, GRID, GRID, 2 * GRID), "neis": [DT_EXT_LINK | 6, 0, 0, 0]}])
        mesh = mesh_with(((100, 100), a), ((700, 100), b))
        self.assertEqual(sorted(mesh.tiles), [(30, 31), (31, 31)])
        self.assertEqual(mesh.find_path(at(100, 100), at(100, 700)), [at(100, 100), at(100, 700)])

    def test_expansion_limit_returns_none(self):
        mesh = mesh_with(((25, 25), build_tile(grid(10, 10, 10))))
        self.assertIsNone(mesh.find_path(at(2, 2), at(98, 98), max_polys=3))
        self.assertIsNotNone(mesh.find_path(at(2, 2), at(98, 98)))

    def test_random_mazes_never_route_through_a_wall(self):
        # Seeded random walls in an 8x8 grid. Every returned segment must miss every wall cell.
        found = 0
        for seed in range(30):
            rng = random.Random(seed)
            cells = [(c, r) for r in range(8) for c in range(8)]
            missing = tuple(rc for rc in cells if rng.random() < 0.3)
            open_cells = [rc for rc in cells if rc not in missing]
            if len(open_cells) < 2:
                continue
            (c0, r0), (c1, r1) = rng.sample(open_cells, 2)
            mesh = mesh_with(((25, 25), build_tile(grid(8, 8, 10, missing=missing))))
            path = mesh.find_path(at(c0 * 10 + 5, r0 * 10 + 5), at(c1 * 10 + 5, r1 * 10 + 5))
            if path is None:
                continue
            found += 1
            for a, b in zip(path, path[1:], strict=False):
                for c, r in missing:
                    self.assertFalse(crosses_cell(a, b, c * 10, c * 10 + 10, r * 10, r * 10 + 10),
                                     f"seed {seed}: segment crosses wall cell {(c, r)}")
        self.assertGreater(found, 15)

    def test_search_of_a_100_poly_grid_is_under_50_ms(self):
        mesh = mesh_with(((25, 25), build_tile(grid(10, 10, 10))))
        start = time.perf_counter()
        path = mesh.find_path(at(2, 2), at(98, 98))
        elapsed = time.perf_counter() - start
        self.assertIsNotNone(path)
        self.assertLess(elapsed, 0.05)


if __name__ == "__main__":
    unittest.main()
