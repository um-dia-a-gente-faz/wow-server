"""Item display id -> inventory icon, from ItemDisplayInfo.dbc (3.3.5a build 12340).

`world.item_template.displayid` is an ItemDisplayInfo.dbc record ID. That record's
InventoryIcon[0] string names a texture under `Interface\\Icons\\<name>.blp` in the
client MPQs. extract_icons.py converts those textures to `<icon_file(name)>` PNGs
in ICONS_DIR, and app.py serves them at `/icons/<file>`.

ItemDisplayInfo.dbc layout for 12340 (TrinityCore 3.3.5 DBCStructure.h, the
commented-out `ItemDisplayInfoEntry`; checked against the client's own file:
25 fields, 100-byte records):

    0      ID
    1-2    ModelName[2]
    3-4    ModelTexture[2]
    5-6    InventoryIcon[2]      <- field 5 is the item's icon
    7-9    GeosetGroup[3]
    10     Flags
    11     SpellVisualID
    12     GroupSoundIndex
    13-14  HelmetGeosetVisID[2]
    15-22  Texture[8]
    23     ItemVisual
    24     ParticleColorID

This is a small self-contained WDBC read. Once tools/dbc/wdbc.py (UM-52) is on
main, `load_display_icons` can switch to it.
"""
import re
import struct

ID_FIELD = 0
INVENTORY_ICON_FIELD = 5
EXPECTED_FIELDS = 25

_HEADER = struct.Struct("<4sIIII")
_SAFE = re.compile(r"[^a-z0-9_\-]")


def parse_display_icons(data):
    """{display_id: icon_name} for every ItemDisplayInfo record with an icon."""
    magic, n_rec, n_field, rec_size, str_size = _HEADER.unpack_from(data)
    if magic != b"WDBC":
        raise ValueError(f"not a WDBC file (magic={magic!r})")
    if n_field != EXPECTED_FIELDS or rec_size != n_field * 4:
        raise ValueError(f"unexpected ItemDisplayInfo layout: {n_field} fields, "
                         f"{rec_size}-byte records (want {EXPECTED_FIELDS}, 100)")
    strings_at = _HEADER.size + n_rec * rec_size
    strings_end = strings_at + str_size
    if strings_end > len(data):
        raise ValueError("truncated ItemDisplayInfo.dbc")
    out = {}
    for r in range(n_rec):
        base = _HEADER.size + r * rec_size
        display_id = struct.unpack_from("<I", data, base + ID_FIELD * 4)[0]
        off = struct.unpack_from("<I", data, base + INVENTORY_ICON_FIELD * 4)[0]
        if not off or strings_at + off >= strings_end:
            continue
        end = data.find(b"\x00", strings_at + off, strings_end)
        name = data[strings_at + off:end if end >= 0 else strings_end].decode("utf-8", "replace")
        if name:
            out[display_id] = name
    return out


def load_display_icons(path):
    with open(path, "rb") as f:
        return parse_display_icons(f.read())


def icon_file(name):
    """PNG file name for an icon: lowercased, path-safe ("INV_Sword_04" -> "inv_sword_04.png").

    MPQ lookups are case-insensitive but the DBC spells names inconsistently, so
    the extractor and the server both normalise through this one function.
    """
    stem = name.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    if stem.endswith(".blp"):
        stem = stem[:-4]
    stem = _SAFE.sub("_", stem)
    return f"{stem}.png" if stem else ""


def mpq_path(name):
    """Path of the icon texture inside the client MPQs."""
    return f"Interface\\Icons\\{name}.blp"
