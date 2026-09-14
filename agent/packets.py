#!/usr/bin/env python3
"""Binary packet helpers for WoW 3.3.5a (little-endian wire format)."""

import hashlib
import struct


def sha1(*parts: bytes) -> bytes:
    h = hashlib.sha1()
    for p in parts:
        h.update(p)
    return h.digest()


def u8(data: bytes, off: int) -> int:
    return data[off]


def u16(data: bytes, off: int) -> int:
    return struct.unpack_from('<H', data, off)[0]


def u32(data: bytes, off: int) -> int:
    return struct.unpack_from('<I', data, off)[0]


def u64(data: bytes, off: int) -> int:
    return struct.unpack_from('<Q', data, off)[0]


def f32(data: bytes, off: int) -> float:
    return struct.unpack_from('<f', data, off)[0]


def p8(v: int) -> bytes:
    return struct.pack('<B', v)


def p16(v: int) -> bytes:
    return struct.pack('<H', v)


def p32(v: int) -> bytes:
    return struct.pack('<I', v)


def p64(v: int) -> bytes:
    return struct.pack('<Q', v)


def pf(v: float) -> bytes:
    return struct.pack('<f', v)


def cstring(data: bytes, off: int) -> tuple[str, int]:
    end = data.index(0, off)
    return data[off:end].decode('ascii', errors='replace'), end + 1


def unpack_packed_guid(data: bytes, off: int) -> tuple[int, int]:
    """Read a WoW packed GUID. Returns (guid, new_offset)."""
    mask = data[off]; off += 1
    guid = 0
    for i in range(8):
        if mask & (1 << i):
            guid |= data[off] << (i * 8)
            off += 1
    return guid, off


def pack_packed_guid(guid: int) -> bytes:
    mask = 0
    out = bytearray()
    for i in range(8):
        b = (guid >> (i * 8)) & 0xFF
        if b:
            mask |= (1 << i)
            out.append(b)
    return bytes([mask]) + bytes(out)
