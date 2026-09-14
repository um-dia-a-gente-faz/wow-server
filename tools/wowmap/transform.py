#!/usr/bin/env python3
"""World-coordinate -> map-image transform for WoW 3.3.5a (TrinityCore).

Reads the client DBCs that TrinityCore's mapextractor already produced
(`<server>/data/dbc/`) and converts a player's `characters.position_x/y` into a
normalised (0..1) position on that zone's world-map image.

## Why this exists

To draw "where are the players" on a real map you need the coordinate rect each
zone map covers. `WorldMapArea.dbc` holds it — but the four float fields are NOT
in (left, right, top, bottom) order. Taking them as (left,right,top,bottom) puts
EVERY real position *outside* its zone rect.

The actual layout of `WorldMapArea.dbc` (3.3.5, 11 fields):

    0 ID (int)            1 MapID (int)         2 AreaID (int)
    3 AreaName (string)   4 Y_a (float)         5 Y_b (float)
    6 X_a (float)         7 X_b (float)         <-- fields 6/7 are the X extent
    8 DisplayMapID (int)  9 DefaultDungeonFloor (int)  10 ParentWorldMapID (int)

i.e. the **Y extent comes first**. Verified against two independent datasets:
  - `world.playercreateinfo` start positions (races 10 Blood Elf, 11 Draenei)
  - `world.creature` spawn extents for zone 3524 (Azuremyst Isle)
With the swap, 5/5 land inside their zone rect; with the naive order, 0/5 do.

Note also: `characters.zone` holds the *parent* zone (3430 Eversong Woods) while
`playercreateinfo.zone` holds the sub-zone (3431 Sunstrider Isle). Both resolve
through the same `WorldMapArea` parent rect.

## Cross-database note

`AreaTable.dbc` gives zone NAMES. The TDB in use has **no `areatable` table**, so
zone names cannot be joined from SQL — read them from the DBC instead.

## Usage

    from transform import DbcTables
    t = DbcTables("/path/to/server/data/dbc")
    t.to_normalised(area_id=3430, world_x=10349.6, world_y=-6357.29)
    # -> (0.7892, 0.3797)   multiply by image width/height for pixels
"""
import os
import struct


class DbcTables:
    """Loads and queries WorldMapArea.dbc / AreaTable.dbc / Map.dbc."""

    def __init__(self, dbc_dir):
        self._wm, self._wm_str = self._read(os.path.join(dbc_dir, "WorldMapArea.dbc"))
        self._area, self._area_str = self._read(os.path.join(dbc_dir, "AreaTable.dbc"))
        self._map, self._map_str = self._read(os.path.join(dbc_dir, "Map.dbc"))

        # area_id -> (left, right, top, bottom) in world coordinates, taken from the
        # RAW field order. Do NOT normalise with min()/max() — that destroys the
        # orientation, and in this DBC left > right (the image X axis runs opposite to
        # world X), so min/max silently mirrors every marker horizontally.
        self.rects = {}
        for r in self._wm:
            if r[3] == 0:
                continue
            self.rects[r[2]] = (self._f(r[6]), self._f(r[7]),   # left, right
                                self._f(r[4]), self._f(r[5]))   # top, bottom

        self.area_names = {r[0]: self._s(self._area_str, r[11]) for r in self._area
                           if len(r) > 11}
        self.map_names = {r[0]: self._s(self._map_str, r[1]) for r in self._map
                          if len(r) > 1}
        self.area_map = {r[0]: r[1] for r in self._area if len(r) > 1}

    # ---- decoding helpers -------------------------------------------------
    @staticmethod
    def _read(path):
        with open(path, "rb") as f:
            data = f.read()
        magic, n_rec, n_field, _rec_size, str_size = struct.unpack("<4sIIII", data[:20])
        assert magic == b"WDBC", f"{path}: not a DBC (magic={magic!r})"
        pos, recs = 20, []
        for _ in range(n_rec):
            recs.append(struct.unpack(f"<{n_field}I", data[pos:pos + n_field * 4]))
            pos += n_field * 4
        return recs, data[pos:pos + str_size]

    @staticmethod
    def _f(u32):
        """DBC float columns are raw bits; decode rather than reading as int."""
        return struct.unpack("<f", struct.pack("<I", u32))[0]

    @staticmethod
    def _s(strings, off):
        if not off:
            return ""
        return strings[off:strings.find(b"\x00", off)].decode("utf-8", "replace")

    # ---- public API -------------------------------------------------------
    def zone_name(self, area_id):
        return self.area_names.get(area_id) or str(area_id)

    def map_name(self, map_id):
        return self.map_names.get(map_id) or str(map_id)

    def to_normalised(self, area_id, world_x, world_y):
        """World coords -> (0..1, 0..1) on that zone's map image, or None."""
        rect = self.rects.get(area_id)
        if not rect:
            return None
        left, right, top, bottom = rect
        if right == left or top == bottom:
            return None
        nx = (world_x - left) / (right - left)
        ny = (top - world_y) / (top - bottom)   # world Y grows up, image Y grows down
        return nx, ny

    def to_pixel(self, area_id, world_x, world_y, width, height):
        n = self.to_normalised(area_id, world_x, world_y)
        return None if n is None else (n[0] * width, n[1] * height)

    def zones_for_map(self, map_id):
        """[(area_id, name, rect), ...] for every zone tile on a map."""
        out = []
        for r in self._wm:
            if r[1] == map_id and r[3]:
                out.append((r[2], self._s(self._wm_str, r[3]), self.rects.get(r[2])))
        return out


if __name__ == "__main__":
    import sys
    t = DbcTables(sys.argv[1] if len(sys.argv) > 1 else "/root/wow-dbc")

    # validation set cross-checked against the live server
    CASES = [
        (3430, 10349.6, -6357.29, "Blood Elf start (playercreateinfo race 10)"),
        (3430, 10345.2, -6349.35, "character position (characters.position_*)"),
        (3524, -3961.64, -13931.2, "Draenei start (playercreateinfo race 11)"),
        (3524, -4786.0, -12186.0, "creature in Azuremyst (min corner)"),
        (3524, -4466.0, -11567.0, "creature in Azuremyst (max corner)"),
    ]
    print(f"{'case':48s} {'norm x':>7s} {'norm y':>7s}  inside?")
    for area, wx, wy, label in CASES:
        n = t.to_normalised(area, wx, wy)
        if n is None:
            print(f"{label:48s}   no rect for area {area}")
            continue
        ok = 0.0 <= n[0] <= 1.0 and 0.0 <= n[1] <= 1.0
        print(f"{label:48s} {n[0]:7.4f} {n[1]:7.4f}  {'YES' if ok else 'NO'}")
