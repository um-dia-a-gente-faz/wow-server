"""WorldMapOverlay.dbc: the explored-area art and subzone rects of each zone map.

Stdlib only, so both `app.py` (subzone names for `/api/areas`) and
`extract_maps.py` (compositing the coloured art) can use it.

## Layout (3.3.5a, build 12340)

`WorldMapOverlay.dbc` has 17 uint32 fields per record (68 bytes). Field names from
TrinityCore 3.3.5 `WorldMapOverlayEntry` (DBCStructure.h, fmt `nxiiiixxxxxxxxxxx`),
checked against the real file (988 records):

    0 ID              1 MapAreaID (WorldMapArea.dbc ID, not an AreaTable ID)
    2-5 AreaID[4]     AreaTable IDs the overlay reveals (0 = unused)
    6 MapPointX       7 MapPointY       (always 0 here)
    8 TextureName     string, "" for overlays that only have a hit rect
    9 TextureWidth    10 TextureHeight
    11 OffsetX        12 OffsetY        top-left of the art on the zone map
    13 HitRectTop     14 HitRectLeft    15 HitRectBottom   16 HitRectRight

Offsets and hit rects are pixels on the zone map's 1024x768 canvas (the 4x3 grid of
256 px base tiles), the same canvas `extract_maps.py` writes, so an overlay is
placed by its offset and needs no world-coordinate transform.

## Textures

The client (FrameXML `WorldMapFrame_Update`) splits an overlay into 256 px tiles:
`Interface\\WorldMap\\<Zone>\\<TextureName><N>.blp`, N = 1-based, row-major,
`ceil(width/256)` per row. The last column/row is drawn only `width % 256` (or
`height % 256`) pixels wide/tall, from a file padded up to the next power of two.
`tile_layout()` reproduces that.
"""
import os
import struct

TILE = 256
CANVAS_W, CANVAS_H = 1024, 768
N_FIELDS = 17


def read_dbc(path):
    """(records as uint32 tuples, string block) of a WDBC file."""
    with open(path, "rb") as f:
        return read_dbc_bytes(f.read(), path)


def read_dbc_bytes(data, path="<bytes>"):
    magic, n_rec, n_field, rec_size, str_size = struct.unpack("<4sIIII", data[:20])
    if magic != b"WDBC":
        raise ValueError(f"{path}: not a DBC (magic={magic!r})")
    pos, recs = 20, []
    for _ in range(n_rec):
        recs.append(struct.unpack_from(f"<{n_field}I", data, pos))
        pos += rec_size
    return recs, data[pos:pos + str_size]


def dbc_string(strings, off):
    if not off:
        return ""
    return strings[off:strings.find(b"\x00", off)].decode("utf-8", "replace")


def parse_overlays(records, strings):
    """WorldMapOverlay records -> list of dicts (see module docstring for fields)."""
    out = []
    for r in records:
        if len(r) < N_FIELDS:
            raise ValueError(f"WorldMapOverlay record has {len(r)} fields, expected {N_FIELDS}")
        top, left, bottom, right = r[13:17]
        out.append({
            "id": r[0],
            "map_area_id": r[1],
            "area_ids": [a for a in r[2:6] if a],
            "texture": dbc_string(strings, r[8]),
            "width": r[9], "height": r[10],
            "offset_x": r[11], "offset_y": r[12],
            # (x, y, w, h); None when the DBC leaves the hit rect empty
            "hit": (left, top, right - left, bottom - top) if right > left and bottom > top else None,
        })
    return out


def load_overlays(dbc_dir):
    """Overlays grouped by WorldMapArea ID, or {} when the DBC isn't there."""
    path = os.path.join(dbc_dir, "WorldMapOverlay.dbc")
    if not os.path.exists(path):
        return {}
    by_zone = {}
    for o in parse_overlays(*read_dbc(path)):
        by_zone.setdefault(o["map_area_id"], []).append(o)
    return by_zone


def _pow2_at_least(n, floor=16):
    size = floor
    while size < n:
        size *= 2
    return size


def tile_layout(width, height):
    """[(n, x, y, w, h, file_w, file_h), ...] for an overlay texture of width x height.

    `n` is the 1-based tile file number, (x, y) its top-left relative to the overlay
    offset, (w, h) the pixels drawn, (file_w, file_h) the BLP's own size. Mirrors the
    3.3.5 FrameXML loop in WorldMapFrame_Update.
    """
    if width <= 0 or height <= 0:
        return []
    wide, tall = -(-width // TILE), -(-height // TILE)
    tiles = []
    for j in range(tall):
        if j < tall - 1:
            h = fh = TILE
        else:
            h = height % TILE or TILE
            fh = _pow2_at_least(h)
        for k in range(wide):
            if k < wide - 1:
                w = fw = TILE
            else:
                w = width % TILE or TILE
                fw = _pow2_at_least(w)
            tiles.append((j * wide + k + 1, k * TILE, j * TILE, w, h, fw, fh))
    return tiles


def subzones(overlays, area_names):
    """API shape for one zone's overlays: names, art rect, hit rect and label point.

    `area_names` maps AreaTable ID -> name. The label sits at the centre of the hit
    rect (the area the client treats as the subzone), else of the art.
    """
    out = []
    for o in overlays:
        if not o["area_ids"]:
            continue
        names = [area_names.get(a) or str(a) for a in o["area_ids"]]
        art = None
        if o["texture"] and o["width"] and o["height"]:
            art = (o["offset_x"], o["offset_y"], o["width"], o["height"])
        box = o["hit"] or art
        if box is None:
            continue
        out.append({
            "id": o["id"],
            "area_ids": o["area_ids"],
            "name": names[0],
            "names": names,
            "art": list(art) if art else None,
            "hit": list(o["hit"]) if o["hit"] else None,
            "label": [round(box[0] + box[2] / 2), round(box[1] + box[3] / 2)],
        })
    return out
