#!/usr/bin/env python3
"""Build a land mask from TEXTURE, then fit the world->image transform.

Colour cannot separate land from sea on these parchment-style maps (both are warm
tones). Texture can: Blizzard's land art is full of detail (mountains, forests, coast
strokes) while the sea is a smooth gradient. So a local-variance map is a serviceable
land mask.

With the mask we can FIT the transform: scan scale/offset for the rect mapping that puts
the most real spawn points on high-variance pixels. If the fitted parameters cluster
around a constant pattern, that tells us how the art relates to the DBC rect.
"""
import os
import struct
import sys

from PIL import Image, ImageFilter, ImageDraw

AREA = int(sys.argv[1]) if len(sys.argv) > 1 else 3524
DBC = "/root/wow-dbc/WorldMapArea.dbc"
MAPS = "/opt/wowmap-test"
PTS = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pts530.txt"


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
left, right, top, bottom = F(row[6]), F(row[7]), F(row[4]), F(row[5])
print(f"area {AREA}: left={left:.1f} right={right:.1f} top={top:.1f} bottom={bottom:.1f}")

im = Image.open(os.path.join(MAPS, f"{AREA}.png")).convert("RGB")
W, H = im.size

# --- land mask: local variance ------------------------------------------------
g = im.convert("L")
blur = g.filter(ImageFilter.GaussianBlur(3))
# |original - blurred| highlights detail/texture
diff = Image.new("L", (W, H))
diff.putdata([abs(a - b) for a, b in zip(g.getdata(), blur.getdata())])
sd = diff.filter(ImageFilter.BoxBlur(6))          # smooth the detail map
mask = sd.load()
thresh = sorted(sd.getdata())[int(W * H * 0.55)]  # top 45% = "textured"
print(f"mask threshold={thresh}")

pts = []
for tok in open(PTS).read().strip().split(";"):
    if tok.strip():
        a, b = tok.split(",")[:2]
        pts.append((float(a), float(b)))

# save the mask for visual inspection
vis = im.copy()
dv = ImageDraw.Draw(vis)
for y in range(0, H, 4):
    for x in range(0, W, 4):
        if mask[x, y] >= thresh:
            dv.point((x, y), fill=(0, 255, 0))
vis.save(f"/opt/wowmap-test/{AREA}_mask.png")
print(f"mask saved: /opt/wowmap-test/{AREA}_mask.png")


def score(ix0, ix1, iy0, iy1):
    """ix/iy are the fractions of the image the rect maps onto (inset fit)."""
    hits = 0
    for wx, wy in pts:
        nx = (wx - left) / (right - left)
        ny = (top - wy) / (top - bottom)
        x = (ix0 + nx * (ix1 - ix0)) * W
        y = (iy0 + ny * (iy1 - iy0)) * H
        if 0 <= x < W and 0 <= y < H and mask[int(x), int(y)] >= thresh:
            hits += 1
    return hits / len(pts)


best = (0, 0, 0, 1, 1)
for iy0 in [i / 20 for i in range(0, 9)]:
    for iy1 in [1 - i / 20 for i in range(0, 9)]:
        if iy1 - iy0 < 0.3:
            continue
        for ix0 in [i / 20 for i in range(0, 9)]:
            for ix1 in [1 - i / 20 for i in range(0, 9)]:
                if ix1 - ix0 < 0.3:
                    continue
                s = score(ix0, ix1, iy0, iy1)
                if s > best[0]:
                    best = (s, ix0, ix1, iy0, iy1)

print(f"\nbest: {best[0] * 100:.1f}% of {len(pts)} points on textured area")
print(f"  x: {best[1]:.2f} .. {best[2]:.2f} of the image")
print(f"  y: {best[3]:.2f} .. {best[4]:.2f} of the image")
print(f"  baseline (0..1): {score(0, 1, 0, 1) * 100:.1f}%")
