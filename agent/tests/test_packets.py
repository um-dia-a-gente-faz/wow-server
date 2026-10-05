"""Tests for agent.packets: packed GUIDs and server header framing.

Run from the repo root: python3 -m unittest discover -s agent/tests
"""

import unittest

from agent import packets as pk
from agent.tests.builders import tc_server_header


class PackedGuidTest(unittest.TestCase):
    GUIDS = [
        0,
        2,
        0xFF,
        0x0102,
        0x0100,                    # zero low byte is omitted
        0xF130000123000456,        # creature: high-guid + entry + counter
        0x0000000100000000,
        0x8000000000000001,
        0xFFFFFFFFFFFFFFFF,
    ]

    def test_round_trip(self):
        for guid in self.GUIDS:
            with self.subTest(guid=hex(guid)):
                packed = pk.pack_packed_guid(guid)
                self.assertEqual(pk.unpack_packed_guid(packed, 0), (guid, len(packed)))

    def test_little_endian_byte_order(self):
        # Bit i of the mask means byte i of the GUID, least significant first.
        self.assertEqual(pk.pack_packed_guid(0x0102), b'\x03\x02\x01')
        self.assertEqual(pk.unpack_packed_guid(b'\x03\x02\x01', 0), (0x0102, 3))
        self.assertEqual(pk.unpack_packed_guid(b'\x81\x56\xF1', 0), (0xF100000000000056, 3))

    def test_unpack_at_offset(self):
        data = b'\xAA\xBB' + pk.pack_packed_guid(0xF130000123000456) + b'\xCC'
        guid, off = pk.unpack_packed_guid(data, 2)
        self.assertEqual(guid, 0xF130000123000456)
        self.assertEqual(data[off], 0xCC)


class ServerHeaderTest(unittest.TestCase):
    def test_small_header(self):
        hdr = tc_server_header(0x0006, 0x01EE)
        self.assertEqual(hdr, b'\x00\x06\xEE\x01')
        self.assertEqual(pk.parse_server_header(hdr), (0x0006, 0x01EE))

    def test_largest_small_header(self):
        hdr = tc_server_header(0x7FFF, 0x00A9)
        self.assertEqual(len(hdr), 4)
        self.assertEqual(pk.parse_server_header(hdr), (0x7FFF, 0x00A9))

    def test_large_header(self):
        hdr = tc_server_header(0x012345, 0x01F6)
        self.assertEqual(hdr, b'\x81\x23\x45\xF6\x01')
        self.assertEqual(pk.parse_server_header(hdr), (0x012345, 0x01F6))

    def test_large_header_boundaries(self):
        for size in (0x8000, 0x8001, 0xFFFF, 0x10000, 0x7FFFFF):
            for opcode in (0x0000, 0x00A9, 0x01F6, 0xFFFF):
                with self.subTest(size=hex(size), opcode=hex(opcode)):
                    hdr = tc_server_header(size, opcode)
                    self.assertEqual(len(hdr), 5)
                    self.assertEqual(pk.parse_server_header(hdr), (size, opcode))

    def test_wrong_length_rejected(self):
        with self.assertRaises(ValueError):
            pk.parse_server_header(b'\x81\x23\x45\xF6')
        with self.assertRaises(ValueError):
            pk.parse_server_header(b'\x00\x06\xEE\x01\x00')


if __name__ == '__main__':
    unittest.main()
