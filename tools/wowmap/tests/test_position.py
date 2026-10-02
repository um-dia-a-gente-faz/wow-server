"""Continent / subzone / in-game map coordinates (UM-75), on synthetic DBC and .map files.

The numbers are the real 3.3.5a values for Eversong Woods (WorldMapArea row 462) and
Rubens' saved position, checked against the client DBCs and the server's 5301243.map.
"""
import pathlib
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from transform import DbcTables, GridAreas  # noqa: E402


def f32(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def write_dbc(path, n_field, rows):
    """rows: lists of int fields, where a str field is added to the string block."""
    strings = bytearray(b"\x00")
    records = []
    for row in rows:
        rec = []
        for value in row + [0] * (n_field - len(row)):
            if isinstance(value, str):
                rec.append(len(strings))
                strings += value.encode() + b"\x00"
            else:
                rec.append(value)
        records.append(struct.pack(f"<{n_field}I", *rec))
    with open(path, "wb") as f:
        f.write(struct.pack("<4sIIII", b"WDBC", len(rows), n_field, n_field * 4, len(strings)))
        f.write(b"".join(records) + bytes(strings))


def write_map(path, area):
    """A TrinityCore map file with only an AREA section: an int or 256 ids."""
    if isinstance(area, int):
        section = struct.pack("<4sHH", b"AREA", 0x0001, area)
    else:
        section = struct.pack("<4sHH", b"AREA", 0, 0) + struct.pack("<256H", *area)
    header = struct.pack("<4s10I", b"MAPS", 10, 12340, 44, len(section), 0, 0, 0, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(header + section)


RUBENS = (530, 10337.1, -6359.9)


class PositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(cls.tmp.name)
        # WorldMapArea: ID, MapID, AreaID, AreaName, then fields 4-7 as floats.
        write_dbc(d / "WorldMapArea.dbc", 11, [
            [462, 530, 3430, "EversongWoods",
             f32(-4487.5), f32(-9412.5), f32(11041.667), f32(7758.333)],
        ])
        # AreaTable: ID, ContinentID, ParentAreaID, ..., field 11 = name (enUS).
        area = lambda aid, parent, name: [aid, 530, parent] + [0] * 8 + [name]  # noqa: E731
        write_dbc(d / "AreaTable.dbc", 36, [
            area(3430, 0, "Eversong Woods"),
            area(3431, 3430, "Sunstrider Isle"),
            area(3433, 0, "Ghostlands"),
            area(3517, 3433, "Windrunner Village"),
        ])
        # Map: ID, Directory, ..., field 5 = MapName_lang[enUS].
        write_dbc(d / "Map.dbc", 66, [
            [530, "Expansion01", 0, 0, 0, "Outland"],
            [36, "DeadminesInstance", 1, 0, 0, "Deadmines"],
        ])
        cls.t = DbcTables(str(d))

        # Grid (12, 43) holds Rubens: cell (9, 14) is Sunstrider Isle, the rest the zone.
        cells = [3430] * 256
        cells[9 * 16 + 14] = 3431
        write_map(d / "5301243.map", cells)
        write_map(d / "5301244.map", 3433)          # a whole grid of one area
        (d / "5301245.map").write_bytes(b"junk")    # not a map file
        cls.grid = GridAreas(str(d))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_continent_is_the_display_name_not_the_directory(self):
        self.assertEqual(self.t.map_name(530), "Expansion01")
        self.assertEqual(self.t.continent_name(530), "Outland")
        self.assertEqual(self.t.continent_name(36), "Deadmines")
        self.assertEqual(self.t.continent_name(9999), "9999")

    def test_game_coords_match_the_in_game_map(self):
        # Sunstrider Isle reads about 38, 21 on the game's Eversong Woods map.
        x, y = self.t.game_coords(3430, RUBENS[1], RUBENS[2])
        self.assertAlmostEqual(x, 38.02, places=2)
        self.assertAlmostEqual(y, 21.46, places=2)
        self.assertIsNone(self.t.game_coords(1581, 0.0, 0.0))

    def test_subzone_from_the_area_grid(self):
        area = self.grid.area_id(*RUBENS)
        self.assertEqual(area, 3431)
        self.assertEqual(self.t.area_parent[area], 3430)
        # 40 yards east (world Y) is the next cell: the zone itself.
        self.assertEqual(self.grid.area_id(530, RUBENS[1], RUBENS[2] - 40), 3430)

    def test_single_area_grid_missing_and_corrupt_files(self):
        y_next_grid = -6359.9 - 533.3333   # gy 44
        self.assertEqual(self.grid.area_id(530, RUBENS[1], y_next_grid), 3433)
        self.assertEqual(self.grid.area_id(530, RUBENS[1], y_next_grid - 533.3333), 0)
        self.assertEqual(self.grid.area_id(1, 0.0, 0.0), 0)
        self.assertEqual(self.grid.area_id(530, 99999.0, 0.0), 0)


if __name__ == "__main__":
    unittest.main()
