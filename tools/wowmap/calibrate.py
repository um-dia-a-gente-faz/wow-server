#!/usr/bin/env python3
"""Brute-force the world->image transform against real spawn data.

Instead of guessing field order / axis orientation, enumerate the plausible variants and
score each by how many real spawn points land on LAND pixels. The map art is warm-toned
(parchment/land) over cool-toned (sea), so `r > b` is a serviceable land classifier.

This is the check that settles the WorldMapArea layout empirically.
"""
import itertools
import os
import struct
import sys

from PIL import Image

DBC = "/root/wow-dbc/WorldMapArea.dbc"
MAPS = "/opt/wowmap-test"
PTS = "/tmp/pts.txt"
AREA = int(sys.argv[1]) if len(sys.argv) > 1 else 3524


def read_dbc(path):
    d = open(path, "rb").read()
    _, nrec, nf, _, ss = struct.unpack("<4sIIII", d[:20])
    pos, recs = 20, []
    for _ in range(nrec):
        recs.append(struct.unpack(f"<{nf}I", d[pos:pos + nf * 4]))
        pos += nf * 4
    return recs, d[pos:pos + ss]


def F(u):
    return struct.unpack("<f", struct.pack("<I", u))[0]


recs, _ = read_dbc(DBC)
row = next(r for r in recs if r[2] == AREA)
f4, f5, f6, f7 = F(row[4]), F(row[5]), F(row[6]), F(row[7])

im = Image.open(os.path.join(MAPS, f"{AREA}.png")).convert("RGB")
W, H = im.size
px = im.load()

pts = []
for tok in open(PTS).read().strip().split(";"):
    if not tok.strip():
        continue
    a, b = tok.split(",")[:2]
    pts.append((float(a), float(b)))


def is_land(x, y):
    if not (0 <= x < W and 0 <= y < H):
        return False
    r, g, b = px[int(x), int(y)]
    return r > b + 8          # warm = land, cool = sea


results = []
# which field pair is the X extent (the other pair is Y), and axis orientation
for xpair, ypair in (("67", "45"), ("45", "67")):
    xa, xb = (f6, f7) if xpair == "67" else (f4, f5)
    ya, yb = (f4, f5) if xpair == "67" else (f6, f7)
    for flipx in (False, True):
        for flipy in (False, True):
            hits = 0
            for wx, wy in pts:
                nx = (wx - xa) / (xb - xa)
                ny = (wy - ya) / (yb - ya)
                if flipx:
                    nx = 1 - nx
                if flipy:
                    ny = 1 - ny
                if is_land(nx * W, ny * H):
                    hits += 1
            results.append((hits / len(pts), xpair, ypair, flipx, flipy))

results.sort(reverse=True)
print(f"area {AREA}: {len(pts)} pontos reais, imagem {W}x{H}")
print(f"campos brutos f4..f7 = {f4}, {f5}, {f6}, {f7}\n")
print(f"{'land%':>7s}  Xpair  Ypair  flipX  flipY")
for frac, xp, yp, fx, fy in results:
    mark = "  <-- melhor" if frac == results[0][0] else ""
    print(f"{frac * 100:6.1f}%  {xp:5s}  {yp:5s}  {str(fx):5s}  {str(fy):5s}{mark}")
