#!/usr/bin/env python3
"""Extract WoW world-map art from the client MPQs and stitch it into per-zone PNGs.

Writes, per zone (`<area_id>` = WorldMapArea field 2, an AreaTable ID):

    <area_id>_base.png   the base art alone: the game's *unexplored* parchment
    <area_id>.png        base art + every WorldMapOverlay explored-area texture,
                         i.e. the zone as the game shows it fully explored

and, per overlay with a texture, `overlays/<overlay_id>.png`: that texture alone,
which the site stacks on the base art for a character's fog of war (fog.py).

Non-obvious things this handles:

1. The world map art lives in the **locale** MPQs (`<locale>/locale-XXxx.MPQ`, with
   `<locale>/patch-XXxx[-2,-3].MPQ` layered on top), not in the base/patch MPQs, and it
   is NOT in the locale MPQ's (listfile) — so it cannot be enumerated.
   The paths follow `Interface\\WorldMap\\<AreaName>\\<AreaName><N>.blp` where
   `<AreaName>` is the space-free name straight out of WorldMapArea.dbc
   (e.g. "EversongWoods"), and `<N>` is a 1-based tile index.

2. Each map is a **4x3 grid of 12 tiles, 256x256 each = 1024x768**, laid out
   left-to-right, top-to-bottom. Tiles are BLP2 with DXT compression.

3. Explored-area overlays come from `WorldMapOverlay.dbc` (see overlays.py): each is
   `Interface\\WorldMap\\<AreaName>\\<TextureName><N>.blp`, split into 256 px tiles,
   and pasted at its (OffsetX, OffsetY) on the same 1024x768 canvas.

BLP2 DXT payloads are wrapped in a minimal DDS header so Pillow can decode them —
writing a DXT decompressor by hand is unnecessary. The DXT variant comes from the
BLP's alpha encoding byte (0 = DXT1, 1 = DXT3, 7 = DXT5): base tiles are DXT1, most
overlays DXT3/DXT5.

Usage:
    python extract_maps.py --client /opt/wow-server/client --dbc /opt/wowmap-data/dbc --out /tmp/maps
    python extract_maps.py --only EversongWoods,Durotar      # a few zones, for testing
"""
import argparse
import io
import os
import struct
import sys

from PIL import Image

import overlays as ovl

TILE = ovl.TILE
GRID_COLS, GRID_ROWS = 4, 3
N_TILES = GRID_COLS * GRID_ROWS

# BLP2 header: magic, type (u32, always 1), compression (u8: 1 palette, 2 DXT,
# 3 raw BGRA), alphaDepth (u8), alphaEncoding (u8), hasMips (u8), width, height,
# then 16 mip offsets and 16 mip sizes, then (for palettes) 256 BGRA colours.
DXT_BY_ALPHA_ENCODING = {0: b"DXT1", 1: b"DXT3", 7: b"DXT5"}


def _dds(fourcc, width, height, payload):
    """Minimal DDS header so Pillow's DDS plugin decodes the DXT blocks for us."""
    hdr = bytearray()
    hdr += b"DDS " + struct.pack("<I", 124)
    hdr += struct.pack("<I", 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000)  # CAPS|HEIGHT|WIDTH|PIXELFORMAT|LINEARSIZE
    hdr += struct.pack("<II", height, width)
    hdr += struct.pack("<I", len(payload))       # pitch / linear size
    hdr += struct.pack("<I", 0)                  # depth
    hdr += struct.pack("<I", 1)                  # mip count
    hdr += b"\x00" * 44                          # reserved
    hdr += struct.pack("<I", 32) + struct.pack("<I", 0x4) + fourcc
    hdr += struct.pack("<IIIII", 0, 0, 0, 0, 0)
    hdr += struct.pack("<I", 0x1000)             # caps: TEXTURE
    hdr += b"\x00" * 16
    assert len(hdr) == 128, len(hdr)
    return bytes(hdr) + payload


def _palette_image(data, width, height, payload, alpha_depth):
    palette = data[148:148 + 1024]
    n = width * height
    idx = payload[:n]
    rgba = bytearray(n * 4)
    for i, p in enumerate(idx):
        b, g, r = palette[p * 4], palette[p * 4 + 1], palette[p * 4 + 2]
        rgba[i * 4:i * 4 + 3] = bytes((r, g, b))
    alpha = payload[n:]
    for i in range(n):
        if alpha_depth == 8:
            a = alpha[i]
        elif alpha_depth == 4:
            a = ((alpha[i // 2] >> (4 * (i % 2))) & 0xF) * 17
        elif alpha_depth == 1:
            a = 255 if alpha[i // 8] >> (i % 8) & 1 else 0
        else:
            a = 255
        rgba[i * 4 + 3] = a
    return Image.frombytes("RGBA", (width, height), bytes(rgba))


def blp_to_image(data):
    """Decode a BLP2 blob (DXT1/3/5, palettized or raw) into a PIL RGBA image."""
    if data[:4] != b"BLP2":
        raise ValueError(f"not BLP2 (magic={data[:4]!r})")
    _type, compression, alpha_depth, alpha_enc, _mips, width, height = \
        struct.unpack("<IBBBBII", data[4:20])
    offsets = struct.unpack("<16I", data[20:84])
    sizes = struct.unpack("<16I", data[84:148])
    payload = data[offsets[0]:offsets[0] + sizes[0]]
    if not payload:
        raise ValueError("empty mipmap 0")

    if compression == 2:
        fourcc = DXT_BY_ALPHA_ENCODING.get(alpha_enc)
        if fourcc is None:
            raise ValueError(f"unsupported BLP2 DXT alpha encoding {alpha_enc}")
        return Image.open(io.BytesIO(_dds(fourcc, width, height, payload))).convert("RGBA")
    if compression == 1:
        return _palette_image(data, width, height, payload, alpha_depth)
    if compression == 3:
        b, g, r, a = Image.frombytes("RGBA", (width, height), payload[:width * height * 4]).split()
        return Image.merge("RGBA", (r, g, b, a))
    raise ValueError(f"unsupported BLP2 compression {compression}")


def zones(dbc_dir, area_dbc="WorldMapArea.dbc"):
    """[(worldmaparea_id, area_id, dir_name), ...] for every named WorldMapArea row."""
    recs, strings = ovl.read_dbc(os.path.join(dbc_dir, area_dbc))
    return [(r[0], r[2], ovl.dbc_string(strings, r[3])) for r in recs if r[3]]


class ArchiveChain:
    """Several MPQs read in priority order, the way the client layers its patches."""

    def __init__(self, archives):
        self.archives = archives

    def read_file(self, path):
        for archive in self.archives:
            try:
                blob = archive.read_file(path)
            except Exception:  # noqa: BLE001 - mpyq raises plain Exceptions for missing files
                blob = None
            if blob:
                return blob
        return None


def read_blp(archive, path):
    blob = archive.read_file(path)
    if not blob:
        return None
    try:
        return blp_to_image(blob)
    except ValueError as e:
        print(f"  {path}: {e}")
        return None


def base_sheet(archive, name):
    """The zone's 4x3 base tiles as one 1024x768 image, or None without art."""
    tiles = [read_blp(archive, f"Interface\\WorldMap\\{name}\\{name}{n}.blp")
             for n in range(1, N_TILES + 1)]
    if not any(tiles):
        return None
    sheet = Image.new("RGBA", (GRID_COLS * TILE, GRID_ROWS * TILE), (0, 0, 0, 0))
    for i, t in enumerate(tiles):
        if t is not None:
            r, c = divmod(i, GRID_COLS)
            sheet.paste(t, (c * TILE, r * TILE))
    return sheet


def overlay_image(archive, name, o):
    """One overlay's texture, its tiles stitched into a width x height RGBA image.

    Returns (image, drawn, missing): tiles pasted and tile files not found. The
    image is None when the overlay has no texture or none of its tiles exist.
    """
    if not o["texture"]:
        return None, 0, 0
    art = Image.new("RGBA", (o["width"], o["height"]), (0, 0, 0, 0))
    drawn = missing = 0
    for n, x, y, w, h, _fw, _fh in ovl.tile_layout(o["width"], o["height"]):
        tile = read_blp(archive, f"Interface\\WorldMap\\{name}\\{o['texture']}{n}.blp")
        if tile is None:
            missing += 1
            continue
        # Draw the file 1:1 and clip it to the pixels FrameXML shows: some edge
        # tiles are bigger than the power of two the client assumes, with the
        # extra rows/columns transparent.
        art.paste(tile.crop((0, 0, min(w, tile.width), min(h, tile.height))), (x, y))
        drawn += 1
    return (art if drawn else None), drawn, missing


def composite_overlays(sheet, archive, name, overlays, save_dir=None):
    """Alpha-composite every overlay texture onto a copy of `sheet`.

    Returns (image, drawn, missing): tiles pasted and tile files not found. With
    `save_dir`, each overlay's own art is also written there as `<overlay_id>.png`.
    """
    out = sheet.copy()
    drawn = missing = 0
    for o in overlays:
        art, d, m = overlay_image(archive, name, o)
        drawn += d
        missing += m
        if art is None:
            continue
        if save_dir:
            art.save(os.path.join(save_dir, f"{o['id']}.png"), "PNG", optimize=True)
        layer = Image.new("RGBA", out.size, (0, 0, 0, 0))
        layer.paste(art, (o["offset_x"], o["offset_y"]))
        out = Image.alpha_composite(out, layer)
    return out, drawn, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default="/opt/wow-server/client")
    ap.add_argument("--dbc", default="/opt/wowmap-data/dbc",
                    help="directory with WorldMapArea.dbc and WorldMapOverlay.dbc")
    ap.add_argument("--out", default="/opt/wowmap-data/maps")
    ap.add_argument("--locale", default="enUS")
    ap.add_argument("--only", default="", help="comma-separated WorldMapArea names")
    ap.add_argument("--area-dbc", default="WorldMapArea.dbc")
    args = ap.parse_args()

    from mpyq import MPQArchive

    rows = zones(args.dbc, args.area_dbc)
    if args.only:
        want = {w.strip().lower() for w in args.only.split(",") if w.strip()}
        rows = [z for z in rows if z[2].lower() in want]
    overlays_by_zone = ovl.load_overlays(args.dbc)
    if not overlays_by_zone:
        print("WorldMapOverlay.dbc not found: writing base art only")

    # Highest priority first: the locale patches override the locale MPQ (some
    # Wrath overlays, e.g. ZulDrak\\Kolramas2.blp, exist only in patch-enUS.MPQ).
    locale_dir = os.path.join(args.client, "Data", args.locale)
    mpq_path = os.path.join(locale_dir, f"locale-{args.locale}.MPQ")
    if not os.path.exists(mpq_path):
        sys.exit(f"locale MPQ not found: {mpq_path}")
    names = [f"patch-{args.locale}-3.MPQ", f"patch-{args.locale}-2.MPQ",
             f"patch-{args.locale}.MPQ", f"locale-{args.locale}.MPQ"]
    paths = [os.path.join(locale_dir, n) for n in names
             if os.path.exists(os.path.join(locale_dir, n))]
    archive = ArchiveChain([MPQArchive(p, listfile=False) for p in paths])
    os.makedirs(args.out, exist_ok=True)
    overlay_dir = os.path.join(args.out, "overlays")    # fog.OVERLAY_DIR
    os.makedirs(overlay_dir, exist_ok=True)

    # Continents share area 0; as before, the last WorldMapArea row for an area wins.
    by_area = {area_id: (wma_id, name) for wma_id, area_id, name in rows}
    print(f"{len(by_area)} areas; MPQs (first wins): {', '.join(os.path.basename(p) for p in paths)}")
    done = skipped = 0
    for area_id, (wma_id, name) in sorted(by_area.items()):
        sheet = base_sheet(archive, name)
        if sheet is None:
            skipped += 1
            continue
        sheet.save(os.path.join(args.out, f"{area_id}_base.png"), "PNG", optimize=True)
        coloured, drawn, missing = composite_overlays(
            sheet, archive, name, overlays_by_zone.get(wma_id, []), save_dir=overlay_dir)
        coloured.save(os.path.join(args.out, f"{area_id}.png"), "PNG", optimize=True)
        note = f", {missing} overlay tiles missing" if missing else ""
        print(f"  {name:24s} area {area_id:5d} -> {area_id}.png ({drawn} overlay tiles{note})")
        done += 1

    print(f"\n{done} zone maps extracted, {skipped} without art, in {args.out}")


if __name__ == "__main__":
    main()
