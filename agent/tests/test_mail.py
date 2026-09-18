"""Unit tests for agent.mail: golden-byte request builders and hand-built
response parsers (layouts cited in agent/mail.py, verified against
TrinityCore 3.3.5 MailHandler.cpp/MailPackets.h/.cpp/Mail.h/SharedDefines.h)."""

import pathlib
import struct
import unittest

from agent import mail

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "mail"


def cstring(s: str) -> bytes:
    return s.encode('utf-8') + b'\x00'


class BuildRequestTest(unittest.TestCase):
    def test_get_mail_list(self):
        self.assertEqual(mail.build_get_mail_list(0x1234), struct.pack('<Q', 0x1234))

    def test_send_mail_no_item(self):
        payload = mail.build_send_mail(0x1234, "Rubens", "Hi", "Body text", money=500, cod=0)
        expected = (struct.pack('<Q', 0x1234) + cstring("Rubens") + cstring("Hi") + cstring("Body text")
                    + struct.pack('<ii', 41, 0) + struct.pack('<B', 0)
                    + struct.pack('<ii', 500, 0) + struct.pack('<QB', 0, 0))
        self.assertEqual(payload, expected)

    def test_send_mail_with_item(self):
        payload = mail.build_send_mail(0x1234, "Rubens", "Hi", "Body", money=0, item_guid=0xABCD)
        expected = (struct.pack('<Q', 0x1234) + cstring("Rubens") + cstring("Hi") + cstring("Body")
                    + struct.pack('<ii', 41, 0) + struct.pack('<B', 1) + struct.pack('<BQ', 0, 0xABCD)
                    + struct.pack('<ii', 0, 0) + struct.pack('<QB', 0, 0))
        self.assertEqual(payload, expected)

    def test_mail_take_money(self):
        self.assertEqual(mail.build_mail_take_money(0x1234, 7), struct.pack('<Qi', 0x1234, 7))

    def test_mail_take_item(self):
        self.assertEqual(mail.build_mail_take_item(0x1234, 7, 22), struct.pack('<Qii', 0x1234, 7, 22))

    def test_mail_mark_as_read(self):
        self.assertEqual(mail.build_mail_mark_as_read(0x1234, 7), struct.pack('<Qi', 0x1234, 7))

    def test_mail_delete_default_reason(self):
        self.assertEqual(mail.build_mail_delete(0x1234, 7), struct.pack('<Qii', 0x1234, 7, 0))


class ParseSendMailResultTest(unittest.TestCase):
    def test_plain_ok(self):
        payload = struct.pack('<III', 5, mail.MAIL_SEND, mail.MAIL_OK)
        info = mail.parse_send_mail_result(payload)
        self.assertEqual(info, {"mail_id": 5, "command": 0, "command_name": "send",
                                 "error_code": 0, "error_name": "ok"})

    def test_recipient_not_found(self):
        payload = struct.pack('<III', 0, mail.MAIL_SEND, 4)
        info = mail.parse_send_mail_result(payload)
        self.assertEqual(info["error_name"], "recipient_not_found")

    def test_equip_error_carries_bag_result(self):
        payload = struct.pack('<IIII', 0, mail.MAIL_SEND, 1, 4)
        info = mail.parse_send_mail_result(payload)
        self.assertEqual(info["error_name"], "equip_error")
        self.assertEqual(info["bag_result"], 4)

    def test_item_taken_ok_carries_attach_and_qty(self):
        payload = struct.pack('<IIIII', 5, mail.MAIL_ITEM_TAKEN, mail.MAIL_OK, 99, 3)
        info = mail.parse_send_mail_result(payload)
        self.assertEqual(info["attach_id"], 99)
        self.assertEqual(info["qty_in_inventory"], 3)

    def test_item_taken_failure_has_no_trailing_fields(self):
        payload = struct.pack('<III', 5, mail.MAIL_ITEM_TAKEN, 6)  # internal_error
        info = mail.parse_send_mail_result(payload)
        self.assertNotIn("attach_id", info)

    def test_money_taken_ok_has_no_trailing_fields(self):
        payload = struct.pack('<III', 5, mail.MAIL_MONEY_TAKEN, mail.MAIL_OK)
        info = mail.parse_send_mail_result(payload)
        self.assertNotIn("attach_id", info)


def attachment_bytes(position=0, attach_id=1, entry=6948, count=1, durability=100,
                      max_durability=100, unlocked=True) -> bytes:
    b = struct.pack('<Bii', position, attach_id, entry)
    for _ in range(mail.MAX_INSPECTED_ENCHANTMENT_SLOT):
        b += struct.pack('<iii', 0, 0, 0)
    b += struct.pack('<ii', 0, 0)  # random property id, seed
    b += struct.pack('<i', count)
    b += struct.pack('<i', 0)  # charges
    b += struct.pack('<I', max_durability)
    b += struct.pack('<i', durability)
    b += struct.pack('<B', 1 if unlocked else 0)
    return b


def mail_entry_bytes(mail_id=1, sender_guid=3, subject="Subj", body="Body", money=0,
                      cod=0, flags=0, attachments: list = ()) -> bytes:
    e = struct.pack('<i', mail_id) + struct.pack('<B', mail.MAIL_NORMAL) + struct.pack('<Q', sender_guid)
    e += struct.pack('<I', cod)
    e += struct.pack('<i', 0)   # package_id
    e += struct.pack('<i', 41)  # stationery
    e += struct.pack('<I', money)
    e += struct.pack('<i', flags)
    e += struct.pack('<f', 29.0)
    e += struct.pack('<i', 0)  # mail_template_id
    e += cstring(subject) + cstring(body)
    e += struct.pack('<B', len(attachments)) + b''.join(attachments)
    return struct.pack('<H', len(e)) + e


class ParseMailListResultTest(unittest.TestCase):
    def test_no_mail(self):
        payload = struct.pack('<iB', 0, 0)
        info = mail.parse_mail_list_result(payload)
        self.assertEqual(info, {"total_records": 0, "mails": []})

    def test_one_mail_with_money_and_item(self):
        entry = mail_entry_bytes(mail_id=1, sender_guid=0x3, subject="Gift", body="Enjoy",
                                  money=1234, attachments=[attachment_bytes(attach_id=22, entry=20812)])
        payload = struct.pack('<i', 1) + struct.pack('<B', 1) + entry
        info = mail.parse_mail_list_result(payload)
        self.assertEqual(info["total_records"], 1)
        m = info["mails"][0]
        self.assertEqual(m["mail_id"], 1)
        self.assertEqual(m["sender_guid"], 0x3)
        self.assertIsNone(m["alt_sender_id"])
        self.assertEqual(m["subject"], "Gift")
        self.assertEqual(m["money"], 1234)
        self.assertEqual(len(m["attachments"]), 1)
        self.assertEqual(m["attachments"][0]["attach_id"], 22)
        self.assertEqual(m["attachments"][0]["entry"], 20812)

    def test_non_player_sender_uses_alt_sender_id(self):
        entry = mail_entry_bytes(mail_id=2, sender_guid=0, subject="Auction", body="")
        # Rebuild with MAIL_AUCTION sender type and an alt_sender_id instead.
        e = struct.pack('<i', 2) + struct.pack('<B', mail.MAIL_AUCTION) + struct.pack('<i', 555)
        e += struct.pack('<I', 0) + struct.pack('<i', 0) + struct.pack('<i', 62)
        e += struct.pack('<I', 0) + struct.pack('<i', 0) + struct.pack('<f', 10.0) + struct.pack('<i', 0)
        e += cstring("Auction") + cstring("") + struct.pack('<B', 0)
        entry = struct.pack('<H', len(e)) + e
        payload = struct.pack('<i', 1) + struct.pack('<B', 1) + entry
        info = mail.parse_mail_list_result(payload)
        m = info["mails"][0]
        self.assertIsNone(m["sender_guid"])
        self.assertEqual(m["alt_sender_id"], 555)

    def test_is_read_flag(self):
        entry = mail_entry_bytes(mail_id=3, flags=mail.MAIL_CHECK_MASK_READ)
        payload = struct.pack('<i', 1) + struct.pack('<B', 1) + entry
        info = mail.parse_mail_list_result(payload)
        self.assertTrue(info["mails"][0]["is_read"])

    def test_two_mails_consumed_exactly(self):
        e1 = mail_entry_bytes(mail_id=1, subject="A")
        e2 = mail_entry_bytes(mail_id=2, subject="B")
        payload = struct.pack('<i', 2) + struct.pack('<B', 2) + e1 + e2
        info = mail.parse_mail_list_result(payload)
        self.assertEqual([m["subject"] for m in info["mails"]], ["A", "B"])


class ParseReceivedMailTest(unittest.TestCase):
    def test_delay(self):
        payload = struct.pack('<f', 0.0)
        self.assertEqual(mail.parse_received_mail(payload), {"delay": 0.0})


class RealFixtureIntegrationTest(unittest.TestCase):
    """Feeds real captured payloads (agent/tests/fixtures/mail/README.md has
    the provenance) through the actual parsers — a live session against
    192.168.1.64 (Luaprata, account AGENT01), 2026-09-17."""

    def _load(self, name: str) -> bytes:
        return (FIXTURES_DIR / name).read_bytes()

    def test_send_mail_result_not_enough_money(self):
        info = mail.parse_send_mail_result(self._load("send_mail_result_not_enough_money.bin"))
        self.assertEqual(info["command_name"], "send")
        self.assertEqual(info["error_name"], "not_enough_money")

    def test_mail_list_result_empty(self):
        info = mail.parse_mail_list_result(self._load("mail_list_result_empty.bin"))
        self.assertEqual(info, {"total_records": 0, "mails": []})


if __name__ == "__main__":
    unittest.main()
