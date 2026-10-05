import os
import pathlib
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402
import areas  # noqa: E402
import state  # noqa: E402
import overlays  # noqa: E402

# Real WorldMapOverlay.dbc rows (3.3.5a build 12340) for Eversong Woods, WorldMapArea 462,
# with field 8 (TextureName) replaced by its string. Silvermoon City's hit rect contains
# Ruins of Silvermoon's; The Dead Scar has no texture, only a hit rect.
ROWS = [
    (1127, 462, 3431, 3432, 0, 0, 0, 0, "SunstriderIsle", 512, 512, 195, 5, 27, 226, 188, 402),
    (1128, 462, 3434, 3460, 3461, 3462, 0, 0, "RuinsofSilvermoon", 256, 256, 307, 136, 223, 401, 303, 460),
    (1175, 462, 3487, 0, 0, 0, 0, 0, "SilvermoonCity", 512, 512, 440, 87, 190, 371, 338, 679),
    (1143, 462, 3468, 0, 0, 0, 0, 0, "TorWatha", 256, 353, 648, 315, 455, 677, 607, 765),
    (1384, 462, 3472, 0, 0, 0, 0, 0, "", 0, 0, 0, 0, 341, 485, 581, 535),
    (9999, 17, 0, 0, 0, 0, 0, 0, "Nothing", 0, 0, 0, 0, 0, 0, 0, 0),
]
AREA_NAMES = {3431: "Sunstrider Isle", 3432: "Shrine of Dath'Remar", 3434: "Ruins of Silvermoon",
              3460: "Skulking Row", 3487: "Silvermoon City", 3468: "Tor'Watha",
              3472: "The Dead Scar"}


def build_dbc(rows):
    """A WDBC file in the 17-field WorldMapOverlay layout."""
    strings = bytearray(b"\x00")
    records = bytearray()
    for row in rows:
        fields = list(row)
        name = fields[8]
        if name:
            fields[8] = len(strings)
            strings += name.encode() + b"\x00"
        else:
            fields[8] = 0
        records += struct.pack("<17I", *fields)
    header = struct.pack("<4sIIII", b"WDBC", len(rows), 17, 68, len(strings))
    return header + bytes(records) + bytes(strings)


class OverlayDbcTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        with open(os.path.join(self.dir.name, "WorldMapOverlay.dbc"), "wb") as f:
            f.write(build_dbc(ROWS))
        self.by_zone = overlays.load_overlays(self.dir.name)

    def test_groups_by_worldmaparea_and_reads_fields(self):
        self.assertEqual(sorted(self.by_zone), [17, 462])
        sunstrider = self.by_zone[462][0]
        self.assertEqual(sunstrider["area_ids"], [3431, 3432])
        self.assertEqual(sunstrider["texture"], "SunstriderIsle")
        self.assertEqual((sunstrider["width"], sunstrider["height"]), (512, 512))
        self.assertEqual((sunstrider["offset_x"], sunstrider["offset_y"]), (195, 5))
        # HitRect is stored top, left, bottom, right -> (x, y, w, h)
        self.assertEqual(sunstrider["hit"], (226, 27, 176, 161))

    def test_missing_dbc_gives_no_overlays(self):
        self.assertEqual(overlays.load_overlays(os.path.join(self.dir.name, "nope")), {})

    def test_subzones_api_shape(self):
        subs = overlays.subzones(self.by_zone[462], AREA_NAMES)
        by_name = {s["name"]: s for s in subs}
        self.assertEqual(set(by_name), {"Sunstrider Isle", "Ruins of Silvermoon", "Silvermoon City",
                                        "Tor'Watha", "The Dead Scar"})
        ruins = by_name["Ruins of Silvermoon"]
        self.assertEqual(ruins["names"], ["Ruins of Silvermoon", "Skulking Row", "3461", "3462"])
        self.assertEqual(ruins["art"], [307, 136, 256, 256])
        self.assertEqual(ruins["label"], [430, 263])   # centre of the hit rect
        dead_scar = by_name["The Dead Scar"]
        self.assertIsNone(dead_scar["art"])
        self.assertEqual(dead_scar["hit"], [485, 341, 50, 240])
        # overlays without area IDs or without any rect are dropped
        self.assertEqual(overlays.subzones(self.by_zone[17], AREA_NAMES), [])

    def test_label_falls_back_to_art_centre_without_hit_rect(self):
        row = list(ROWS[3])
        row[13:17] = [0, 0, 0, 0]
        o = overlays.parse_overlays(*overlays.read_dbc_bytes(build_dbc([row])))
        self.assertEqual(overlays.subzones(o, AREA_NAMES)[0]["label"], [776, 492])


class TileLayoutTests(unittest.TestCase):
    def test_exact_multiple_of_256(self):
        self.assertEqual(overlays.tile_layout(512, 256), [
            (1, 0, 0, 256, 256, 256, 256),
            (2, 256, 0, 256, 256, 256, 256),
        ])

    def test_partial_last_row_is_cropped_from_a_power_of_two_file(self):
        # Tor'Watha is 256x353: tile 2 draws 97 rows from a 256x128 file (as in the MPQ)
        self.assertEqual(overlays.tile_layout(256, 353), [
            (1, 0, 0, 256, 256, 256, 256),
            (2, 0, 256, 256, 97, 256, 128),
        ])

    def test_row_major_numbering(self):
        tiles = overlays.tile_layout(300, 300)
        self.assertEqual([(t[0], t[1], t[2]) for t in tiles],
                         [(1, 0, 0), (2, 256, 0), (3, 0, 256), (4, 256, 256)])
        self.assertEqual(tiles[3][3:], (44, 44, 64, 64))

    def test_empty(self):
        self.assertEqual(overlays.tile_layout(0, 0), [])


class FakeTables:
    """WorldMapArea row 462 (Eversong Woods, map 530) and the subzone names."""

    _wm = [(462, 530, 3430, 1, 0, 0, 0, 0, 0, 0, 0)]
    rects = {3430: (12996.0, 9466.0, -4950.0, -7250.0)}
    area_names = AREA_NAMES

    def zone_name(self, area_id):
        return "Eversong Woods"

    def map_name(self, map_id):
        return "Expansion01"


class FetchAreasTests(unittest.TestCase):
    def test_areas_carry_subzones_from_the_overlay_dbc(self):
        by_zone = overlays.parse_overlays(*overlays.read_dbc_bytes(build_dbc(ROWS)))
        grouped = {462: [o for o in by_zone if o["map_area_id"] == 462]}
        with mock.patch.object(state, "tables", return_value=FakeTables()), \
                mock.patch.object(state, "overlays", return_value=grouped):
            (eversong,) = areas.fetch_areas(530)
        self.assertEqual(eversong["area_id"], 3430)
        names = [s["name"] for s in eversong["subzones"]]
        self.assertIn("Sunstrider Isle", names)
        self.assertEqual(len(names), 5)

    def test_page_has_label_toggle_and_hover(self):
        self.assertIn('id="tglLabels"', app.PAGE)
        self.assertIn("function subzoneAt", app.PAGE)


try:
    from PIL import Image  # noqa: F401 - only extract_maps needs Pillow; CI may not have it
    import extract_maps
except ImportError:  # pragma: no cover
    extract_maps = None


@unittest.skipIf(extract_maps is None, "Pillow not installed")
class ExtractTests(unittest.TestCase):
    def test_blp_dxt_variant_comes_from_alpha_encoding(self):
        # 4x4 DXT5 block: alpha 255 everywhere, colour 0 = pure red
        block = bytes([255, 255]) + b"\x00" * 6 + struct.pack("<HH", 0xF800, 0xF800) + b"\x00" * 4
        header = b"BLP2" + struct.pack("<IBBBBII", 1, 2, 8, 7, 0, 4, 4)
        offsets = struct.pack("<16I", 148, *([0] * 15))
        sizes = struct.pack("<16I", len(block), *([0] * 15))
        image = extract_maps.blp_to_image(header + offsets + sizes + block)
        self.assertEqual(image.size, (4, 4))
        self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 255))

    def test_overlay_is_pasted_at_its_offset(self):
        sheet = Image.new("RGBA", (1024, 768), (0, 0, 0, 255))

        class Archive:
            def read_file(self, path):
                return path

        tile = Image.new("RGBA", (256, 128), (0, 255, 0, 255))
        overlay = {"texture": "TorWatha", "width": 256, "height": 353, "offset_x": 648, "offset_y": 315}
        with mock.patch.object(extract_maps, "read_blp", return_value=tile):
            out, drawn, missing = extract_maps.composite_overlays(sheet, Archive(), "EversongWoods", [overlay])
        self.assertEqual((drawn, missing), (2, 0))
        self.assertEqual(out.getpixel((648, 315)), (0, 255, 0, 255))
        self.assertEqual(out.getpixel((647, 315)), (0, 0, 0, 255))
        # the 128-row first tile file only covers rows 315..442; row 2 is cropped to 97
        self.assertEqual(out.getpixel((700, 315 + 256 + 96)), (0, 255, 0, 255))
        self.assertEqual(out.getpixel((700, 315 + 256 + 97)), (0, 0, 0, 255))


if __name__ == "__main__":
    unittest.main()
