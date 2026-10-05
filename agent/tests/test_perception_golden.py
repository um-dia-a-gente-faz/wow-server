"""Golden test for WorldState.snapshot() (#265): one world touching every
snapshot section, compared byte-for-byte with a committed JSON. A refactor of
agent/perception must leave it unchanged. Regenerate on purpose with
`python3 -m agent.tests.test_perception_golden --write`."""

import json
import pathlib
import sys
import unittest
from unittest import mock

from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo
from agent.tests.test_perception import create_block

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "perception" / "snapshot_golden.json"
ME = 0x1
HEAD = 0xF120000000000005
FOOD = 0xF120000000000006


def _guid_fields(base, guid):
    return {base: guid & 0xFFFFFFFF, base + 1: guid >> 32}


def build_world():
    ws = per.WorldState()
    ws.set_my_map(0)
    ws.set_my_guid(ME)
    raw = {}
    raw.update(_guid_fields(uf.PLAYER_FIELD_INV_SLOT_HEAD, HEAD))
    raw.update(_guid_fields(uf.PLAYER_FIELD_PACK_SLOT_1, FOOD))
    q = uf.PLAYER_QUEST_LOG_1_1
    raw.update({q: 33, q + 1: 0, q + 2: 3, q + 4: 0})
    ws.update_object(create_block(ME, uo.TYPEID_PLAYER, 10.0, 10.0, 0.0, fields=raw))
    for guid, entry, count in ((HEAD, 1234, 1), (FOOD, 159, 4)):
        ws.update_object(create_block(guid, uo.TYPEID_ITEM, fields={
            uf.OBJECT_FIELD_ENTRY: entry, uf.ITEM_FIELD_STACK_COUNT: count}))
    ws.apply_item_query_response({"entry": 159, "found": True, "name": "Tough Jerky"})
    ws.items.items[1234] = {"name": "Cap", "quality": 2, "inventory_type": 1, "stats": [], "extra": 1}
    ws.update_object(create_block(0x10, uo.TYPEID_UNIT, 12.0, 10.0, 0.0, fields={
        uf.OBJECT_FIELD_ENTRY: 6368, uf.UNIT_FIELD_HEALTH: 5, uf.UNIT_FIELD_MAXHEALTH: 10,
        uf.UNIT_FIELD_LEVEL: 3}))
    ws.update_object(create_block(0x11, uo.TYPEID_UNIT, 11.0, 10.0, 0.0, fields={
        uf.OBJECT_FIELD_ENTRY: 6369}))
    ws.update_object(create_block(0x12, uo.TYPEID_PLAYER, 13.0, 10.0, 0.0))
    ws.update_object(create_block(0x13, uo.TYPEID_GAMEOBJECT, 14.0, 10.0, 0.0))
    ws.update_object(create_block(0x14, uo.TYPEID_UNIT, 500.0, 500.0, 0.0))  # out of range
    ws.apply_creature_query_response({"entry": 6368, "found": True, "name": "Wolf", "subname": "",
                                      "rank": "normal", "creature_type_name": "beast"})
    ws.apply_questgiver_status({"guid": 0x11, "status": 4, "status_name": "available"})
    ws.apply_gossip_message({"npc_guid": 0x11, "text_id": 7, "options": [], "quests": []})
    ws.start_trade_request(0x12, True)
    ws.open_mailbox_request(0x13)
    ws.apply_received_mail({})
    ws.apply_channel_notify({"notice_name": "you_joined", "channel": "General - Elwynn",
                             "channel_id": 1, "flags": 0})
    ws.quest_texts.quests[33] = {"title": "Wolves", "objectives": "Kill 8", "required_credit": [
        {"entry": 6368, "count": 8}], "required_items": [{"entry": 99, "count": 2}]}
    return ws


def snapshot_json():
    group = {"leader_guid": 0x12, "raid": False, "loot_method": 1,
             "members": [{"guid": 0x12, "name": "Bob"}]}
    with mock.patch("time.monotonic", return_value=100.0):
        ws = build_world()
    snap = ws.snapshot(
        corpse_position=(0, 1.0, 2.0, 3.0, 0.0), pending_invite={"inviter": "Bob"},
        chat_inbox=[{"from": "Bob", "text": "hi"}], group=group, limit=2)
    return json.dumps(snap, indent=1, sort_keys=True, default=repr)


class SnapshotGoldenTest(unittest.TestCase):
    def test_snapshot_matches_golden(self):
        self.assertEqual(snapshot_json(), GOLDEN.read_text().rstrip("\n"))


    def test_no_perception_module_over_300_lines(self):
        for path in sorted(pathlib.Path(per.__file__).parent.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertLessEqual(len(path.read_text().splitlines()), 300)


if __name__ == "__main__":
    if "--write" in sys.argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(snapshot_json() + "\n")
    else:
        unittest.main()
