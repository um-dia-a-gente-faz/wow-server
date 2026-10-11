#!/usr/bin/env python3
"""Cursor-based Reader/Writer for packet payloads (#274).

The Reader owns the offset, so a parser reads fields in wire order instead of
threading `off` through every line. A read past the end raises PacketTruncated
(a ProtocolError) naming the opcode, the offset and the bytes wanted, and leaves
the cursor where it was. All multi-byte values are little-endian, the same
results as the packets.py helpers.
"""

import builtins
import struct

from . import packets as pk


class PacketTruncated(pk.ProtocolError):
    """A read ran past the end of a payload."""

    def __init__(self, opcode: int, offset: int, wanted: int | None = None):
        """`wanted` is None when a raw struct/index error was wrapped and the size is unknown."""
        self.opcode = opcode
        self.offset = offset
        self.wanted = wanted
        tail = "" if wanted is None else f", wanted {wanted} bytes"
        super().__init__(f"opcode {opcode:#x}: truncated at offset {offset}{tail}")


class PacketMalformed(pk.ProtocolError, ValueError):
    """The bytes are all there but not a valid layout: leftover bytes, an unknown type.
    Also a ValueError, which the checks it replaces used to raise."""


class Reader:
    __slots__ = ("_data", "_off", "_opcode")

    def __init__(self, data: bytes, opcode: int = 0):
        self._data = data
        self._off = 0
        self._opcode = opcode

    @property
    def offset(self) -> int:
        return self._off

    def remaining(self) -> int:
        return len(self._data) - self._off

    def _need(self, n: int):
        if n < 0:
            raise ValueError(f"negative read of {n} bytes")   # a caller bug, not bad wire data
        if n > self.remaining():
            raise PacketTruncated(self._opcode, self._off, n)

    def _fixed(self, fmt: str):
        size = struct.calcsize(fmt)
        self._need(size)
        value = struct.unpack_from(fmt, self._data, self._off)[0]
        self._off += size
        return value

    def u8(self) -> int:
        return self._fixed('<B')

    def u16(self) -> int:
        return self._fixed('<H')

    def u32(self) -> int:
        return self._fixed('<I')

    def i8(self) -> int:
        return self._fixed('<b')

    def i32(self) -> int:
        return self._fixed('<i')

    def u64(self) -> int:
        return self._fixed('<Q')

    def f32(self) -> float:
        return self._fixed('<f')

    def guid(self) -> int:
        """A raw (unpacked) 8-byte guid."""
        return self.u64()

    def packed_guid(self) -> int:
        """A packed guid: a mask byte, then one byte per set bit (pk.unpack_packed_guid's layout).
        Checks the whole guid before reading, so a short one leaves the cursor unchanged."""
        self._need(1)
        mask = self._data[self._off]
        self._need(1 + bin(mask).count('1'))
        self._off += 1
        guid = 0
        for i in range(8):
            if mask & (1 << i):
                guid |= self._data[self._off] << (i * 8)
                self._off += 1
        return guid

    def cstring(self, encoding: str = 'ascii') -> str:
        """A NUL-terminated string (ascii like pk.cstring unless `encoding` says otherwise; bad
        bytes are replaced). No NUL before the end raises PacketTruncated asking for the 1
        terminator byte."""
        end = self._data.find(b'\x00', self._off)
        if end < 0:
            raise PacketTruncated(self._opcode, self._off, 1)
        text = self._data[self._off:end].decode(encoding, errors='replace')
        self._off = end + 1
        return text

    def done(self):
        """Raise PacketMalformed if bytes are left: a layout mistake then shows up as leftover
        bytes instead of silently shifted fields."""
        if self.remaining():
            raise PacketMalformed(
                f"opcode {self._opcode:#x}: parsed {self._off} of {len(self._data)} bytes "
                f"({self.remaining()} leftover)")

    def skip(self, n: int):
        self._need(n)
        self._off += n

    def bytes(self, n: int) -> bytes:
        self._need(n)
        out = self._data[self._off:self._off + n]
        self._off += n
        return out


class Writer:
    """Builds a payload field by field. Every method returns self; getvalue() returns the bytes."""

    __slots__ = ("_parts",)

    def __init__(self):
        self._parts: list[bytes] = []

    def _add(self, b: bytes) -> "Writer":
        self._parts.append(b)
        return self

    def u8(self, v: int) -> "Writer":
        return self._add(pk.p8(v))

    def u16(self, v: int) -> "Writer":
        return self._add(pk.p16(v))

    def u32(self, v: int) -> "Writer":
        return self._add(pk.p32(v))

    def u64(self, v: int) -> "Writer":
        return self._add(pk.p64(v))

    def f32(self, v: float) -> "Writer":
        return self._add(pk.pf(v))

    def guid(self, v: int) -> "Writer":
        return self._add(pk.p64(v))

    def packed_guid(self, v: int) -> "Writer":
        return self._add(pk.pack_packed_guid(v))

    def cstring(self, s: str) -> "Writer":
        return self._add(s.encode('utf-8') + b'\x00')

    def bytes(self, b: builtins.bytes) -> "Writer":
        return self._add(builtins.bytes(b))

    def getvalue(self) -> builtins.bytes:
        return b''.join(self._parts)
