"""Unit tests for agent.quests (UM-41): golden-byte request builders,
fixture-based response parsers, quest log field decoding
(agent.update_fields.decode_quest_log), WorldState wiring
(agent.perception), and the accept/complete/turn_in/abandon Action
subclasses (agent.actions) — mirrors the fixture patterns in
agent/tests/test_npc.py and agent/tests/test_perception.py."""

import struct
import unittest
from types import SimpleNamespace

from agent import actions as ac
from agent import perception as per
from agent import quests as qu
from agent import update_fields as uf
from agent import update_object as uo


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
    def test_quest_query(self):
        self.assertEqual(qu.build_quest_query(1234, 0xABCD), struct.pack('<IQ', 1234, 0xABCD))

    def test_quest_query_default_guid(self):
        self.assertEqual(qu.build_quest_query(1234), struct.pack('<IQ', 1234, 0))

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

def req_pairs(pairs) -> bytes:
    out = b''
    for entry, count in pairs:
        out += struct.pack('<i', entry) + struct.pack('<I', count)
    return out


class ParseQuestQueryResponseTest(unittest.TestCase):
    def test_full_response(self):
        payload = (
            struct.pack('<I', 100)          # quest id
            + struct.pack('<i', 2)          # method
            + struct.pack('<I', 10)         # level
            + struct.pack('<I', 0)          # flags
            + cstring("Rest and Relaxation")
            + cstring("Go kill some rats.")
            + cstring("Rats slain: %d/%d")
            + cstring("Well done.")
            + struct.pack('<I', 500)        # reward money
            + struct.pack('<I', 1000)       # reward xp
            + req_pairs([(1234, 10), (0, 0), (0, 0), (0, 0)])   # required credit
            + req_pairs([(2589, 5), (0, 0), (0, 0), (0, 0)])    # required items
            + req_pairs([(3740, 1), (0, 0), (0, 0), (0, 0)])    # reward items
            + struct.pack('<I', 0)          # next quest in chain
        )
        info = qu.parse_quest_query_response(payload)
        self.assertEqual(info["quest_id"], 100)
        self.assertEqual(info["title"], "Rest and Relaxation")
        self.assertEqual(info["objectives"], "Rats slain: %d/%d")
        self.assertEqual(info["reward_money"], 500)
        self.assertEqual(info["reward_xp"], 1000)
        self.assertEqual(info["required_credit"][0], {"entry": 1234, "count": 10})
        self.assertEqual(info["required_items"][0], {"entry": 2589, "count": 5})
        self.assertEqual(info["reward_items"][0], {"entry": 3740, "count": 1})
        self.assertEqual(info["next_quest_in_chain"], 0)


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
        payload = (
            struct.pack('<Q', 0x1234) + cstring("Greetings!")
            + struct.pack('<I', 0) + struct.pack('<I', 1)
            + bytes([2])
            + struct.pack('<i', 100) + struct.pack('<I', 2) + struct.pack('<i', 10) + cstring("Quest A")
            + struct.pack('<i', 101) + struct.pack('<I', 4) + struct.pack('<i', 12) + cstring("Quest B")
        )
        info = qu.parse_questgiver_quest_list(payload)
        self.assertEqual(info["npc_guid"], 0x1234)
        self.assertEqual(info["title"], "Greetings!")
        self.assertEqual(len(info["quests"]), 2)
        self.assertEqual(info["quests"][0], {"quest_id": 100, "icon": 2, "level": 10, "title": "Quest A"})
        self.assertEqual(info["quests"][1]["quest_id"], 101)


class ParseQuestgiverQuestDetailsTest(unittest.TestCase):
    def test_details(self):
        payload = (
            struct.pack('<Q', 0x1234) + struct.pack('<I', 100)
            + cstring("Quest A") + cstring("Details here.") + cstring("Kill 10 rats.")
            + bytes([0]) + struct.pack('<I', 1)
            + struct.pack('<I', 500) + struct.pack('<I', 1000)
            + req_pairs([(1234, 10), (0, 0), (0, 0), (0, 0)])
            + req_pairs([(0, 0), (0, 0), (0, 0), (0, 0)])
        )
        info = qu.parse_questgiver_quest_details(payload)
        self.assertEqual(info["quest_id"], 100)
        self.assertEqual(info["title"], "Quest A")
        self.assertFalse(info["auto_finish"])
        self.assertEqual(info["required_credit"][0], {"entry": 1234, "count": 10})


class ParseQuestgiverRequestItemsTest(unittest.TestCase):
    def test_request_items(self):
        payload = (
            struct.pack('<Q', 0x1234) + struct.pack('<I', 100)
            + cstring("Quest A") + cstring("Bring me items.")
            + struct.pack('<I', 0) + bytes([0])
            + req_pairs([(2589, 5), (0, 0), (0, 0), (0, 0)])
        )
        info = qu.parse_questgiver_request_items(payload)
        self.assertEqual(info["required_items"][0], {"entry": 2589, "count": 5})
        self.assertFalse(info["auto_finish"])


class ParseQuestgiverOfferRewardTest(unittest.TestCase):
    def test_offer_reward(self):
        payload = (
            struct.pack('<Q', 0x1234) + struct.pack('<I', 100)
            + cstring("Quest A") + cstring("Well done, take this.")
            + struct.pack('<I', 500) + struct.pack('<I', 1000)
            + req_pairs([(3740, 1), (0, 0), (0, 0), (0, 0)])
            + req_pairs([(3741, 1), (3742, 1), (0, 0), (0, 0)])
        )
        info = qu.parse_questgiver_offer_reward(payload)
        self.assertEqual(info["reward_items"][0], {"entry": 3740, "count": 1})
        self.assertEqual(len(info["reward_choice_items"]), 4)
        self.assertEqual(info["reward_choice_items"][1], {"entry": 3742, "count": 1})


class ParseQuestgiverQuestCompleteTest(unittest.TestCase):
    def test_quest_complete(self):
        payload = struct.pack('<III', 100, 1000, 500)
        info = qu.parse_questgiver_quest_complete(payload)
        self.assertEqual(info, {"quest_id": 100, "xp_reward": 1000, "money_reward": 500})


class ParseQuestUpdateEventsTest(unittest.TestCase):
    def test_add_kill(self):
        payload = struct.pack('<IiII', 100, 1234, 3, 8) + struct.pack('<Q', 0xF130000000005678)
        info = qu.parse_questupdate_add_kill(payload)
        self.assertEqual(info, {"quest_id": 100, "entry": 1234, "count": 3,
                                 "required": 8, "victim_guid": 0xF130000000005678})

    def test_add_item(self):
        payload = struct.pack('<II', 2589, 3)
        self.assertEqual(qu.parse_questupdate_add_item(payload), {"item_entry": 2589, "count": 3})

    def test_complete(self):
        payload = struct.pack('<I', 100)
        self.assertEqual(qu.parse_questupdate_complete(payload), {"quest_id": 100})


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
        unit = next(u for u in snap["nearby_units"] if u["guid"] == 1)
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

    def test_quest_log_enriched_once_text_is_cached(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.update_object(create_block(1, fields=self._log_fields(0, 2158, counters=(3, 0, 0, 0))))
        ws.apply_quest_query_response({
            "quest_id": 2158, "title": "Rest and Relaxation", "objectives": "Kill mana wyrms",
            "required_credit": [{"entry": 1547, "count": 8}], "required_items": [],
        })
        log = ws.build_quest_log()
        self.assertEqual(log[0]["title"], "Rest and Relaxation")
        self.assertEqual(log[0]["objectives"][0]["text"], "1547: 3/8")
        snap = ws.snapshot(my_position=(0, 0.0, 0.0, 0.0, 0.0))
        self.assertEqual(snap["quest_log"][0]["title"], "Rest and Relaxation")


# ── Actions (agent.actions) ────────────────────────────────────────────────

def fake_session(player_position=(0, 0.0, 0.0, 0.0, 0.0)):
    sent = []
    sess = SimpleNamespace(player_position=player_position, events=[])
    sess._send_packet = lambda opcode, payload=b'': sent.append((opcode, payload))
    sess._sent = sent
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
