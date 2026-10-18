"""Latency benchmark for navmesh.find_path on a synthetic block of tiles (stdlib only).

    python3 -m tools.pathfinder.bench [--tiles 4] [--cells 40] [--queries 200] [--profile]

The real .mmtile files live on the VM, so the default mesh is built here: a tiles x tiles block of
tiles, each cells x cells quad polys (40x40 = 1600, the size of a real Azeroth tile), a detail vertex
and four detail triangles per poly, about 8% wall cells, and ext links on every shared border. The
query set is fixed by --seed. Tiles are built as bytes, parsed with read_tile and handed to NavMesh as a dict; nothing is written
to disk. The index of each tile is built by the first query that touches it.
"""
from __future__ import annotations

import argparse
import cProfile
import pstats
import random
import struct
import time

from .navmesh import NavMesh, read_tile

GRID = 533.3333
EXT = 0x8000


def tile_bytes(ti: int, tj: int, tiles: int, cells: int, walls: set) -> bytes:
    """Tile at Detour x block ti and z block tj: file key (31 - tj, 31 - ti), see navmesh._grid_index."""
    cell = GRID / cells
    index = {}
    for r in range(cells):
        for c in range(cells):
            if (c, r) not in walls:
                index[c, r] = len(index)
    verts, recs, details, dverts, tris = [], [], [], [], []
    for c, r in index:
        x0, z0 = ti * GRID + c * cell, tj * GRID + r * cell
        corners = [(x0, z0), (x0 + cell, z0), (x0 + cell, z0 + cell), (x0, z0 + cell)]
        ys = [0.05 * x + 0.03 * z + 2 * ((x * 7 + z * 3) % 5 == 0) for x, z in corners]
        base = len(verts)
        verts += [(x, y, z) for (x, z), y in zip(corners, ys, strict=True)]
        neis = []
        for dc, dr, side in ((0, -1, 6), (1, 0, 0), (0, 1, 2), (-1, 0, 4)):   # edges 0..3
            nc, nr = c + dc, r + dr
            if (nc, nr) in index:
                neis.append(index[nc, nr] + 1)
            elif not (0 <= nc < cells and 0 <= nr < cells):
                di, dj = {0: (1, 0), 2: (0, 1), 4: (-1, 0), 6: (0, -1)}[side]
                neis.append(EXT | side if 0 <= ti + di < tiles and 0 <= tj + dj < tiles else 0)
            else:
                neis.append(0)
        recs.append(struct.pack("<I6H6HHBB", 0, *range(base, base + 4), 0, 0, *neis, 0, 0, 0, 4, 0))
        details.append(struct.pack("<IIBBxx", len(dverts), len(tris), 5, 4))
        dverts.append((x0 + cell / 2, sum(ys) / 4, z0 + cell / 2))
        tris += [(a, b, 4, 0) for a, b in ((0, 1), (1, 2), (2, 3), (3, 0))]
    pad = lambda b: b + b"\0" * (-len(b) % 4)  # noqa: E731
    header = struct.pack("<iiiiiI9i3f6ff", 0x444E4156, 7, 0, 0, 0, 0, len(recs), len(verts), 2,
                         len(recs), len(dverts), len(tris), 0, 0, 0, 0.0, 0.0, 0.5, *[0.0] * 6, 0.0)
    body = header + b"".join(pad(s) for s in (
        b"".join(struct.pack("<3f", *v) for v in verts), b"".join(recs), b"\0" * 32,
        b"".join(details), b"".join(struct.pack("<3f", *v) for v in dverts),
        b"".join(struct.pack("<4B", *t) for t in tris), b"", b""))
    return struct.pack("<IIIIB3x", 0x4D4D4150, 7, 15, len(body), 1) + body


def build(tiles: int, cells: int, seed: int) -> tuple[NavMesh, list]:
    """The mesh, parsed in memory (no files), and its walkable cells as WoW (x, y) points."""
    rng = random.Random(seed)
    parts, free = {}, []
    cell = GRID / cells
    for ti in range(tiles):
        for tj in range(tiles):
            walls = {(rng.randrange(cells), rng.randrange(cells)) for _ in range(cells * cells // 12)}
            parts[31 - tj, 31 - ti] = read_tile(tile_bytes(ti, tj, tiles, cells, walls))
            free += [(tj * GRID + (c + .5) * cell, ti * GRID + (r + .5) * cell)       # WoW x = Detour z
                     for r in range(cells) for c in range(cells) if (c, r) not in walls]
    return NavMesh({}, parts), free


def queries(free: list, count: int, seed: int) -> dict:
    """count pairs per band of straight-line distance in yards."""
    rng = random.Random(seed)
    out = {}
    for name, lo, hi in (("short 20-80", 20, 80), ("medium 80-300", 80, 300), ("long 300-1200", 300, 1200)):
        pairs = []
        while len(pairs) < count:
            a, b = rng.choice(free), rng.choice(free)
            if lo <= ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** .5 <= hi:
                pairs.append(((*a, 0.0), (*b, 0.0)))
        out[name] = pairs
    return out


def pct(ms: list, p: float) -> float:
    return sorted(ms)[min(len(ms) - 1, int(p * len(ms)))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiles", type=int, default=4)
    ap.add_argument("--cells", type=int, default=40)
    ap.add_argument("--queries", type=int, default=50, help="per band")
    ap.add_argument("--seed", type=int, default=848)
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()
    t0 = time.perf_counter()
    mesh, free = build(args.tiles, args.cells, args.seed)
    sets = queries(free, args.queries, args.seed)
    print(f"{args.tiles ** 2} tiles x {args.cells ** 2} polys; build + parse {time.perf_counter() - t0:.2f} s")
    if True:
        def run(pairs):
            ms, none = [], 0
            for s, e in pairs:
                t = time.perf_counter()
                none += mesh.find_path(s, e) is None
                ms.append((time.perf_counter() - t) * 1000)
            return ms, none

        allms = []
        prof = cProfile.Profile() if args.profile else None
        for name, pairs in sets.items():
            if prof:
                prof.enable()
            ms, none = run(pairs)
            if prof:
                prof.disable()
            allms += ms
            print(f"{name:14} p50 {pct(ms, .5):8.1f} ms  p95 {pct(ms, .95):8.1f} ms  max {max(ms):8.1f}  no_path {none}")
        print(f"{'all':14} p50 {pct(allms, .5):8.1f} ms  p95 {pct(allms, .95):8.1f} ms")
        if prof:
            pstats.Stats(prof).sort_stats("tottime").print_stats(12)


if __name__ == "__main__":
    main()
