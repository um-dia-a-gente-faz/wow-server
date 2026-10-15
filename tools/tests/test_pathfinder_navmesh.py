"""tools/pathfinder/navmesh.py against the TrinityCore 3.3.5a movement-map layout (README.md has
the source line for each fact). build_tile writes that layout by hand, with its own format
strings, so a reader mistake shows up here as a failing test. Stdlib only.
"""
import pathlib
import struct
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from pathfinder.navmesh import NavMesh, NavMeshError, read_tile  # noqa: E402

MMAP_MAGIC = 0x4D4D4150
DT_MAGIC = 0x444E4156   # 'DNAV'
DT_VERSION = 7
MMAP_VERSION = 15


def pad4(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 4)


def build_tile(polys, *, off_mesh=(), links=2, mmap_magic=MMAP_MAGIC, mmap_version=MMAP_VERSION,
               dt_version=DT_VERSION, tile_magic=DT_MAGIC, tile_version=DT_VERSION):
    """One .mmtile file. polys: dicts with 'verts' (Detour points, CCW), optional 'tris' (local
    index triples; default fan), 'extra' (detail verts, referenced from index len(verts) on) and
    'type' (1 = off-mesh). off_mesh: (pos6, rad, poly index) connections."""
    verts, poly_recs, detail_recs, detail_verts, tris = [], [], [], [], []
    for p in polys:
        base, corners = len(verts), p["verts"]
        nv = len(corners)
        verts += corners
        idx = [base + i for i in range(nv)] + [0] * (6 - nv)
        poly_recs.append(struct.pack("<I6H6HHBB", 0, *idx, *[0] * 6, 0, nv, p.get("type", 0) << 6))
        extra = p.get("extra", [])
        local = p.get("tris", [(0, i, i + 1) for i in range(1, nv - 1)])
        detail_recs.append(struct.pack("<IIBBxx", len(detail_verts), len(tris), nv + len(extra), len(local)))
        detail_verts += extra
        tris += [(*t, 0) for t in local]
    header = struct.pack("<iiiiiI9i3f6ff", tile_magic, tile_version, 0, 0, 0, 0,
                         len(polys), len(verts), links, len(polys), len(detail_verts), len(tris),
                         0, len(off_mesh), 0, 0.0, 0.0, 0.0, *[0.0] * 6, 0.0)
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
        # A raised detail vertex at the centre (index 4 = poly vert count + 0) sets the centre height.
        corners = square(0, 10, 0, 10)
        fan = [(0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)]
        mesh = mesh_with(((5, 5), build_tile([{"verts": corners, "extra": [(5, 2.0, 5)], "tris": fan}])))
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


if __name__ == "__main__":
    unittest.main()
