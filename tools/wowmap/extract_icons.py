#!/usr/bin/env python3
"""Extract item icons from the client MPQs into PNGs for the inspect drawer.

Every icon named by ItemDisplayInfo.dbc's InventoryIcon field (the table
`world.item_template.displayid` points into, see item_icons.py) is read from
`Interface\\Icons\\<name>.blp`, decoded and written as `<out>/<icon_file(name)>`.
That is every icon an item can show, about 4.7k files and a few tens of MB.

Icons live in the base and patch MPQs (unlike the world map art, which is in the
locale MPQs), so the archives are searched in the client's own priority order:
newest patch first, locale before base. The first archive holding the file wins.

Decoding: Pillow's BLP plugin reads BLP1 and BLP2 (palettised and DXT1/3/5).
If it refuses a file, the DDS-wrapper decoder from extract_maps.py is tried.

Needs `mpyq` and `Pillow` (`pip install mpyq Pillow`), same as extract_maps.py.

Usage:
    python extract_icons.py --client /opt/wow-server/client \\
        --dbc /opt/wowmap-data/dbc --out /opt/wowmap-data/icons
    python extract_icons.py --only INV_Sword_04,INV_Misc_QuestionMark   # a few, for testing
Existing PNGs are skipped unless --force is given, so re-runs are cheap.
"""
import argparse
import io
import os
import sys

from item_icons import icon_file, load_display_icons, mpq_path

# Highest priority first. 3.3.5a's patches contain whole files (no incremental
# patching), so "first archive that has it" is what the client does too.
ARCHIVE_ORDER = (
    "{locale}/patch-{locale}-3.MPQ", "{locale}/patch-{locale}-2.MPQ", "{locale}/patch-{locale}.MPQ",
    "patch-3.MPQ", "patch-2.MPQ", "patch.MPQ",
    "{locale}/lichking-locale-{locale}.MPQ", "{locale}/expansion-locale-{locale}.MPQ",
    "{locale}/locale-{locale}.MPQ", "{locale}/base-{locale}.MPQ",
    "lichking.MPQ", "expansion.MPQ", "common-2.MPQ", "common.MPQ",
)


def decode_blp(blob):
    """BLP bytes -> RGBA PIL image."""
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(blob))
        img.load()
        return img.convert("RGBA")
    except Exception as first:  # noqa: BLE001
        try:
            from extract_maps import blp_to_image
            return blp_to_image(blob)
        except Exception:  # noqa: BLE001
            raise first from None


def open_archives(client, locale):
    from mpyq import MPQArchive
    archives = []
    for rel in ARCHIVE_ORDER:
        p = os.path.join(client, "Data", rel.format(locale=locale))
        if os.path.exists(p):
            archives.append((os.path.basename(p), MPQArchive(p, listfile=False)))
    return archives


def read_first(archives, path):
    for _name, archive in archives:
        try:
            blob = archive.read_file(path)
        except Exception:  # noqa: BLE001
            blob = None
        if blob:
            return blob
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default="/opt/wow-server/client")
    ap.add_argument("--dbc", default="/opt/wowmap-data/dbc")
    ap.add_argument("--out", default="/opt/wowmap-data/icons")
    ap.add_argument("--locale", default="enUS")
    ap.add_argument("--only", default="", help="comma-separated icon names")
    ap.add_argument("--force", action="store_true", help="overwrite existing PNGs")
    args = ap.parse_args()

    names = {}
    for name in load_display_icons(os.path.join(args.dbc, "ItemDisplayInfo.dbc")).values():
        names.setdefault(icon_file(name), name)
    if args.only:
        want = {icon_file(w) for w in args.only.split(",") if w.strip()}
        names = {f: n for f, n in names.items() if f in want}
        for f in want - names.keys():   # allow icons that no item uses (e.g. a placeholder)
            names[f] = f[:-4]

    archives = open_archives(args.client, args.locale)
    if not archives:
        sys.exit(f"no MPQs found under {os.path.join(args.client, 'Data')}")
    os.makedirs(args.out, exist_ok=True)
    print(f"{len(names)} icons; {len(archives)} MPQs: {', '.join(a for a, _ in archives)}")

    done = skipped = missing = failed = 0
    for fn, name in sorted(names.items()):
        out = os.path.join(args.out, fn)
        if os.path.exists(out) and not args.force:
            skipped += 1
            continue
        blob = read_first(archives, mpq_path(name))
        if not blob:
            missing += 1
            print(f"  missing {mpq_path(name)}")
            continue
        try:
            decode_blp(blob).save(out, "PNG", optimize=True)
            done += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  {name}: {e}")

    print(f"\n{done} written, {skipped} already there, {missing} not in the MPQs, "
          f"{failed} failed to decode -> {args.out}")


if __name__ == "__main__":
    main()
