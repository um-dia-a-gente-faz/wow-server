#!/usr/bin/env python3
"""Overlay known real positions onto an extracted zone map — the decisive transform test.

If the transform (and the tile assembly) is correct, every known point lands on LAND
in a sensible arrangement. If the Y axis is flipped or the rect fields are misordered,
points land in the sea or mirrored.

Points: world.creature spawns for the zone + world.playercreateinfo start positions.
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
    ap.add_argument("--points", required=True,
                    help="'x,y,label;x,y,label' (world coords)")
    args = ap.parse_args()

    t = DbcTables(args.dbc)
    src = os.path.join(args.maps, f"{args.area}.png")
    if not os.path.exists(src):
        sys.exit(f"missing {src}")
    im = Image.open(src).convert("RGBA")
    W, H = im.size
    d = ImageDraw.Draw(im)

    rect = t.rects.get(args.area)
    print(f"area {args.area} ({t.zone_name(args.area)}) rect x[{rect[0]:.0f},{rect[1]:.0f}] "
          f"y[{rect[2]:.0f},{rect[3]:.0f}]  image {W}x{H}")

    for spec in args.points.split(";"):
        spec = spec.strip()
        if not spec:
            continue
        wx, wy, label = spec.split(",", 2)
        wx, wy = float(wx), float(wy)
        p = t.to_pixel(args.area, wx, wy)
        if p is None:
            continue
        x, y = p
        land = "?" if not (0 <= x < W and 0 <= y < H) else ""
        # crosshair + label
        d.line([(x - 14, y), (x + 14, y)], fill=(255, 0, 0, 255), width=3)
        d.line([(x, y - 14), (x, y + 14)], fill=(255, 0, 0, 255), width=3)
        d.ellipse([x - 6, y - 6, x + 6, y + 6], outline=(255, 255, 0, 255), width=3)
        d.text((x + 12, y + 8), f"{label} ({x:.0f},{y:.0f})", fill=(255, 255, 0, 255))
        print(f"  {label:28s} world({wx:.0f},{wy:.0f}) -> px({x:.0f},{y:.0f}) {land}")

    im.save(args.out, "PNG")
    print("saved:", args.out)


if __name__ == "__main__":
    main()
