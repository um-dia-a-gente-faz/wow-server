#!/usr/bin/env python3
"""Extract WoW world-map art from the client MPQs and stitch it into per-zone PNGs.

Two non-obvious things this handles:

1. The world map art lives in the **locale** MPQs (`<locale>/locale-XXxx.MPQ`), not in
   the base/patch MPQs, and it is NOT in the (listfile) — so it cannot be enumerated.
   The paths follow `Interface\\WorldMap\\<AreaName>\\<AreaName><N>.blp` where
   `<AreaName>` is the space-free name straight out of WorldMapArea.dbc
   (e.g. "EversongWoods"), and `<N>` is a 1-based tile index.

2. Each map is a **4x3 grid of 12 tiles, 256x256 each = 1024x768**, laid out
   left-to-right, top-to-bottom. Tiles are BLP2 with DXT compression.

BLP2 DXT payloads are wrapped in a minimal DDS header so Pillow can decode them —
writing a DXT decompressor by hand is unnecessary.

Usage:
    python extract_maps.py --client /opt/wow-server/client --dbc /root/wow-dbc --out /opt/wowmap/maps
    python extract_maps.py --only EversongWoods,Durotar      # a few zones, for testing
"""
import argparse
import os
import struct
import sys

from PIL import Image

TILE = 256
GRID_COLS, GRID_ROWS = 4, 3
N_TILES = GRID_COLS * GRID_ROWS

DDS_FOURCC = {1: b"DXT1", 2: b"DXT3", 3: b"DXT5"}


def blp_to_image(data):
    """Decode a BLP2 (DXT-compressed) blob into a PIL image via a DDS wrapper."""
    if data[:4] != b"BLP2":
        raise ValueError(f"not BLP2 (magic={data[:4]!r})")
    compression, _flags, width, height = struct.unpack("<4I", data[4:20])
    if compression not in DDS_FOURCC:
        raise ValueError(f"unsupported BLP2 compression {compression} (need DXT1/3/5)")

    offsets = struct.unpack("<16I", data[20:84])
    sizes = struct.unpack("<16I", data[84:148])
    mip0_off, mip0_size = offsets[0], sizes[0]
    payload = data[mip0_off:mip0_off + mip0_size]
    if not payload:
        raise ValueError("empty mipmap 0")

    # Minimal DDS header so Pillow's DDS plugin decodes the DXT blocks for us.
    hdr = bytearray()
    hdr += b"DDS " + struct.pack("<I", 124)
    hdr += struct.pack("<I", 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000)  # CAPS|HEIGHT|WIDTH|PIXELFORMAT|LINEARSIZE
    hdr += struct.pack("<II", height, width)
    hdr += struct.pack("<I", len(payload))       # pitch / linear size
    hdr += struct.pack("<I", 0)                  # depth
    hdr += struct.pack("<I", 1)                  # mip count
    hdr += b"\x00" * 44                          # reserved
    hdr += struct.pack("<I", 32) + struct.pack("<I", 0x4) + DDS_FOURCC[compression]
    hdr += struct.pack("<IIIII", 0, 0, 0, 0, 0)
    hdr += struct.pack("<I", 0x1000)             # caps: TEXTURE
    hdr += b"\x00" * 16
    assert len(hdr) == 128, len(hdr)

    import io
    return Image.open(io.BytesIO(bytes(hdr) + payload)).convert("RGBA")


def area_names(dbc_path):
    d = open(dbc_path, "rb").read()
    _, nrec, nf, _, ss = struct.unpack("<4sIIII", d[:20])
    pos, recs = 20, []
    for _ in range(nrec):
        recs.append(struct.unpack(f"<{nf}I", d[pos:pos + nf * 4]))
        pos += nf * 4
    sb = d[pos:pos + ss]
    out = {}
    for r in recs:
        if r[3]:
            out[r[2]] = sb[r[3]:sb.find(b"\x00", r[3])].decode("utf-8", "replace")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default="/opt/wow-server/client")
    ap.add_argument("--dbc", default="/root/wow-dbc")
    ap.add_argument("--out", default="/opt/wowmap/maps")
    ap.add_argument("--locale", default="enUS")
    ap.add_argument("--only", default="")
    ap.add_argument("--area-dbc", default="WorldMapArea.dbc")
    args = ap.parse_args()

    from mpyq import MPQArchive

    names = area_names(os.path.join(args.dbc, args.area_dbc))
    if args.only:
        want = {w.strip().lower() for w in args.only.split(",") if w.strip()}
        names = {k: v for k, v in names.items() if v.lower() in want}

    mpq_path = os.path.join(args.client, "Data", args.locale, f"locale-{args.locale}.MPQ")
    if not os.path.exists(mpq_path):
        sys.exit(f"locale MPQ not found: {mpq_path}")
    archive = MPQArchive(mpq_path)
    os.makedirs(args.out, exist_ok=True)

    print(f"{len(names)} areas; locale MPQ {mpq_path}")
    done = skipped = 0
    for area_id, name in sorted(names.items()):
        tiles = []
        for n in range(1, N_TILES + 1):
            try:
                blob = archive.read_file(f"Interface\\WorldMap\\{name}\\{name}{n}.blp")
            except Exception:  # noqa: BLE001
                blob = None
            if not blob:
                tiles.append(None)
                continue
            try:
                tiles.append(blp_to_image(blob))
            except Exception as e:  # noqa: BLE001
                print(f"  {name} tile {n}: {e}")
                tiles.append(None)

        if not any(tiles):
            skipped += 1
            continue

        sheet = Image.new("RGBA", (GRID_COLS * TILE, GRID_ROWS * TILE), (0, 0, 0, 0))
        for i, t in enumerate(tiles):
            if t is None:
                continue
            r, c = divmod(i, GRID_COLS)
            sheet.paste(t, (c * TILE, r * TILE))

        out = os.path.join(args.out, f"{area_id}.png")
        sheet.save(out, "PNG", optimize=True)
        have = sum(1 for t in tiles if t is not None)
        print(f"  {name:24s} area {area_id:5d} -> {os.path.basename(out)} ({have}/12 tiles)")
        done += 1

    print(f"\n{done} maps extracted, {skipped} without art, in {args.out}")


if __name__ == "__main__":
    main()
