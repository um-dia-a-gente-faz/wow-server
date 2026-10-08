#!/usr/bin/env python3
"""Binary packet helpers for WoW 3.3.5a (little-endian wire format)."""

import hashlib
import struct


class ProtocolError(Exception):
    """A server packet is malformed (truncated, bad count, undecodable text).
    The one family a handler may raise on bad wire data; the router converts
    the raw parse errors into it (#364)."""


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


def parse_server_header(hdr: bytes) -> tuple[int, int]:
    """Decode a decrypted server->client world packet header.

    Returns (size, opcode); size counts the 2 opcode bytes plus the payload.
    Layout per TrinityCore 3.3.5 ServerPktHeader:
      normal (4 B): [size_hi][size_lo][opcode_lo][opcode_hi]
      large  (5 B, size > 0x7FFF): [0x80|size_hi][size_mid][size_lo][opcode_lo][opcode_hi]
    """
    if hdr[0] & 0x80:
        if len(hdr) != 5:
            raise ValueError(f"large server header must be 5 bytes, got {len(hdr)}")
        size = ((hdr[0] & 0x7F) << 16) | (hdr[1] << 8) | hdr[2]
        return size, hdr[3] | (hdr[4] << 8)
    if len(hdr) != 4:
        raise ValueError(f"server header must be 4 bytes, got {len(hdr)}")
    return (hdr[0] << 8) | hdr[1], hdr[2] | (hdr[3] << 8)


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
