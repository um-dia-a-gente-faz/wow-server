#!/usr/bin/env python3
"""Throwaway art builder for the UM-77 map-engine spike (see docs/MAP_ENGINE_SPIKE.md).

Reads the user-supplied 3.3.5a client (read-only) and writes, under --out:

    dbc/                      WorldMapArea, WorldMapContinent, WorldMapOverlay, AreaTable, Map
    continents/<Name>.png     the continent world-map art (1024x768 sheet, 1002x668 visible)
    zones/<area_id>.png       zone art, via tools/wowmap/extract_maps.py (--zones)
    tiles/<Dir>/<z>/<x>/<y>.jpg   minimap tile pyramid (z6 = the client's own tiles)
    data.json                 rects and tile extents the prototype pages read

Client art is copyrighted game data: --out must never be inside the repo (it is not
gitignored on purpose). Needs `mpyq` and `Pillow` (tools/ may use them).

    python3 build_art.py --client "/path/to/World of Warcraft 3.3.5a" --out /tmp/spike-art \
        --minimap Kalimdor --zones Durotar,Mulgore,Barrens,Teldrassil
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WOWMAP = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "wowmap"))
sys.path.insert(0, WOWMAP)

from PIL import Image, ImageStat  # noqa: E402

import extract_maps as em  # noqa: E402
from transform import DbcTables  # noqa: E402

DATA_MPQS = ["patch-3.MPQ", "patch-2.MPQ", "patch.MPQ", "lichking.MPQ", "expansion.MPQ",
             "common-2.MPQ", "common.MPQ"]
LOCALE_MPQS = ["patch-{l}-3.MPQ", "patch-{l}-2.MPQ", "patch-{l}.MPQ", "locale-{l}.MPQ"]
CONTINENTS = ["Cosmic", "Azeroth", "Kalimdor", "Expansion01", "Northrend"]
DBCS = ["WorldMapArea", "WorldMapContinent", "WorldMapOverlay", "AreaTable", "Map"]
GRID = 533.3333  # yards per ADT; one minimap tile is one ADT


def open_chain(client, locale):
    from mpyq import MPQArchive
    data = os.path.join(client, "Data")
    paths = [os.path.join(data, locale, n.format(l=locale)) for n in LOCALE_MPQS]
    paths += [os.path.join(data, n) for n in DATA_MPQS]
    return em.ArchiveChain([MPQArchive(p, listfile=False) for p in paths if os.path.exists(p)])


def minimap_pyramid(chain, directory, out, quality=80):
    """Tiles of one md5translate.trs directory -> XYZ pyramid z0..z6 (z6 = native 256 px).

    Tile `map<col>_<row>` is the ADT with col = int(32 - world_y/533.33) (east-west, west
    is smaller) and row = int(32 - world_x/533.33) (north-south, north is smaller), so
    64x64 tiles span the whole map and z0 is the whole map in one 256 px tile.
    """
    trs = chain.read_file("Textures\\Minimap\\md5translate.trs").decode("latin1")
    pat = re.compile(rf"^{re.escape(directory)}\\map(\d+)_(\d+)\.blp\t(\w+\.blp)$")
    entries = [(int(m.group(1)), int(m.group(2)), m.group(3))
               for m in map(pat.match, trs.splitlines()) if m]
    t0 = time.time()
    level = {}
    for col, row, name in entries:
        blob = chain.read_file("Textures\\Minimap\\" + name)
        if blob:
            level[(col, row)] = em.blp_to_image(blob).convert("RGB")
    # Parents are built from 2x2 children; a missing child must be filled with the sea colour
    # (the flattest native tile's mean), not black, or every coast gets a black halo.
    flat = min(list(level.values())[:300], key=lambda im: sum(ImageStat.Stat(im).stddev))
    ocean = tuple(int(v) for v in ImageStat.Stat(flat).mean)
    z = 6
    total = 0
    while True:
        for (x, y), im in level.items():
            d = os.path.join(out, "tiles", directory, str(z), str(x))
            os.makedirs(d, exist_ok=True)
            im.save(os.path.join(d, f"{y}.jpg"), "JPEG", quality=quality)
            total += 1
        if z == 0:
            break
        parents = {}
        for (x, y), im in level.items():
            parents.setdefault((x // 2, y // 2), Image.new("RGB", (512, 512), ocean))\
                .paste(im, ((x % 2) * 256, (y % 2) * 256))
        level = {k: im.resize((256, 256), Image.LANCZOS) for k, im in parents.items()}
        z -= 1
    cols = [c for c, _r, _h in entries]
    rows = [r for _c, r, _h in entries]
    print(f"{directory}: {len(entries)} native tiles, {total} written in "
          f"{time.time() - t0:.0f} s (cols {min(cols)}..{max(cols)}, rows {min(rows)}..{max(rows)})")
    return {"ocean": ocean, "native_tiles": len(entries), "tiles_written": total,
            "cols": [min(cols), max(cols)], "rows": [min(rows), max(rows)]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--locale", default="enUS")
    ap.add_argument("--minimap", default="", help="md5translate directory, e.g. Kalimdor")
    ap.add_argument("--zones", default="", help="comma-separated WorldMapArea names")
    args = ap.parse_args()
    if os.path.realpath(args.out).startswith(os.path.realpath(os.path.join(HERE, "..", ".."))):
        sys.exit("--out is inside the repo: client art must not be committed")

    chain = open_chain(args.client, args.locale)
    for sub in ("dbc", "continents"):
        os.makedirs(os.path.join(args.out, sub), exist_ok=True)
    for name in DBCS:
        blob = chain.read_file(f"DBFilesClient\\{name}.dbc")
        with open(os.path.join(args.out, "dbc", f"{name}.dbc"), "wb") as f:
            f.write(blob)
    for name in CONTINENTS:
        sheet = em.base_sheet(chain, name)
        sheet.save(os.path.join(args.out, "continents", f"{name}.png"))
    if args.zones:
        subprocess.run([sys.executable, os.path.join(WOWMAP, "extract_maps.py"),
                        "--client", args.client, "--dbc", os.path.join(args.out, "dbc"),
                        "--out", os.path.join(args.out, "zones"), "--locale", args.locale,
                        "--only", args.zones], check=True)

    t = DbcTables(os.path.join(args.out, "dbc"))
    # WorldMapArea rows with AreaID 0 are the continents; rects are (field4, field5, field6, field7)
    conts = {}
    for r in t._wm:
        if r[2] == 0 and r[3]:
            conts[t._s(t._wm_str, r[3])] = {"map": r[1], "y1": t._f(r[4]), "y2": t._f(r[5]),
                                           "x1": t._f(r[6]), "x2": t._f(r[7])}
    zones = []
    for r in t._wm:
        if r[2] and r[3]:
            zones.append({"id": r[2], "map": r[1], "name": t._s(t._wm_str, r[3]),
                          "y1": t._f(r[4]), "y2": t._f(r[5]), "x1": t._f(r[6]), "x2": t._f(r[7]),
                          "display_map": t.display_map.get(r[2], -1)})
    data = {"continents": conts, "zones": zones}
    if args.minimap:
        data["minimap"] = {args.minimap: minimap_pyramid(chain, args.minimap, args.out)}
    with open(os.path.join(args.out, "data.json"), "w") as f:
        json.dump(data, f)
    print("done:", args.out)


if __name__ == "__main__":
    main()
