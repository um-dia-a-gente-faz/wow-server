"""Build the world bundle (ADR 0016, slice 1) from the client DBCs. Run by hand where they are:

    python3 tools/worldknowledge/build.py --dbc-dir /opt/wowmap-data/dbc --out world.sqlite3

Writes a temporary file next to --out and renames it, so a failed build leaves the old bundle alone.
The schema and its version come from agent/worldknowledge.py (one definition).

Field indexes are from TrinityCore branch 3.3.5, src/server/shared/DataStores/DBCStructure.h (the
`// <n>` comments), checked against the format strings in DBCfmt.h (`SpellEntryfmt` 234 chars,
`SpellCastTimefmt` "nixx" = 4, `SpellRangefmt` 40 chars), whose lengths are the field counts below.
A file with another count is from another build and fails the build.

Signedness, from the same header: `SpellEntry` (line 1391) `PowerType` is `uint32` (line 1432), so it
is read unsigned; `SpellCastTimesEntry` (line 1505) `Base` is `int32` (line 1508), so it is read
signed.

    Spell.dbc            234 fields  0 ID, 28 CastingTimeIndex, 39 SpellLevel, 41 PowerType,
                                     42 ManaCost, 46 RangeIndex, 136-151 Name[16],
                                     153-168 NameSubtext[16] (the rank), 204 ManaCostPct
    SpellCastTimes.dbc     4 fields  0 ID, 1 Base (ms, int32)
    SpellRange.dbc        40 fields  0 ID, 1-2 RangeMin[hostile, friendly],
                                     3-4 RangeMax[hostile, friendly]

Index 0 in CastingTimeIndex or RangeIndex means no row: instant cast, no range (0).
"""
import argparse
import hashlib
import os
import sqlite3
import sys
import tempfile

for _p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),        # tools/
           os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")):  # checkout root
    _p = os.path.abspath(_p)
    if _p not in sys.path:
        sys.path.append(_p)
from agent.worldknowledge import SCHEMA, SCHEMA_VERSION  # noqa: E402
from dbc.wdbc import WdbcFile  # noqa: E402

SPELL_FIELDS, CAST_TIMES_FIELDS, RANGE_FIELDS = 234, 4, 40
S_CAST_TIME, S_LEVEL, S_POWER, S_MANA, S_RANGE, S_NAME, S_RANK, S_MANA_PCT = 28, 39, 41, 42, 46, 136, 153, 204
CLIENT_BUILD = "12340"


def _open(dbc_dir, name, fields):
    path = os.path.join(dbc_dir, name)
    f = WdbcFile.open(path, use_mmap=True)
    if f.field_count != fields:
        raise ValueError(f"{path}: {f.field_count} fields, expected {fields} (not a build {CLIENT_BUILD} file)")
    return f


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _spell_rows(dbc_dir):
    spells = _open(dbc_dir, "Spell.dbc", SPELL_FIELDS)
    times = _open(dbc_dir, "SpellCastTimes.dbc", CAST_TIMES_FIELDS)
    ranges = _open(dbc_dir, "SpellRange.dbc", RANGE_FIELDS)
    cast_ms = {times.uint(r, 0): times.int(r, 1) for r in times.records()}
    # index -> (min hostile, max hostile, max friendly)
    range_yd = {ranges.uint(r, 0): (ranges.float(r, 1), ranges.float(r, 3), ranges.float(r, 4))
                for r in ranges.records()}
    slot = spells.detect_locale(S_NAME)   # a client fills only its own locale slot
    for r in spells.records():
        lo, hi, hi_friendly = range_yd.get(spells.uint(r, S_RANGE), (0.0, 0.0, 0.0))
        yield (spells.uint(r, 0), spells.loc_string(r, S_NAME, slot), spells.loc_string(r, S_RANK, slot),
               spells.uint(r, S_LEVEL), spells.uint(r, S_POWER), spells.uint(r, S_MANA),
               spells.uint(r, S_MANA_PCT), cast_ms.get(spells.uint(r, S_CAST_TIME), 0), lo, hi, hi_friendly)


def build(dbc_dir: str, out_path: str) -> dict:
    """Write the bundle to out_path; return the row count per table."""
    out_dir = os.path.dirname(os.path.abspath(out_path))
    fd, tmp = tempfile.mkstemp(prefix=".world-bundle-", suffix=".tmp", dir=out_dir)
    os.close(fd)
    try:
        conn = sqlite3.connect(tmp)
        try:
            conn.executescript(SCHEMA)
            rows = list(_spell_rows(dbc_dir))
            conn.executemany("INSERT INTO spell VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.execute("INSERT INTO tables VALUES ('spell', 'client')")
            conn.executemany("INSERT INTO meta VALUES (?, ?)", [("client_build", CLIENT_BUILD)] + [
                (f"sha256:{n}", _sha256(os.path.join(dbc_dir, n)))
                for n in ("Spell.dbc", "SpellCastTimes.dbc", "SpellRange.dbc")])
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
        os.chmod(tmp, 0o644)   # mkstemp makes it 0600; the bundle is read by other users (containers)
        os.replace(tmp, out_path)
    except BaseException:
        os.unlink(tmp)
        raise
    return {"spell": len(rows)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dbc-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        counts = build(args.dbc_dir, args.out)
    except (OSError, ValueError, sqlite3.Error) as e:   # WdbcError is a ValueError
        print(f"build failed, {args.out} untouched: {e}", file=sys.stderr)
        return 1
    for table, n in counts.items():
        print(f"{table}: {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
