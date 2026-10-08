"""Unit tests for agent.quests (UM-41): golden-byte request builders,
fixture-based response parsers, quest log field decoding
(agent.update_fields.decode_quest_log), WorldState wiring
(agent.perception), and the accept/complete/turn_in/abandon Action
subclasses (agent.actions) — mirrors the fixture patterns in
agent/tests/test_npc.py and agent/tests/test_perception.py."""

import os
import pathlib
import struct
import tempfile
import unittest
import uuid

from agent import actions as ac
from agent import perception as per
from agent import quests as qu
from agent import session as se
from agent import update_fields as uf
from agent import update_object as uo
from agent.tests.builders import FakeSession, make_session, reward_list


def cstring(s: str) -> bytes:
    return s.encode('utf-8') + b'\x00'


# ── Request builders (golden bytes) ────────────────────────────────────────

class OpcodeValuesTest(unittest.TestCase):
    def test_questgiver_quest_details_opcode(self):
        # 0x187 is CMSG_QUESTGIVER_QUERY_QUEST's response slot in some stale
        # references; the real 3.3.5a SMSG_QUESTGIVER_QUEST_DETAILS value is
        # 0x188.
        self.assertEqual(qu.SMSG_QUESTGIVER_QUEST_DETAILS, 0x188)


class BuildRequestTest(unittest.TestCase):
    def test_quest_query_is_only_the_quest_id(self):
        # QueryQuestInfo::Read reads a single uint32; the guid is ignored.
        self.assertEqual(qu.build_quest_query(1234, 0xABCD), struct.pack('<I', 1234))
        self.assertEqual(qu.build_quest_query(1234), struct.pack('<I', 1234))

    def test_questgiver_status_query(self):
        self.assertEqual(qu.build_questgiver_status_query(0x1234), struct.pack('<Q', 0x1234))

    def test_questgiver_hello(self):
        self.assertEqual(qu.build_questgiver_hello(0x1234), struct.pack('<Q', 0x1234))

    def test_questgiver_query_quest(self):
        self.assertEqual(qu.build_questgiver_query_quest(0x1234, 100), struct.pack('<QI', 0x1234, 100))

    def test_questgiver_accept_quest(self):
        self.assertEqual(qu.build_questgiver_accept_quest(0x1234, 100),
                          struct.pack('<QII', 0x1234, 100, 0))

    def test_questgiver_complete_quest(self):
        self.assertEqual(qu.build_questgiver_complete_quest(0x1234, 100), struct.pack('<QI', 0x1234, 100))

    def test_questgiver_request_reward(self):
        self.assertEqual(qu.build_questgiver_request_reward(0x1234, 100), struct.pack('<QI', 0x1234, 100))

    def test_questgiver_choose_reward_default(self):
        self.assertEqual(qu.build_questgiver_choose_reward(0x1234, 100),
                          struct.pack('<QII', 0x1234, 100, 0))

    def test_questgiver_choose_reward_explicit(self):
        self.assertEqual(qu.build_questgiver_choose_reward(0x1234, 100, 2),
                          struct.pack('<QII', 0x1234, 100, 2))

    def test_questlog_remove_quest(self):
        self.assertEqual(qu.build_questlog_remove_quest(5), struct.pack('<B', 5))


# ── Response parsers ────────────────────────────────────────────────────

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "quests"


def fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def quest_query_payload(quest_id=100, title="Quest A", objectives="Do things.",
                        required_credit=((0, 0),) * 4, required_items=((0, 0),) * 6,
                        objective_texts=("",) * 4, reward_choices=((0, 0),) * 6) -> bytes:
    """Hand-built SMSG_QUEST_QUERY_RESPONSE in QueryQuestInfoResponse::Write
    order, for paths the live fixture doesn't cover (GO objectives, item
    objectives, custom objective text)."""
    out = struct.pack('<IIiIiII', quest_id, 2, 5, 3, 12, 0, 0)
    out += struct.pack('<IiIi', 0, 0, 0, 0)                    # required factions
    out += struct.pack('<II', 0, 4)                            # next quest, xp difficulty
    out += struct.pack('<iIIi', 250, 0, 0, 0)                  # money, bonus money, display spell, spell
    out += struct.pack('<IfIIIIIIi', 0, 0.0, 0, 8, 0, 0, 0, 0, 0)[:36]  # honor..reward faction flags
    out += b''.join(struct.pack('<II', 0, 0) for _ in range(4))          # reward items
    out += b''.join(struct.pack('<II', e, c) for e, c in reward_choices)
    out += struct.pack('<5I', 0, 0, 0, 0, 0) + struct.pack('<5i', 0, 0, 0, 0, 0) * 2
    out += struct.pack('<IffI', 0, 0.0, 0.0, 0)                # POI
    out += cstring(title) + cstring(objectives) + cstring("Details.") + cstring("") + cstring("Return.")
    for entry, count in required_credit:
        out += struct.pack('<IIII', entry, count, 0, 0)
    for entry, count in required_items:
        out += struct.pack('<II', entry, count)
    for text in objective_texts:
        out += cstring(text)
    return out


class ParseQuestQueryResponseTest(unittest.TestCase):
    def test_live_fixture_reclaiming_sunstrider_isle(self):
        # Captured live 2026-09-24 (fixtures/quests/README.md). The old
        # parser read the zone id 3431 (0x0D67) as the title: "g\r".
        info = qu.parse_quest_query_response(fixture("quest_query_response_8325.bin"))
        self.assertEqual(info["quest_id"], 8325)
        self.assertEqual(info["title"], "Reclaiming Sunstrider Isle")
        self.assertTrue(info["objectives"].startswith("Kill 8 Mana Wyrms"))
        self.assertTrue(info["details"].startswith("The sooner you begin your education"))
        self.assertEqual(info["completion_text"],
                         "Return to Magistrix Erona at Sunstrider Isle in Eversong Woods.")
        self.assertEqual(info["level"], 1)
        self.assertEqual(info["min_level"], 1)
        self.assertEqual(info["sort_id"], 3431)
        self.assertEqual(info["flags"], 524424)
        self.assertEqual(info["reward_money"], 30)
        self.assertEqual(info["reward_choice_items"],
                         [{"entry": 20997, "count": 1}, {"entry": 20998, "count": 1}])
        self.assertEqual(info["reward_reputations"], [{"faction": 911, "value": 5, "override": 0}])
        self.assertEqual(len(info["required_credit"]), 4)
        self.assertEqual(info["required_credit"][0],
                         {"entry": 15274, "gameobject": False, "count": 8,
                          "item_drop": 0, "item_drop_count": 0, "text": ""})
        self.assertEqual([r["count"] for r in info["required_credit"][1:]], [0, 0, 0])
        self.assertEqual(len(info["required_items"]), 6)
        self.assertTrue(all(r["count"] == 0 for r in info["required_items"]))

    def test_gameobject_item_and_custom_text_objectives(self):
        payload = quest_query_payload(
            required_credit=((1234, 10), (0x80000000 | 181000, 3), (0, 0), (0, 0)),
            required_items=((2589, 5), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0)),
            objective_texts=("", "Crystal destroyed", "", ""),
            reward_choices=((3741, 1), (0, 0), (3742, 2), (0, 0), (0, 0), (0, 0)))
        info = qu.parse_quest_query_response(payload)
        self.assertEqual(info["required_credit"][0]["entry"], 1234)
        self.assertFalse(info["required_credit"][0]["gameobject"])
        self.assertEqual(info["required_credit"][1]["entry"], 181000)
        self.assertTrue(info["required_credit"][1]["gameobject"])
        self.assertEqual(info["required_credit"][1]["text"], "Crystal destroyed")
        self.assertEqual(info["required_items"][0], {"entry": 2589, "count": 5})
        self.assertEqual(info["reward_choice_items"],
                         [{"entry": 3741, "count": 1}, {"entry": 3742, "count": 2}])

    def test_truncated_payload_raises(self):
        with self.assertRaises((IndexError, struct.error, ValueError)):
            qu.parse_quest_query_response(fixture("quest_query_response_8325.bin")[:-3])

    def test_trailing_bytes_raise(self):
        with self.assertRaises(ValueError):
            qu.parse_quest_query_response(fixture("quest_query_response_8325.bin") + b"\x00")


class ParseQuestgiverStatusTest(unittest.TestCase):
    def test_known_status(self):
        # SMSG_QUESTGIVER_STATUS is guid (8 bytes) + a single status byte
        # (9 bytes total), not guid + uint32 (12 bytes).
        payload = struct.pack('<Q', 0xF130000000001234) + struct.pack('<B', 4)
        self.assertEqual(len(payload), 9)
        info = qu.parse_questgiver_status(payload)
        self.assertEqual(info["guid"], 0xF130000000001234)
        self.assertEqual(info["status"], 4)
        self.assertEqual(info["status_name"], "available")

    def test_unknown_status_falls_back_to_status_n(self):
        payload = struct.pack('<Q', 1) + struct.pack('<B', 77)
        self.assertEqual(len(payload), 9)
        info = qu.parse_questgiver_status(payload)
        self.assertEqual(info["status_name"], "status_77")


class ParseQuestgiverQuestListTest(unittest.TestCase):
    def test_two_quests(self):
        # PlayerMenu::SendQuestGiverQuestList: per quest id, icon, level,
        # flags, repeatable byte, title.
        payload = (
            struct.pack('<Q', 0x1234) + cstring("Greetings!")
            + struct.pack('<II', 0, 1)
            + bytes([2])
            + struct.pack('<IIiI', 100, 2, 10, 8) + bytes([0]) + cstring("Quest A")
            + struct.pack('<IIiI', 101, 4, 12, 0) + bytes([1]) + cstring("Quest B")
        )
        info = qu.parse_questgiver_quest_list(payload)
        self.assertEqual(info["npc_guid"], 0x1234)
        self.assertEqual(info["title"], "Greetings!")
        self.assertEqual(info["quests"][0], {"quest_id": 100, "icon": 2, "level": 10, "flags": 8,
                                              "repeatable": False, "title": "Quest A"})
        self.assertEqual(info["quests"][1]["quest_id"], 101)
        self.assertTrue(info["quests"][1]["repeatable"])


class ParseQuestgiverQuestDetailsTest(unittest.TestCase):
    def test_details(self):
        # QuestGiverQuestDetails::Write: note the second guid (InformUnit)
        # before the quest id.
        payload = (
            struct.pack('<QQI', 0x1234, 0, 8325)
            + cstring("Reclaiming Sunstrider Isle") + cstring("Details here.") + cstring("Kill 8 wyrms.")
            + bytes([1]) + struct.pack('<II', 0x80088, 0) + bytes([0])
            + reward_list([(20997, 1, 111), (20998, 1, 222)])
            + reward_list([])
            + struct.pack('<III', 30, 45, 0) + struct.pack('<f', 0.0)
            + struct.pack('<IiIIII', 0, 0, 0, 0, 0, 1)
            + struct.pack('<5I', 911, 0, 0, 0, 0) + struct.pack('<5i', 5, 0, 0, 0, 0)
            + struct.pack('<5i', 0, 0, 0, 0, 0)
            + struct.pack('<i', 2) + struct.pack('<IIII', 1, 0, 0, 0)
        )
        info = qu.parse_questgiver_quest_details(payload)
        self.assertEqual(info["npc_guid"], 0x1234)
        self.assertEqual(info["quest_id"], 8325)
        self.assertEqual(info["title"], "Reclaiming Sunstrider Isle")
        self.assertEqual(info["objectives"], "Kill 8 wyrms.")
        self.assertTrue(info["auto_launched"])
        self.assertEqual(info["reward_choice_items"][1], {"entry": 20998, "count": 1, "display_id": 222})
        self.assertEqual(info["reward_items"], [])
        self.assertEqual(info["reward_money"], 30)
        self.assertEqual(info["reward_xp"], 45)
        self.assertEqual(len(info["emotes"]), 2)


class ParseQuestgiverRequestItemsTest(unittest.TestCase):
    def test_request_items(self):
        payload = (
            struct.pack('<Qi', 0x1234, 100)
            + cstring("Quest A") + cstring("Bring me items.")
            + struct.pack('<iiiIii', 0, 1, 0, 0, 0, 0)
            + struct.pack('<I', 1) + struct.pack('<iiI', 2589, 5, 1234)
            + struct.pack('<IIII', 0, 4, 8, 16)
        )
        info = qu.parse_questgiver_request_items(payload)
        self.assertEqual(info["quest_id"], 100)
        self.assertEqual(info["request_items_text"], "Bring me items.")
        self.assertEqual(info["required_items"], [{"entry": 2589, "count": 5, "display_id": 1234}])
        self.assertFalse(info["can_complete"])


class ParseQuestgiverOfferRewardTest(unittest.TestCase):
    def test_offer_reward(self):
        payload = (
            struct.pack('<QI', 0x1234, 100)
            + cstring("Quest A") + cstring("Well done, take this.")
            + bytes([1]) + struct.pack('<II', 0, 0)
            + struct.pack('<I', 1) + struct.pack('<II', 0, 1)
            + reward_list([(3741, 1, 11), (3742, 1, 12)])
            + reward_list([(3740, 1, 10)])
            + struct.pack('<III', 500, 1000, 0) + struct.pack('<f', 0.0)
            + struct.pack('<IIiIIII', 0, 0, 0, 0, 0, 0, 0)
            + struct.pack('<15i', *([0] * 15))
        )
        info = qu.parse_questgiver_offer_reward(payload)
        self.assertEqual(info["npc_guid"], 0x1234)
        self.assertEqual(info["quest_id"], 100)
        self.assertEqual(info["offer_reward_text"], "Well done, take this.")
        self.assertEqual(info["reward_items"], [{"entry": 3740, "count": 1, "display_id": 10}])
        self.assertEqual([i["entry"] for i in info["reward_choice_items"]], [3741, 3742])
        self.assertEqual(info["reward_money"], 500)
        self.assertEqual(info["reward_xp"], 1000)


class ParseQuestgiverQuestCompleteTest(unittest.TestCase):
    def test_quest_complete(self):
        # Player::SendQuestReward: quest, xp, money, honor, talents, arena.
        payload = struct.pack('<6I', 100, 1000, 500, 0, 0, 0)
        info = qu.parse_questgiver_quest_complete(payload)
        self.assertEqual(info, {"quest_id": 100, "xp_reward": 1000, "money_reward": 500,
                                 "honor_reward": 0, "talent_reward": 0, "arena_reward": 0})


class ParseQuestUpdateEventsTest(unittest.TestCase):
    def test_add_kill_live_fixture(self):
        # Sunspeaker's first Mana Wyrm kill on quest 8325 (fixtures/quests/README.md).
        info = qu.parse_questupdate_add_kill(fixture("questupdate_add_kill_8325.bin"))
        self.assertEqual(info, {"quest_id": 8325, "entry": 15274, "gameobject": False, "count": 1,
                                 "required": 8, "victim_guid": 0xF130003BAA002500})

    def test_add_kill_gameobject_entry(self):
        payload = struct.pack('<IIII', 100, 0x80000000 | 181000, 2, 3) + struct.pack('<Q', 5)
        info = qu.parse_questupdate_add_kill(payload)
        self.assertEqual(info["entry"], 181000)
        self.assertTrue(info["gameobject"])

    def test_add_item_is_empty_on_335(self):
        self.assertEqual(qu.parse_questupdate_add_item(b''), {})

    def test_complete(self):
        payload = struct.pack('<I', 100)
        self.assertEqual(qu.parse_questupdate_complete(payload), {"quest_id": 100})


class QuestEventDispatchTest(unittest.TestCase):
    """The session handlers must record the parsed packet, not drop it: the
    first live ADD_KILL was dropped with "_record_event() got multiple
    values for argument 'kind'" (UM-91)."""

    def test_add_kill_is_recorded(self):
        sess = make_session()
        sess._dispatch(qu.SMSG_QUESTUPDATE_ADD_KILL, fixture("questupdate_add_kill_8325.bin"))
        event = sess.events[-1]
        self.assertEqual(event["kind"], "quest_progress")
        self.assertEqual(event["objective"], "kill")
        self.assertEqual(event["quest_id"], 8325)
        self.assertEqual((event["count"], event["required"]), (1, 8))

    def test_add_item_is_recorded(self):
        sess = make_session()
        sess._dispatch(qu.SMSG_QUESTUPDATE_ADD_ITEM, b'')
        self.assertEqual(sess.events[-1]["kind"], "quest_progress")
        self.assertEqual(sess.events[-1]["objective"], "item")

    def test_complete_turned_in_and_failed_are_recorded(self):
        sess = make_session()
        sess._dispatch(qu.SMSG_QUESTUPDATE_COMPLETE, struct.pack('<I', 8325))
        sess._dispatch(qu.SMSG_QUESTGIVER_QUEST_COMPLETE, struct.pack('<6I', 8325, 45, 30, 0, 0, 0))
        sess._dispatch(qu.SMSG_QUESTGIVER_QUEST_FAILED, struct.pack('<II', 8325, 4))
        self.assertEqual([e["kind"] for e in sess.events][-3:],
                         ["quest_complete", "quest_turned_in", "quest_failed"])

    def test_quest_query_response_reaches_the_cache(self):
        sess = make_session()
        sess._dispatch(qu.SMSG_QUEST_QUERY_RESPONSE, fixture("quest_query_response_8325.bin"))
        self.assertEqual(sess.world_state.quest_texts.quests[8325]["title"], "Reclaiming Sunstrider Isle")


class QuestCacheTest(unittest.TestCase):
    def test_want_dedupes_and_drains(self):
        cache = qu.QuestCache()
        cache.want(100, 0x1234)
        cache.want(100, 0x1234)  # duplicate, ignored
        drained = cache.drain()
        self.assertEqual(drained, [(100, 0x1234)])
        self.assertEqual(cache.drain(), [])  # nothing left pending

    def test_on_response_clears_in_flight_and_caches(self):
        cache = qu.QuestCache()
        cache.want(100)
        cache.drain()
        cache.on_response({"quest_id": 100, "title": "Quest A"})
        self.assertIn(100, cache.quests)
        cache.want(100)  # already cached, not re-queued
        self.assertEqual(cache.drain(), [])


# ── Quest log field decoding (agent.update_fields) ─────────────────────────

class DecodeQuestLogTest(unittest.TestCase):
    def _fields_for_slot(self, slot, quest_id, state=0, counters=(0, 0, 0, 0), qtime=0):
        base = uf.PLAYER_QUEST_LOG_1_1 + slot * uf.QUEST_LOG_FIELDS_PER_SLOT
        counters_a = (counters[0] & 0xFFFF) | ((counters[1] & 0xFFFF) << 16)
        counters_b = (counters[2] & 0xFFFF) | ((counters[3] & 0xFFFF) << 16)
        return {base: quest_id, base + 1: state, base + 2: counters_a, base + 3: counters_b, base + 4: qtime}

    def test_empty_log_returns_no_slots(self):
        self.assertEqual(uf.decode_quest_log({}), [])

    def test_single_slot(self):
        raw = self._fields_for_slot(0, 2158, state=3, counters=(3, 0, 0, 0), qtime=12345)
        slots = uf.decode_quest_log(raw)
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["slot"], 0)
        self.assertEqual(slots[0]["quest_id"], 2158)
        self.assertEqual(slots[0]["state"], 3)
        self.assertEqual(slots[0]["counters"], [3, 0, 0, 0])
        self.assertEqual(slots[0]["time"], 12345)

    def test_multiple_slots_out_of_order_indices(self):
        raw = {}
        raw.update(self._fields_for_slot(0, 100))
        raw.update(self._fields_for_slot(5, 200, counters=(1, 2, 3, 4)))
        slots = uf.decode_quest_log(raw)
        self.assertEqual([s["slot"] for s in slots], [0, 5])
        self.assertEqual(slots[1]["quest_id"], 200)
        self.assertEqual(slots[1]["counters"], [1, 2, 3, 4])

    def test_zero_quest_id_slot_is_skipped(self):
        raw = self._fields_for_slot(3, 0)
        self.assertEqual(uf.decode_quest_log(raw), [])

    def test_last_slot_index_25_offsets_correctly(self):
        # Sanity check against the arithmetic cited in update_fields.py:
        # slot 24 (the 25th, last) must land exactly at PLAYER_QUEST_LOG_1_1
        # + 24*5 = UNIT_END + 0x0A + 120, one 5-field slot before
        # PLAYER_VISIBLE_ITEM_1_ENTRYID (UNIT_END + 0x87 per the comment).
        base_slot_24 = uf.PLAYER_QUEST_LOG_1_1 + 24 * uf.QUEST_LOG_FIELDS_PER_SLOT
        self.assertEqual(base_slot_24 + 5, uf.UNIT_END + 0x87)


# ── WorldState wiring (agent.perception) ───────────────────────────────────

def create_block(guid, object_type=uo.TYPEID_PLAYER, fields=None):
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=object_type,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0},
        fields=fields or {},
    )


class QuestGiverStatusWiringTest(unittest.TestCase):
    def test_new_quest_giver_is_queued_for_status_query(self):
        ws = per.WorldState()
        ws.update_object(create_block(1, object_type=uo.TYPEID_UNIT,
                                       fields={0x03: 500, uf.UNIT_NPC_FLAGS: per.UNIT_NPC_FLAG_QUESTGIVER}))
        queued = ws.drain_quest_giver_status_queries()
        self.assertEqual(queued, [1])
        # Draining again returns nothing until re-queued.
        self.assertEqual(ws.drain_quest_giver_status_queries(), [])

    def test_status_response_backfills_object(self):
        ws = per.WorldState()
        ws.update_object(create_block(1, object_type=uo.TYPEID_UNIT,
                                       fields={uf.UNIT_NPC_FLAGS: per.UNIT_NPC_FLAG_QUESTGIVER}))
        ws.drain_quest_giver_status_queries()
        ws.apply_questgiver_status({"guid": 1, "status": 4, "status_name": "available"})
        obj = ws.get_object(1)
        self.assertEqual(obj.quest_giver_status, 4)
        self.assertEqual(obj.quest_giver_status_name, "available")

    def test_snapshot_exposes_quest_giver_status_on_nearby_unit(self):
        ws = per.WorldState()
        ws.set_my_guid(99)
        ws.set_my_map(0)
        ws.update_object(create_block(99, object_type=uo.TYPEID_PLAYER))
        ws.update_object(create_block(1, object_type=uo.TYPEID_UNIT,
                                       fields={uf.UNIT_NPC_FLAGS: per.UNIT_NPC_FLAG_QUESTGIVER}))
        ws.apply_questgiver_status({"guid": 1, "status": 4, "status_name": "available"})
        snap = ws.snapshot(my_position=(0, 0.0, 0.0, 0.0, 0.0))
        unit = next(u for u in snap["nearby_units"] if ws.handles.resolve(u["guid"]) == 1)
        self.assertEqual(unit["quest_giver_status"], "available")


class QuestLogSnapshotTest(unittest.TestCase):
    def _log_fields(self, slot, quest_id, counters=(0, 0, 0, 0)):
        base = uf.PLAYER_QUEST_LOG_1_1 + slot * uf.QUEST_LOG_FIELDS_PER_SLOT
        counters_a = (counters[0] & 0xFFFF) | ((counters[1] & 0xFFFF) << 16)
        counters_b = (counters[2] & 0xFFFF) | ((counters[3] & 0xFFFF) << 16)
        return {base: quest_id, base + 1: 0, base + 2: counters_a, base + 3: counters_b, base + 4: 0}

    def test_quest_log_appears_in_snapshot_and_queues_text_query(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.update_object(create_block(1, fields=self._log_fields(0, 2158, counters=(3, 0, 0, 0))))
        log = ws.build_quest_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["quest_id"], 2158)
        self.assertEqual(log[0]["counters"], [3, 0, 0, 0])
        self.assertNotIn("title", log[0])  # not cached yet
        self.assertEqual(ws.quest_texts.drain(), [(2158, 0)])

    def _world_with_quest(self, counters=(0, 0, 0, 0), state=0):
        ws = per.WorldState()
        ws.names.cache_path = os.path.join(tempfile.gettempdir(),
                                           f"wow-agent-test-names-{uuid.uuid4().hex}.json")
        ws.set_my_guid(1)
        fields = self._log_fields(0, 8325, counters=counters)
        fields[uf.PLAYER_QUEST_LOG_1_1 + 1] = state
        ws.update_object(create_block(1, fields=fields))
        ws.apply_quest_query_response(
            qu.parse_quest_query_response(fixture("quest_query_response_8325.bin")))
        return ws

    def test_live_quest_text_gives_title_and_named_objective(self):
        # UM-91 acceptance: the live snapshot said title "g\r" and
        # "393216: 0/1966080"; with the real layout it's the quest title and
        # counter 0 against the Mana Wyrm objective.
        ws = self._world_with_quest(counters=(3, 0, 0, 0))
        ws.names.creatures[15274] = {"entry": 15274, "name": "Mana Wyrm", "found": True}
        log = ws.build_quest_log()
        self.assertEqual(log[0]["title"], "Reclaiming Sunstrider Isle")
        self.assertTrue(log[0]["objectives_text"].startswith("Kill 8 Mana Wyrms"))
        self.assertEqual(log[0]["state_name"], "incomplete")
        self.assertEqual(log[0]["objectives"], [{
            "entry": 15274, "gameobject": False, "name": "Mana Wyrm",
            "count": 3, "needed": 8, "text": "Mana Wyrm slain: 3/8"}])
        snap = ws.snapshot(my_position=(0, 0.0, 0.0, 0.0, 0.0))
        self.assertEqual(snap["quest_log"][0]["title"], "Reclaiming Sunstrider Isle")

    def test_unknown_objective_creature_is_queued_for_a_name_query(self):
        ws = self._world_with_quest()
        log = ws.build_quest_log()
        self.assertEqual(log[0]["objectives"][0]["text"], "creature 15274 slain: 0/8")
        self.assertIn(("creature", 15274, 0), ws.names.drain())

    def test_slot_state_is_a_bitmask(self):
        # QuestSlotStateMask (Player.h): 0 = in progress, 1 = complete, 2 = failed.
        self.assertEqual(self._world_with_quest(state=1).build_quest_log()[0]["state_name"], "complete")
        self.assertEqual(self._world_with_quest(state=2).build_quest_log()[0]["state_name"], "failed")

    def test_item_objectives_have_no_counter(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.update_object(create_block(1, fields=self._log_fields(0, 100, counters=(2, 0, 0, 0))))
        ws.apply_quest_query_response(qu.parse_quest_query_response(quest_query_payload(
            required_credit=((0x80000000 | 181000, 3), (0, 0), (0, 0), (0, 0)),
            required_items=((2589, 5), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0)),
            objective_texts=("Crystal destroyed", "", "", ""))))
        objectives = ws.build_quest_log()[0]["objectives"]
        self.assertEqual(objectives[0]["text"], "Crystal destroyed: 2/3")
        self.assertTrue(objectives[0]["gameobject"])
        self.assertEqual(objectives[1], {"item": 2589, "count": None, "needed": 5,
                                          "text": "item 2589: ?/5"})


# ── Actions (agent.actions) ────────────────────────────────────────────────

def fake_session(player_position=(0, 0.0, 0.0, 0.0, 0.0)):
    sess = FakeSession(player_position=player_position, events=[])
    return sess


class FakeWorld:
    """Minimal world double exposing exactly what quest actions' check()/
    execute() touch: get_object, get_ui_state, build_quest_log."""

    def __init__(self, objects=None, ui_state=None, quest_log=None):
        self._objects = objects or {}
        self._ui_state = ui_state
        self._quest_log = quest_log or []

    def get_object(self, guid):
        return self._objects.get(guid)

    def get_ui_state(self):
        return self._ui_state

    def build_quest_log(self):
        return self._quest_log


class FakeTarget:
    def __init__(self, distance=1.0):
        self._distance = distance

    def distance_to(self, pos):
        return self._distance


class AcceptQuestActionTest(unittest.TestCase):
    def test_check_rejects_when_no_quest_window_open(self):
        world = FakeWorld(objects={1: FakeTarget()})
        action = ac.REGISTRY["accept_quest"]
        error = action.check(fake_session(), world, npc_guid=1, quest_id=100)
        self.assertIn("no quest list/details/gossip window", error)

    def test_check_rejects_quest_not_in_open_list(self):
        world = FakeWorld(objects={1: FakeTarget()},
                           ui_state={"kind": "quest_list", "npc_guid": 1,
                                     "quests": [{"quest_id": 999, "title": "Other"}]})
        action = ac.REGISTRY["accept_quest"]
        error = action.check(fake_session(), world, npc_guid=1, quest_id=100)
        self.assertIn("not offered", error)

    def test_execute_sends_accept_packet(self):
        world = FakeWorld(objects={1: FakeTarget()},
                           ui_state={"kind": "quest_list", "npc_guid": 1,
                                     "quests": [{"quest_id": 100, "title": "Quest A"}]})
        session = fake_session()
        action = ac.REGISTRY["accept_quest"]
        self.assertIsNone(action.check(session, world, npc_guid=1, quest_id=100))
        result = action.execute(session, world, npc_guid=1, quest_id=100)
        self.assertTrue(result.ok)
        opcode, payload = session._sent[0]
        self.assertEqual(opcode, qu.CMSG_QUESTGIVER_ACCEPT_QUEST)
        self.assertEqual(payload, qu.build_questgiver_accept_quest(1, 100))

    def test_check_out_of_range(self):
        world = FakeWorld(objects={1: FakeTarget(distance=50.0)},
                           ui_state={"kind": "quest_list", "npc_guid": 1, "quests": []})
        action = ac.REGISTRY["accept_quest"]
        error = action.check(fake_session(), world, npc_guid=1, quest_id=100)
        self.assertIn("out of interact range", error)

    # ── Bug 3: gossip-primary questgivers (UM-40 gossip window carries the
    # offered quests, e.g. Magistrix Erona/npc_flags=3) must also work with
    # accept_quest, without opening a separate quest_list/quest_details
    # window first.

    def test_check_accepts_quest_in_open_gossip_window(self):
        world = FakeWorld(objects={1: FakeTarget()},
                           ui_state={"kind": "gossip", "npc_guid": 1, "menu_id": 1, "text_id": 1,
                                     "options": [],
                                     "quests": [{"quest_id": 8325, "quest_type": 0, "level": 5,
                                                 "flags": 0, "repeatable": False,
                                                 "title": "A Threat Within"}]})
        action = ac.REGISTRY["accept_quest"]
        error = action.check(fake_session(), world, npc_guid=1, quest_id=8325)
        self.assertIsNone(error)

    def test_check_rejects_quest_not_in_open_gossip_window(self):
        world = FakeWorld(objects={1: FakeTarget()},
                           ui_state={"kind": "gossip", "npc_guid": 1, "menu_id": 1, "text_id": 1,
                                     "options": [], "quests": [{"quest_id": 999, "title": "Other"}]})
        action = ac.REGISTRY["accept_quest"]
        error = action.check(fake_session(), world, npc_guid=1, quest_id=8325)
        self.assertIn("not offered", error)

    def test_execute_from_gossip_window_queries_then_accepts(self):
        world = FakeWorld(objects={1: FakeTarget()},
                           ui_state={"kind": "gossip", "npc_guid": 1, "menu_id": 1, "text_id": 1,
                                     "options": [],
                                     "quests": [{"quest_id": 8325, "quest_type": 0, "level": 5,
                                                 "flags": 0, "repeatable": False,
                                                 "title": "A Threat Within"}]})
        session = fake_session()
        action = ac.REGISTRY["accept_quest"]
        self.assertIsNone(action.check(session, world, npc_guid=1, quest_id=8325))
        result = action.execute(session, world, npc_guid=1, quest_id=8325)
        self.assertTrue(result.ok)
        self.assertEqual(len(session._sent), 2)
        query_opcode, query_payload = session._sent[0]
        self.assertEqual(query_opcode, qu.CMSG_QUESTGIVER_QUERY_QUEST)
        self.assertEqual(query_payload, qu.build_questgiver_query_quest(1, 8325))
        accept_opcode, accept_payload = session._sent[1]
        self.assertEqual(accept_opcode, qu.CMSG_QUESTGIVER_ACCEPT_QUEST)
        self.assertEqual(accept_payload, qu.build_questgiver_accept_quest(1, 8325))


class CompleteQuestActionTest(unittest.TestCase):
    def test_check_rejects_when_quest_not_in_log(self):
        world = FakeWorld(objects={1: FakeTarget()}, quest_log=[])
        action = ac.REGISTRY["complete_quest"]
        error = action.check(fake_session(), world, npc_guid=1, quest_id=100)
        self.assertIn("not in the quest log", error)

    def test_execute_sends_complete_packet(self):
        world = FakeWorld(objects={1: FakeTarget()}, quest_log=[{"quest_id": 100, "slot": 0}])
        session = fake_session()
        action = ac.REGISTRY["complete_quest"]
        self.assertIsNone(action.check(session, world, npc_guid=1, quest_id=100))
        result = action.execute(session, world, npc_guid=1, quest_id=100)
        self.assertTrue(result.ok)
        opcode, payload = session._sent[0]
        self.assertEqual(opcode, qu.CMSG_QUESTGIVER_COMPLETE_QUEST)
        self.assertEqual(payload, qu.build_questgiver_complete_quest(1, 100))


class TurnInQuestActionTest(unittest.TestCase):
    def test_check_rejects_without_offer_reward_window(self):
        world = FakeWorld()
        action = ac.REGISTRY["turn_in_quest"]
        error = action.check(fake_session(), world, npc_guid=1, quest_id=100)
        self.assertIn("no offer-reward window", error)

    def test_check_rejects_out_of_range_reward_choice(self):
        world = FakeWorld(ui_state={"kind": "quest_offer_reward", "npc_guid": 1, "quest_id": 100,
                                     "reward_choice_items": [{"entry": 1, "count": 1}]})
        action = ac.REGISTRY["turn_in_quest"]
        error = action.check(fake_session(), world, npc_guid=1, quest_id=100, reward_choice=5)
        self.assertIn("out of range", error)

    def test_execute_sends_choose_reward_packet(self):
        world = FakeWorld(ui_state={"kind": "quest_offer_reward", "npc_guid": 1, "quest_id": 100,
                                     "reward_choice_items": [{"entry": 1, "count": 1},
                                                              {"entry": 2, "count": 1}]})
        session = fake_session()
        action = ac.REGISTRY["turn_in_quest"]
        self.assertIsNone(action.check(session, world, npc_guid=1, quest_id=100, reward_choice=1))
        result = action.execute(session, world, npc_guid=1, quest_id=100, reward_choice=1)
        self.assertTrue(result.ok)
        opcode, payload = session._sent[0]
        self.assertEqual(opcode, qu.CMSG_QUESTGIVER_CHOOSE_REWARD)
        self.assertEqual(payload, qu.build_questgiver_choose_reward(1, 100, 1))


class AbandonQuestActionTest(unittest.TestCase):
    def test_check_rejects_empty_slot(self):
        world = FakeWorld(quest_log=[{"quest_id": 100, "slot": 0}])
        action = ac.REGISTRY["abandon_quest"]
        error = action.check(fake_session(), world, slot=3)
        self.assertIn("empty", error)

    def test_execute_sends_remove_quest_packet(self):
        world = FakeWorld(quest_log=[{"quest_id": 100, "slot": 2}])
        session = fake_session()
        action = ac.REGISTRY["abandon_quest"]
        self.assertIsNone(action.check(session, world, slot=2))
        result = action.execute(session, world, slot=2)
        self.assertTrue(result.ok)
        opcode, payload = session._sent[0]
        self.assertEqual(opcode, qu.CMSG_QUESTLOG_REMOVE_QUEST)
        self.assertEqual(payload, qu.build_questlog_remove_quest(2))


if __name__ == "__main__":
    unittest.main()
