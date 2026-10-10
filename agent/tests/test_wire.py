"""Tests for agent.wire: the cursor-based Reader/Writer over a packet payload (#274).

Run from the repo root: python3 -m unittest discover -t . -s agent/tests
"""

import struct
import unittest

from agent import loot, mail, trade
from agent import packets as pk
from agent.wire import PacketTruncated, Reader, Writer


class PacketTruncatedTest(unittest.TestCase):
    def test_is_a_protocol_error(self):
        self.assertTrue(issubclass(PacketTruncated, pk.ProtocolError))

    def test_message_names_opcode_offset_and_wanted(self):
        err = PacketTruncated(0x23B, 7, 4)
        self.assertEqual((err.opcode, err.offset, err.wanted), (0x23B, 7, 4))
        msg = str(err)
        self.assertIn("0x23b", msg)
        self.assertIn("7", msg)
        self.assertIn("4", msg)


class ReaderTruncationTest(unittest.TestCase):
    """Each fixed-width read raises at the first missing byte, names the read, and leaves the cursor alone."""

    WIDTHS = [
        ("u8", 1), ("u16", 2), ("u32", 4), ("u64", 8), ("f32", 4), ("guid", 8),
    ]

    def test_short_read_raises_and_offset_unchanged(self):
        for method, width in self.WIDTHS:
            with self.subTest(method=method):
                r = Reader(b'\x01' * (width - 1), opcode=0x123)
                with self.assertRaises(PacketTruncated) as cm:
                    getattr(r, method)()
                self.assertEqual(cm.exception.opcode, 0x123)
                self.assertEqual(cm.exception.offset, 0)
                self.assertEqual(cm.exception.wanted, width)
                self.assertEqual(r.offset, 0)
                self.assertEqual(r.remaining(), width - 1)

    def test_truncation_reports_offset_of_the_failed_read(self):
        r = Reader(b'\x01\x02\x03', opcode=0x10)
        self.assertEqual(r.u16(), 0x0201)
        with self.assertRaises(PacketTruncated) as cm:
            r.u32()
        self.assertEqual(cm.exception.offset, 2)
        self.assertEqual(cm.exception.wanted, 4)
        self.assertEqual(r.offset, 2)

    def test_bytes_and_skip_truncate(self):
        r = Reader(b'abc')
        with self.assertRaises(PacketTruncated) as cm:
            r.bytes(4)
        self.assertEqual((cm.exception.offset, cm.exception.wanted), (0, 4))
        with self.assertRaises(PacketTruncated):
            r.skip(4)
        self.assertEqual(r.offset, 0)

    def test_empty_payload_truncates_on_any_read(self):
        with self.assertRaises(PacketTruncated):
            Reader(b'').u8()

    def test_exact_fit_reads_without_error(self):
        r = Reader(struct.pack('<BHIQf', 1, 2, 3, 4, 1.5) + b'xy')
        self.assertEqual((r.u8(), r.u16(), r.u32(), r.u64()), (1, 2, 3, 4))
        self.assertEqual(r.f32(), 1.5)
        self.assertEqual(r.bytes(2), b'xy')
        self.assertEqual(r.remaining(), 0)


class ReaderMatchesPacketHelpersTest(unittest.TestCase):
    def test_fixed_width_reads_match_packets_helpers(self):
        data = bytes(range(1, 40))
        for off in range(0, 20):
            with self.subTest(off=off):
                for method, helper in (("u8", pk.u8), ("u16", pk.u16),
                                       ("u32", pk.u32), ("u64", pk.u64)):
                    r = Reader(data)
                    r.skip(off)
                    self.assertEqual(getattr(r, method)(), helper(data, off))

    def test_signed_read(self):
        r = Reader(struct.pack('<i', -5))
        self.assertEqual(r.i32(), -5)
        self.assertEqual(r.remaining(), 0)


class PackedGuidTest(unittest.TestCase):
    GUIDS = [0, 2, 0xFF, 0x0100, 0xF130000123000456, 0x8000000000000001, 0xFFFFFFFFFFFFFFFF]

    def test_round_trip_through_writer_and_reader(self):
        for guid in self.GUIDS:
            with self.subTest(guid=hex(guid)):
                data = Writer().packed_guid(guid).getvalue()
                self.assertEqual(data, pk.pack_packed_guid(guid))
                r = Reader(data)
                self.assertEqual(r.packed_guid(), guid)
                self.assertEqual(r.remaining(), 0)

    def test_mask_promises_more_bytes_than_present(self):
        # mask 0x07 says three low bytes follow; only one does.
        r = Reader(b'\x07\x01', opcode=0x77)
        with self.assertRaises(PacketTruncated) as cm:
            r.packed_guid()
        self.assertEqual(cm.exception.opcode, 0x77)
        self.assertEqual(r.offset, 0)

    def test_missing_mask_truncates(self):
        with self.assertRaises(PacketTruncated):
            Reader(b'').packed_guid()


class CStringTest(unittest.TestCase):
    def test_reads_string_and_consumes_nul(self):
        r = Reader(b'hello\x00X')
        self.assertEqual(r.cstring(), 'hello')
        self.assertEqual(r.offset, 6)
        self.assertEqual(r.u8(), ord('X'))

    def test_matches_packets_cstring(self):
        data = b'ab\x00cd\x00'
        self.assertEqual(Reader(data).cstring(), pk.cstring(data, 0)[0])

    def test_empty_string(self):
        r = Reader(b'\x00\x01')
        self.assertEqual(r.cstring(), '')
        self.assertEqual(r.offset, 1)

    def test_no_terminator_raises_and_offset_unchanged(self):
        r = Reader(b'abc', opcode=0x99)
        r.skip(1)
        with self.assertRaises(PacketTruncated) as cm:
            r.cstring()
        self.assertEqual(cm.exception.opcode, 0x99)
        self.assertEqual(cm.exception.offset, 1)
        self.assertEqual(cm.exception.wanted, 1)
        self.assertEqual(r.offset, 1)

    def test_empty_payload_has_no_terminator(self):
        with self.assertRaises(PacketTruncated):
            Reader(b'').cstring()

    def test_undecodable_bytes_are_replaced_not_raised(self):
        self.assertEqual(Reader(b'\xff\x00').cstring(), '�')


class WriterRoundTripTest(unittest.TestCase):
    def test_every_width_round_trips(self):
        data = (Writer()
                .u8(0xAB).u16(0xBEEF).u32(0xDEADBEEF).u64(0x0102030405060708)
                .f32(2.5).guid(0xF130000123000456)
                .cstring('name').bytes(b'\x01\x02')
                .getvalue())
        r = Reader(data)
        self.assertEqual(r.u8(), 0xAB)
        self.assertEqual(r.u16(), 0xBEEF)
        self.assertEqual(r.u32(), 0xDEADBEEF)
        self.assertEqual(r.u64(), 0x0102030405060708)
        self.assertEqual(r.f32(), 2.5)
        self.assertEqual(r.guid(), 0xF130000123000456)
        self.assertEqual(r.cstring(), 'name')
        self.assertEqual(r.bytes(2), b'\x01\x02')
        self.assertEqual(r.remaining(), 0)

    def test_writer_methods_return_self_and_match_packets_helpers(self):
        w = Writer()
        self.assertIs(w.u8(1), w)
        self.assertEqual(Writer().u32(7).getvalue(), pk.p32(7))
        self.assertEqual(Writer().f32(1.0).getvalue(), pk.pf(1.0))
        self.assertEqual(Writer().guid(3).getvalue(), pk.p64(3))

    def test_cstring_is_nul_terminated(self):
        self.assertEqual(Writer().cstring('').getvalue(), b'\x00')
        self.assertEqual(Writer().cstring('ab').getvalue(), b'ab\x00')


class MigratedParsersTest(unittest.TestCase):
    """A short payload in a migrated parser raises PacketTruncated, not struct.error/IndexError."""

    CASES = [
        ("mail.parse_send_mail_result", lambda p: mail.parse_send_mail_result(p), b'\x01\x00\x00\x00\x02\x00'),
        ("mail.parse_mail_list_result", lambda p: mail.parse_mail_list_result(p), b'\x00\x00\x00\x00\x01'),
        ("mail.parse_received_mail", lambda p: mail.parse_received_mail(p), b'\x00\x00'),
        ("trade.parse_trade_status", lambda p: trade.parse_trade_status(p), b'\x01\x00'),
        ("trade.parse_trade_status_extended", lambda p: trade.parse_trade_status_extended(p), b'\x01'),
        ("loot.parse_loot_response", lambda p: loot.parse_loot_response(p), struct.pack('<Q', 1) + b'\x01\x00'),
        ("loot.parse_loot_release_response", lambda p: loot.parse_loot_release_response(p), b'\x00' * 4),
        ("loot.parse_loot_money_notify", lambda p: loot.parse_loot_money_notify(p), b'\x00\x00'),
        ("loot.parse_item_push_result", lambda p: loot.parse_item_push_result(p), b'\x00' * 8),
        ("loot.parse_item_query_response", lambda p: loot.parse_item_query_response(p), struct.pack('<I', 5)),
    ]

    def test_short_payload_raises_packet_truncated(self):
        for name, parse, payload in self.CASES:
            with self.subTest(parser=name):
                with self.assertRaises(PacketTruncated):
                    parse(payload)

    def test_truncation_names_the_failed_read(self):
        with self.assertRaises(PacketTruncated) as cm:
            mail.parse_received_mail(b'\x00\x00')
        self.assertEqual((cm.exception.offset, cm.exception.wanted), (0, 4))


if __name__ == '__main__':
    unittest.main()
