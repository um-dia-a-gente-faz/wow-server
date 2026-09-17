"""Unit tests for agent.npc: golden-byte request builders and fixture-based
response parsers (hand-built bytes matching the layouts cited in
agent/npc.py, verified against TrinityCore 3.3.5 source)."""

import struct
import unittest

from agent import npc
from agent import packets as pk


def cstring(s: str) -> bytes:
    return s.encode('utf-8') + b'\x00'


class BuildRequestTest(unittest.TestCase):
    """Golden-byte tests: each builder's output must match the exact wire
    layout, byte for byte, not just round-trip through our own parser."""

    def test_gossip_hello(self):
        self.assertEqual(npc.build_gossip_hello(0x1234), struct.pack('<Q', 0x1234))

    def test_list_inventory(self):
        self.assertEqual(npc.build_list_inventory(0xABCD), struct.pack('<Q', 0xABCD))

    def test_trainer_list(self):
        self.assertEqual(npc.build_trainer_list(0xABCD), struct.pack('<Q', 0xABCD))

    def test_gameobj_use(self):
        self.assertEqual(npc.build_gameobj_use(0xF00D), struct.pack('<Q', 0xF00D))

    def test_npc_text_query(self):
        self.assertEqual(npc.build_npc_text_query(1234, 0xABCD), struct.pack('<IQ', 1234, 0xABCD))

    def test_gossip_select_option_without_code(self):
        payload = npc.build_gossip_select_option(0x1234, 7, 2)
        self.assertEqual(payload, struct.pack('<QII', 0x1234, 7, 2))

    def test_gossip_select_option_with_code(self):
        payload = npc.build_gossip_select_option(0x1234, 7, 2, code="letmein")
        self.assertEqual(payload, struct.pack('<QII', 0x1234, 7, 2) + cstring("letmein"))

    def test_buy_item(self):
        payload = npc.build_buy_item(0x1234, 6948, 3, 5)
        self.assertEqual(payload, struct.pack('<QIIIB', 0x1234, 6948, 3, 5, 1))

    def test_sell_item(self):
        payload = npc.build_sell_item(0x1234, 0x5678, 2)
        self.assertEqual(payload, struct.pack('<QQI', 0x1234, 0x5678, 2))

    def test_train_spell(self):
        payload = npc.build_train_spell(0x1234, 587)
        self.assertEqual(payload, struct.pack('<Qi', 0x1234, 587))


def gossip_option_bytes(index=0, icon=0, coded=False, box_money=0, text="Hello",
                        box_text="") -> bytes:
    return (struct.pack('<i', index) + bytes([icon]) + struct.pack('<b', 1 if coded else 0)
            + struct.pack('<I', box_money) + cstring(text) + cstring(box_text))


def gossip_quest_bytes(quest_id=1, quest_type=0, level=5, flags=0, repeatable=False,
                       title="A Quest") -> bytes:
    return (struct.pack('<i', quest_id) + struct.pack('<i', quest_type) + struct.pack('<i', level)
            + struct.pack('<i', flags) + bytes([1 if repeatable else 0]) + cstring(title))


class ParseGossipMessageTest(unittest.TestCase):
    def test_options_and_quests(self):
        payload = (
            struct.pack('<Q', 0xF130000000001234)      # npc guid
            + struct.pack('<i', 42)                     # menu id
            + struct.pack('<i', 999)                    # text id
            + struct.pack('<I', 2)                       # option count
            + gossip_option_bytes(index=0, icon=0, text="Train me")
            + gossip_option_bytes(index=1, icon=3, coded=True, box_money=100,
                                   text="Buy passage", box_text="Enter password")
            + struct.pack('<I', 1)                        # quest count
            + gossip_quest_bytes(quest_id=100, quest_type=1, level=10, flags=0,
                                  repeatable=True, title="Kill Ten Rats")
        )
        info = npc.parse_gossip_message(payload)
        self.assertEqual(info["npc_guid"], 0xF130000000001234)
        self.assertEqual(info["menu_id"], 42)
        self.assertEqual(info["text_id"], 999)
        self.assertEqual(len(info["options"]), 2)
        self.assertEqual(info["options"][0], {
            "index": 0, "icon": 0, "coded": False, "box_money": 0,
            "text": "Train me", "box_text": "",
        })
        self.assertEqual(info["options"][1], {
            "index": 1, "icon": 3, "coded": True, "box_money": 100,
            "text": "Buy passage", "box_text": "Enter password",
        })
        self.assertEqual(len(info["quests"]), 1)
        self.assertEqual(info["quests"][0], {
            "quest_id": 100, "quest_type": 1, "level": 10, "flags": 0,
            "repeatable": True, "title": "Kill Ten Rats",
        })

    def test_no_options_or_quests(self):
        payload = struct.pack('<Q', 1) + struct.pack('<i', 1) + struct.pack('<i', 1) \
            + struct.pack('<I', 0) + struct.pack('<I', 0)
        info = npc.parse_gossip_message(payload)
        self.assertEqual(info["options"], [])
        self.assertEqual(info["quests"], [])


def vendor_item_bytes(slot=1, entry=6948, display_id=100, quantity=-1, price=500,
                       durability=0, stack_count=1, extended_cost=0) -> bytes:
    return struct.pack('<iIIiiiii', slot, entry, display_id, quantity, price,
                        durability, stack_count, extended_cost)


class ParseListInventoryTest(unittest.TestCase):
    def test_items(self):
        payload = (struct.pack('<Q', 0xF130000000005678) + bytes([2])
                   + vendor_item_bytes(slot=1, entry=6948, price=500)
                   + vendor_item_bytes(slot=2, entry=159, price=10, quantity=20))
        info = npc.parse_list_inventory(payload)
        self.assertEqual(info["vendor_guid"], 0xF130000000005678)
        self.assertEqual(len(info["items"]), 2)
        self.assertEqual(info["items"][0], {
            "slot": 1, "entry": 6948, "display_id": 100, "quantity": -1,
            "price": 500, "durability": 0, "stack_count": 1, "extended_cost": 0,
        })
        self.assertEqual(info["items"][1]["entry"], 159)
        self.assertEqual(info["items"][1]["quantity"], 20)
        self.assertIsNone(info["reason"])

    def test_empty_vendor_has_reason(self):
        payload = struct.pack('<Q', 1) + bytes([0]) + struct.pack('<b', 3)
        info = npc.parse_list_inventory(payload)
        self.assertEqual(info["items"], [])
        self.assertEqual(info["reason"], 3)


def trainer_spell_bytes(spell_id=587, state=2, cost=1000, point_cost=(0, 0),
                        req_level=10, req_skill_line=0, req_skill_rank=0,
                        req_abilities=(0, 0, 0)) -> bytes:
    return (struct.pack('<i', spell_id) + bytes([state]) + struct.pack('<i', cost)
            + struct.pack('<2i', *point_cost) + bytes([req_level])
            + struct.pack('<i', req_skill_line) + struct.pack('<i', req_skill_rank)
            + struct.pack('<3i', *req_abilities))


class ParseTrainerListTest(unittest.TestCase):
    def test_spells(self):
        payload = (struct.pack('<Q', 0xF130000000009999) + struct.pack('<i', 0)
                   + struct.pack('<i', 2)
                   + trainer_spell_bytes(spell_id=587, req_level=10)
                   + trainer_spell_bytes(spell_id=133, req_level=1, req_abilities=(587, 0, 0))
                   + cstring("Welcome, mage."))
        info = npc.parse_trainer_list(payload)
        self.assertEqual(info["trainer_guid"], 0xF130000000009999)
        self.assertEqual(info["trainer_type"], 0)
        self.assertEqual(len(info["spells"]), 2)
        self.assertEqual(info["spells"][0]["spell_id"], 587)
        self.assertEqual(info["spells"][0]["required_level"], 10)
        self.assertEqual(info["spells"][1]["required_abilities"], [587, 0, 0])
        self.assertEqual(info["greeting"], "Welcome, mage.")


class ParseNpcTextUpdateTest(unittest.TestCase):
    def test_found(self):
        def option(prob=1.0, text0="Hello there.", text1="", language=0):
            return (struct.pack('<f', prob) + cstring(text0) + cstring(text1)
                    + struct.pack('<i', language) + struct.pack('<6I', 0, 0, 0, 0, 0, 0))

        payload = struct.pack('<I', 555) + option(text0="Welcome to my shop.") \
            + b''.join(option(prob=0.0, text0="") for _ in range(7))
        info = npc.parse_npc_text_update(payload)
        self.assertTrue(info["found"])
        self.assertEqual(info["text_id"], 555)
        self.assertEqual(len(info["options"]), 8)
        self.assertEqual(info["options"][0]["text0"], "Welcome to my shop.")
        self.assertEqual(info["options"][0]["emotes"], [{"delay": 0, "id": 0}] * 3)

    def test_not_found(self):
        payload = struct.pack('<I', 555 | 0x80000000)
        info = npc.parse_npc_text_update(payload)
        self.assertEqual(info, {"text_id": 555, "found": False})


class NpcTextCacheTest(unittest.TestCase):
    def test_dedupes_in_flight_and_known(self):
        cache = npc.NpcTextCache()
        cache.want(1, 0xABCD)
        cache.want(1, 0xABCD)  # in-flight, ignored
        self.assertEqual(len(cache.drain()), 1)
        cache.on_response({"text_id": 1, "found": True, "options": []})
        cache.want(1, 0xABCD)  # already known, ignored
        self.assertEqual(cache.drain(), [])

    def test_budget_limits_drain(self):
        cache = npc.NpcTextCache(budget_per_second=2)
        for i in range(5):
            cache.want(i, 0xABCD)
        self.assertEqual(len(cache.drain()), 2)
        self.assertEqual(len(cache.drain()), 0)  # budget exhausted this second
