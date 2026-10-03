#!/usr/bin/env python3
"""Dense scatter test: plot many real spawn positions onto a zone map.

If the WorldMapArea rect and the tile assembly are right, the dots should trace the
zone's landmass. Systematic error (wrong field order, flipped axis, off-by-one rect)
shows up immediately as dots in the sea or mirrored.
"""
import argparse
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from transform import DbcTables  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbc", default="/root/wow-dbc")
    ap.add_argument("--maps", default="/opt/wowmap-test")
    ap.add_argument("--area", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--points-file", required=True)
    # Optional per-zone inset: the rect may map onto only part of the art.
    ap.add_argument("--ix0", type=float, default=0.0)
    ap.add_argument("--ix1", type=float, default=1.0)
    ap.add_argument("--iy0", type=float, default=0.0)
    ap.add_argument("--iy1", type=float, default=1.0)
    args = ap.parse_args()

    t = DbcTables(args.dbc)
    src = os.path.join(args.maps, f"{args.area}.png")
    im = Image.open(src).convert("RGBA")
    W, H = im.size
    d = ImageDraw.Draw(im, "RGBA")

    raw = open(args.points_file).read().strip()
    pts = [p for p in raw.split(";") if p.strip()]
    n_ok = n_out = 0
    for p in pts:
        try:
            wx, wy = (float(v) for v in p.split(",")[:2])
        except ValueError:
            continue
        n = t.to_normalised(args.area, wx, wy)
        if n is None:
            continue
        x = (args.ix0 + n[0] * (args.ix1 - args.ix0)) * W
        y = (args.iy0 + n[1] * (args.iy1 - args.iy0)) * H
        inside = 0 <= x < W and 0 <= y < H
        n_ok += inside
        n_out += not inside
        d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(255, 30, 30, 230))

    im.save(args.out, "PNG")
    print(f"area {args.area} ({t.zone_name(args.area)}): {n_ok} inside, {n_out} outside -> {args.out}")


if __name__ == "__main__":
    main()