#!/usr/bin/env python3
"""Extract character body models from the client MPQs for the inspect drawer (#171).

For each race/gender: the M2 and its first skin profile become `<race>_<gender>.bin`
(models.build_blob) and the base skin texture from CharSections.dbc becomes
`<race>_<gender>.png`. Only default-look geosets are kept, so a file is ~100-200 KB
and a PNG ~300 KB; all 20 models are well under 10 MB.

Needs `mpyq` and `Pillow`, same as extract_icons.py (whose archive search it reuses).

Usage:
    python extract_models.py --client /opt/wow-server/client \\
        --dbc /opt/wowmap-data/dbc --out /opt/wowmap-data/models
    python extract_models.py --only 1_0        # Human male, for testing
Existing files are skipped unless --force is given.
"""
import argparse
import os
import sys

import models
from extract_icons import decode_blp, open_archives, read_first


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default="/opt/wow-server/client")
    ap.add_argument("--dbc", default="/opt/wowmap-data/dbc")
    ap.add_argument("--out", default="/opt/wowmap-data/models")
    ap.add_argument("--locale", default="enUS")
    ap.add_argument("--only", default="", help="comma-separated <race>_<gender> keys")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    with open(os.path.join(args.dbc, "CharSections.dbc"), "rb") as f:
        sections = f.read()
    want = {k for k in args.only.split(",") if k}
    archives = open_archives(args.client, args.locale)
    if not archives:
        sys.exit(f"no MPQs found under {os.path.join(args.client, 'Data')}")
    os.makedirs(args.out, exist_ok=True)

    ok = failed = 0
    for race in sorted(models.RACE_FOLDERS):
        for gender in sorted(models.GENDERS):
            key = models.model_key(race, gender)
            if want and key not in want:
                continue
            bin_path, png_path = (os.path.join(args.out, key + e) for e in (".bin", ".png"))
            if not args.force and os.path.exists(bin_path) and os.path.exists(png_path):
                continue
            try:
                m2_path, skin_path = models.mpq_paths(race, gender)
                m2, skin = read_first(archives, m2_path), read_first(archives, skin_path)
                tex_name = models.base_skin_paths(sections, race, gender)
                tex = read_first(archives, tex_name) if tex_name else None
                if not (m2 and skin and tex):
                    raise ValueError(f"missing in MPQs: m2={bool(m2)} skin={bool(skin)} texture={tex_name}")
                blob = models.build_blob(m2, skin)
                decode_blp(tex).save(png_path, "PNG", optimize=True)
                with open(bin_path, "wb") as f:
                    f.write(blob)
                ok += 1
                print(f"{key} {models.RACE_FOLDERS[race]} {models.GENDERS[gender]}: "
                      f"{len(blob)} B mesh, {os.path.getsize(png_path)} B skin")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"{key}: {e}")
    print(f"{ok} written, {failed} failed -> {args.out}")


if __name__ == "__main__":
    main()
