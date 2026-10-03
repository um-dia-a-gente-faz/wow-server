"""Fog of war: the zone map as one character sees it in game.

The game colours a zone map area by area as the character explores it; the rest
stays parchment. The data behind that:

- `characters.exploredZones` is `PLAYER_EXPLORED_ZONES_1..128` written out as 128
  space-separated uint32 values (TrinityCore 3.3.5 `Player::SaveToDB`, read back by
  `_LoadIntoDataField` in `Player::LoadFromDB`).
- Each `AreaTable.dbc` row has an explore bit in field 3 (`AreaBit`, called
  `exploreFlag` in older sources). `Player::CheckAreaExploreAndOutdoor` sets bit
  `AreaBit % 32` of value `AreaBit / 32` when the character enters the area. In the
  12340 `AreaTable.dbc` all 2307 rows have a distinct bit, the highest 3617, so they
  fit the 128 * 32 = 4096 bits.
- Each `WorldMapOverlay.dbc` row lists up to four AreaTable IDs (see overlays.py).
  The overlay is drawn once any of them is explored. TrinityCore doesn't use this
  DBC, so that rule is the client's and is not checked against server source.

`extract_maps.py` writes every overlay texture to `<maps>/overlays/<overlay_id>.png`;
`compose()` stacks the revealed ones on `<area_id>_base.png`. Pillow is imported
only there, so the rest of this module is stdlib.
"""
import collections
import io
import os
import threading

OVERLAY_DIR = "overlays"
EXPLORED_VALUES = 128          # PLAYER_EXPLORED_ZONES_SIZE
CACHE_SIZE = 24                # composed PNGs kept in memory, about 1.5 MB each


def explored_bits(text):
    """The set of explore bits in a `characters.exploredZones` value.

    Anything that isn't a uint32 counts as 0, the way the server's partial load
    leaves the remaining values unset.
    """
    bits = set()
    for index, token in enumerate((text or "").split()[:EXPLORED_VALUES]):
        try:
            value = int(token)
        except ValueError:
            continue
        if not 0 <= value <= 0xFFFFFFFF:
            continue
        while value:
            low = value & -value
            bits.add(index * 32 + low.bit_length() - 1)
            value ^= low
    return bits


def revealed(overlays, area_bits, bits):
    """IDs of the overlays a character with explore bits `bits` has revealed, sorted.

    `area_bits` maps AreaTable ID -> explore bit.
    """
    return sorted(o["id"] for o in overlays
                  if any(area_bits.get(a) in bits for a in o["area_ids"]))


def overlay_path(maps_dir, overlay_id):
    return os.path.join(maps_dir, OVERLAY_DIR, f"{int(overlay_id)}.png")


def textured(overlays):
    return [o for o in overlays if o["texture"] and o["width"] and o["height"]]


def has_art(maps_dir, area_id, overlays):
    """True when the per-overlay art for a zone has been extracted."""
    with_art = textured(overlays)
    return (bool(with_art)
            and os.path.isfile(os.path.join(maps_dir, f"{int(area_id)}_base.png"))
            and all(os.path.isfile(overlay_path(maps_dir, o["id"])) for o in with_art))


_cache = collections.OrderedDict()
_cache_lock = threading.Lock()


def compose(maps_dir, area_id, overlays, ids):
    """PNG bytes: the zone's base art plus the overlays in `ids`, in DBC order (the
    order `extract_maps.py` draws them in). None when the base art is missing.

    Overlays without a texture or without an extracted file are skipped.
    """
    base_path = os.path.join(maps_dir, f"{int(area_id)}_base.png")
    try:
        stamp = os.stat(base_path).st_mtime_ns
    except OSError:
        return None
    wanted = set(ids)
    chosen = [o for o in textured(overlays) if o["id"] in wanted]
    key = (maps_dir, int(area_id), stamp, tuple(o["id"] for o in chosen))
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    from PIL import Image

    with Image.open(base_path) as base:
        out = base.convert("RGBA")
    for o in chosen:
        try:
            with Image.open(overlay_path(maps_dir, o["id"])) as art:
                art = art.convert("RGBA")
        except OSError:
            continue
        layer = Image.new("RGBA", out.size, (0, 0, 0, 0))
        layer.paste(art, (o["offset_x"], o["offset_y"]))
        out = Image.alpha_composite(out, layer)
    buf = io.BytesIO()
    out.save(buf, "PNG", compress_level=3)
    data = buf.getvalue()
    with _cache_lock:
        _cache[key] = data
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return data
