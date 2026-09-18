"""Unit tests for agent.trade: golden-byte request builders and hand-built
response parsers (layouts cited in agent/trade.py, verified against
TrinityCore 3.3.5 TradeHandler.cpp/TradeData.cpp/SharedDefines.h)."""

import pathlib
import struct
import unittest

from agent import trade

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "trade"


class BuildRequestTest(unittest.TestCase):
    """Golden-byte tests: each builder's output must match the exact wire
    layout, byte for byte."""

    def test_initiate_trade(self):
        self.assertEqual(trade.build_initiate_trade(0x1234), struct.pack('<Q', 0x1234))

    def test_begin_trade_is_empty(self):
        self.assertEqual(trade.build_begin_trade(), b'')

    def test_busy_trade_is_empty(self):
        self.assertEqual(trade.build_busy_trade(), b'')

    def test_ignore_trade_is_empty(self):
        self.assertEqual(trade.build_ignore_trade(), b'')

    def test_accept_trade_is_empty(self):
        self.assertEqual(trade.build_accept_trade(), b'')

    def test_unaccept_trade_is_empty(self):
        self.assertEqual(trade.build_unaccept_trade(), b'')

    def test_cancel_trade_is_empty(self):
        self.assertEqual(trade.build_cancel_trade(), b'')

    def test_set_trade_item(self):
        self.assertEqual(trade.build_set_trade_item(2, 255, 23), struct.pack('<BBB', 2, 255, 23))

    def test_clear_trade_item(self):
        self.assertEqual(trade.build_clear_trade_item(3), struct.pack('<B', 3))

    def test_set_trade_gold(self):
        self.assertEqual(trade.build_set_trade_gold(12345), struct.pack('<I', 12345))


class ParseTradeStatusTest(unittest.TestCase):
    def test_begin_trade_carries_trader_guid(self):
        payload = struct.pack('<I', trade.TRADE_STATUS_BEGIN_TRADE) + struct.pack('<Q', 0x1122334455667788)
        info = trade.parse_trade_status(payload)
        self.assertEqual(info["status"], 1)
        self.assertEqual(info["status_name"], "begin_trade")
        self.assertEqual(info["trader_guid"], 0x1122334455667788)

    def test_open_window_has_no_extra_fields(self):
        payload = struct.pack('<II', trade.TRADE_STATUS_OPEN_WINDOW, 0)
        info = trade.parse_trade_status(payload)
        self.assertEqual(info, {"status": 2, "status_name": "open_window"})

    def test_close_window_carries_result_and_category(self):
        payload = struct.pack('<IIBI', trade.TRADE_STATUS_CLOSE_WINDOW, 29, 1, 7)
        info = trade.parse_trade_status(payload)
        self.assertEqual(info["result"], 29)
        self.assertEqual(info["result_name"], "not_enough_money")
        self.assertTrue(info["is_target_result"])
        self.assertEqual(info["item_limit_category_id"], 7)

    def test_close_window_unknown_result_falls_back_to_numeric_name(self):
        payload = struct.pack('<IIBI', trade.TRADE_STATUS_CLOSE_WINDOW, 999, 0, 0)
        info = trade.parse_trade_status(payload)
        self.assertEqual(info["result_name"], "result_999")

    def test_not_on_taplist_carries_slot(self):
        payload = struct.pack('<I', trade.TRADE_STATUS_NOT_ON_TAPLIST) + bytes([4])
        info = trade.parse_trade_status(payload)
        self.assertEqual(info["slot"], 4)

    def test_wrong_realm_carries_slot(self):
        payload = struct.pack('<I', trade.TRADE_STATUS_WRONG_REALM) + bytes([1])
        info = trade.parse_trade_status(payload)
        self.assertEqual(info["slot"], 1)

    def test_plain_status_has_no_tail(self):
        for status, name in ((0, "busy"), (3, "trade_canceled"), (4, "trade_accept"),
                              (7, "back_to_trade"), (8, "trade_complete")):
            with self.subTest(status=status):
                payload = struct.pack('<I', status)
                info = trade.parse_trade_status(payload)
                self.assertEqual(info, {"status": status, "status_name": name})

    def test_unknown_status_name_falls_back_to_numeric(self):
        payload = struct.pack('<I', 13)  # reserved/unused in SharedDefines.h
        info = trade.parse_trade_status(payload)
        self.assertEqual(info["status_name"], "status_13")


def _empty_trade_slot(i: int) -> bytes:
    return struct.pack('<B', i) + b'\x00' * (4 * 18)


def _item_trade_slot(i, entry, display_id=100, count=1, wrapped=0, gift_creator=0,
                      enchant_id=0, socket_enchants=(0, 0, 0), creator=0, charges=0,
                      suffix_factor=0, random_property_id=0, lock_id=0,
                      max_durability=0, durability=0) -> bytes:
    return (struct.pack('<B', i)
            + struct.pack('<IIII', entry, display_id, count, wrapped)
            + struct.pack('<Q', gift_creator)
            + struct.pack('<I', enchant_id)
            + struct.pack('<3I', *socket_enchants)
            + struct.pack('<Q', creator)
            + struct.pack('<IIIIII', charges, suffix_factor, random_property_id,
                           lock_id, max_durability, durability))


class ParseTradeStatusExtendedTest(unittest.TestCase):
    def test_empty_offer_all_slots_empty(self):
        payload = struct.pack('<BIIII', 1, 0, trade.TRADE_SLOT_COUNT, trade.TRADE_SLOT_COUNT, 0) \
            + struct.pack('<I', 0)
        payload += b''.join(_empty_trade_slot(i) for i in range(trade.TRADE_SLOT_COUNT))
        info = trade.parse_trade_status_extended(payload)
        self.assertTrue(info["is_trader_data"])
        self.assertEqual(info["money"], 0)
        self.assertEqual(info["items"], {})
        self.assertEqual(len(payload), 1 + 4 * 5 + trade.TRADE_SLOT_COUNT * (1 + 4 * 18))

    def test_one_item_offered(self):
        payload = struct.pack('<BIIII', 1, 0, trade.TRADE_SLOT_COUNT, trade.TRADE_SLOT_COUNT, 500) \
            + struct.pack('<I', 0)
        for i in range(trade.TRADE_SLOT_COUNT):
            if i == 3:
                payload += _item_trade_slot(3, entry=6948, count=2, durability=80, max_durability=100)
            else:
                payload += _empty_trade_slot(i)
        info = trade.parse_trade_status_extended(payload)
        self.assertEqual(info["money"], 500)
        self.assertEqual(set(info["items"].keys()), {3})
        item = info["items"][3]
        self.assertEqual(item["entry"], 6948)
        self.assertEqual(item["count"], 2)
        self.assertEqual(item["durability"], 80)
        self.assertEqual(item["max_durability"], 100)

    def test_is_trader_data_false_when_flag_clear(self):
        payload = struct.pack('<BIIII', 0, 0, trade.TRADE_SLOT_COUNT, trade.TRADE_SLOT_COUNT, 0) \
            + struct.pack('<I', 0)
        payload += b''.join(_empty_trade_slot(i) for i in range(trade.TRADE_SLOT_COUNT))
        info = trade.parse_trade_status_extended(payload)
        self.assertFalse(info["is_trader_data"])


class RealFixtureIntegrationTest(unittest.TestCase):
    """Feeds real captured payloads (agent/tests/fixtures/trade/README.md
    has the provenance) through the actual parsers — a full live trade
    between Farstrider and Shadowblade on 192.168.1.64, 2026-09-17."""

    def _load(self, name: str) -> bytes:
        return (FIXTURES_DIR / name).read_bytes()

    def test_begin_trade(self):
        info = trade.parse_trade_status(self._load("trade_status_begin_trade.bin"))
        self.assertEqual(info["status_name"], "begin_trade")
        self.assertEqual(info["trader_guid"], 3)

    def test_open_window(self):
        info = trade.parse_trade_status(self._load("trade_status_open_window.bin"))
        self.assertEqual(info["status_name"], "open_window")

    def test_back_to_trade(self):
        info = trade.parse_trade_status(self._load("trade_status_back_to_trade.bin"))
        self.assertEqual(info["status_name"], "back_to_trade")

    def test_trade_accept(self):
        info = trade.parse_trade_status(self._load("trade_status_trade_accept.bin"))
        self.assertEqual(info["status_name"], "trade_accept")

    def test_trade_canceled(self):
        info = trade.parse_trade_status(self._load("trade_status_trade_canceled.bin"))
        self.assertEqual(info["status_name"], "trade_canceled")

    def test_trade_complete(self):
        info = trade.parse_trade_status(self._load("trade_status_trade_complete.bin"))
        self.assertEqual(info["status_name"], "trade_complete")

    def test_extended_with_item(self):
        payload = self._load("trade_status_extended_with_item.bin")
        info = trade.parse_trade_status_extended(payload)
        self.assertEqual(len(payload), 532)
        self.assertTrue(info["is_trader_data"])
        self.assertEqual(info["money"], 0)
        self.assertEqual(set(info["items"].keys()), {0})
        item = info["items"][0]
        self.assertEqual(item["entry"], 20812)
        self.assertEqual(item["display_id"], 33222)
        self.assertEqual(item["count"], 1)

    def test_extended_empty(self):
        payload = self._load("trade_status_extended_empty.bin")
        info = trade.parse_trade_status_extended(payload)
        self.assertEqual(len(payload), 532)
        self.assertTrue(info["is_trader_data"])
        self.assertEqual(info["money"], 0)
        self.assertEqual(info["items"], {})


if __name__ == "__main__":
    unittest.main()
