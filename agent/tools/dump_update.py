#!/usr/bin/env python3
"""Pretty-print a captured SMSG_UPDATE_OBJECT payload.

Usage: python3 -m agent.tools.dump_update <file.bin> [<file.bin> ...]

Files are raw, already-decompressed update-object payloads (uint32
block_count, then blocks) — exactly what agent/tests/fixtures/update_object/
holds and what AGENT_DUMP_PACKETS writes to disk.

Once agent/update_object.py exists (UM-32) this prints a full block-by-block
decode. Until then it only prints what agent/session.py itself understands
today: the block count, each block's type, and OUT_OF_RANGE/NEAR_OBJECTS GUID
lists (self-delimiting) — it stops at the first VALUES/MOVEMENT/CREATE block
per packet, same as the current `_parse_update_object`, rather than guessing
at an offset it can't yet compute correctly.
"""

import struct
import sys

from .. import packets as pk

UPDATETYPE_NAMES = {
    0: "VALUES",
    1: "MOVEMENT",
    2: "CREATE_OBJECT",
    3: "CREATE_OBJECT2",
    4: "OUT_OF_RANGE_OBJECTS",
    5: "NEAR_OBJECTS",
}


def _dump_with_real_parser(data: bytes, parse_update_object) -> None:
    try:
        from .. import update_fields as uf  # UM-33+
    except ImportError:
        uf = None

    blocks = parse_update_object(data)
    print(f"  {len(blocks)} block(s), fully parsed:")
    for i, b in enumerate(blocks):
        print(f"  [{i}] {b}")
        if uf is not None and b.fields is not None:
            print(f"       decoded: {uf.decode_fields(b.object_type, b.fields)}")


def _dump_best_effort(data: bytes) -> None:
    """Matches agent/session.py::_parse_update_object's current capability:
    reads the type of each block, fully resolves OUT_OF_RANGE/NEAR_OBJECTS
    (self-delimiting), and stops at the first VALUES/MOVEMENT/CREATE block
    since its body length isn't parseable yet."""
    off = 4
    block_count = pk.u32(data, 0)
    print(f"  block_count={block_count}")
    for i in range(block_count):
        if off >= len(data):
            print(f"  [{i}] ! truncated: offset {off} >= len {len(data)}")
            return
        update_type = data[off]; off += 1
        name = UPDATETYPE_NAMES.get(update_type, f"UNKNOWN({update_type})")
        if update_type in (4, 5):
            count = pk.u32(data, off); off += 4
            guids = []
            for _ in range(count):
                guid, off = pk.unpack_packed_guid(data, off)
                guids.append(hex(guid))
            print(f"  [{i}] {name}: {count} guid(s): {guids}")
            continue
        if update_type > 5:
            print(f"  [{i}] ! unknown update_type {update_type} at offset {off - 1}")
            return
        guid, off = pk.unpack_packed_guid(data, off)
        print(f"  [{i}] {name}: guid={hex(guid)}  "
              f"(body not parsed by agent/session.py yet — offset of any "
              f"further block in this packet is unknown; stopping here)")
        return


def dump(path: str) -> None:
    with open(path, 'rb') as f:
        data = f.read()
    print(f"{path}: {len(data)} bytes")
    if len(data) < 4:
        print("  ! too short to contain a block_count")
        return
    try:
        from .. import update_object as uo  # UM-32+
    except ImportError:
        _dump_best_effort(data)
    else:
        _dump_with_real_parser(data, uo.parse_update_object)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    for path in argv:
        dump(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
