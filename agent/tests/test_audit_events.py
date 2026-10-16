"""ADR 0020 slices 1, 2 and 5 (#390): the level-up, the quest turn-in, the kill and the death as activity
events on the decision audit line."""
import collections
import glob
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from agent import think
from agent.audit import AuditLogger
from agent.handlers import death as death_handlers
from agent.handlers import loot as loot_handlers
from agent.handlers import quests as quest_handlers
from agent.handlers import spells as spell_handlers
from agent.runloop import run_think_loop
from agent.state import ACTIVITY_MAXLEN, GameState
from agent.tests.builders import FakeSession, quest_complete_payload
from agent.tests.test_think import FakeLLMClient
from agent.perception import WorldState


def _levelup_payload(level):
    return (struct.pack('<II', level, 20)
            + struct.pack('<7I', *range(7))
            + struct.pack('<5I', *range(5)))


ME = 0x42                                      # a player guid: high 16 bits 0x0000
OTHER_PLAYER = 0x43
CREATURE = 0xF130 << 48 | 1234 << 24 | 0x99    # a Unit guid, entry 1234 in bits 24..47 (ObjectGuid.h, 92127cd)
KILLER_GUESS = 0xF130 << 48 | 77 << 24 | 0x5   # a creature, guessed as the killer of a death


def _party_kill(killer, victim):
    return struct.pack('<QQ', killer, victim)


def _me(health):
    return SimpleNamespace(health=health, is_ghost=lambda: False)


def _item_push_payload(guid, entry, count):
    """SMSG_ITEM_PUSH_RESULT (Player::SendNewItem, Player.cpp:13855): player guid, received, created,
    send_chat, bag, slot, entry, suffix, random property, count, count in inventory."""
    return (struct.pack('<Q', guid) + struct.pack('<III', 1, 0, 1) + bytes([0])
            + struct.pack('<I', 23) + struct.pack('<I', entry) + struct.pack('<I', 0)
            + struct.pack('<i', -1) + struct.pack('<II', count, count))


def _loot_money_payload(copper):
    """SMSG_LOOT_MONEY_NOTIFY (LootHandler.cpp:188-202): uint32 money, uint8 sole looter."""
    return struct.pack('<I', copper) + bytes([1])


def _record(audit, events=None):
    return audit.record(cycle=1, snapshot={}, tool_call={"name": "idle", "args": {}}, valid=True,
                        result={"ok": True, "error": None}, events=events, ts=1731000000.0)


def _only_line(base):
    (path,) = glob.glob(os.path.join(base, "Jev1", "*.jsonl"))
    with open(path, encoding="utf-8") as f:
        (line,) = f.readlines()
    return json.loads(line)


class ActivityOffTests(unittest.TestCase):
    def test_off_by_default_nothing_is_buffered(self):
        state = GameState()
        self.assertFalse(state.activity_events)
        state.emit_activity("level", to=5)
        self.assertEqual(state.drain_activity(), [])

    def test_off_quest_turn_in_buffers_nothing(self):
        ctx = SimpleNamespace(state=GameState())
        quest_handlers.handle_questgiver_quest_complete(ctx, quest_complete_payload(8325, 45, 30))
        self.assertEqual(ctx.state.drain_activity(), [])

    def test_off_kill_and_death_buffer_nothing(self):
        state = GameState()
        state.player_guid = ME
        ctx = SimpleNamespace(state=state)
        spell_handlers.handle_party_kill_log(ctx, _party_kill(ME, CREATURE))
        death_handlers.check_death_transition(ctx, _me(100))
        death_handlers.check_death_transition(ctx, _me(0))
        self.assertEqual(state.drain_activity(), [])

    def test_off_records_no_events_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditLogger("Jev1", base_dir=tmp)
            _record(audit, events=GameState().drain_activity())
            self.assertNotIn("events", _only_line(tmp))


class ActivityOnTests(unittest.TestCase):
    def setUp(self):
        self.state = GameState()
        self.state.activity_events = True

    def test_levelup_info_makes_one_level_event_on_the_next_line(self):
        ctx = SimpleNamespace(state=self.state)
        before = time.time()
        spell_handlers.handle_levelup_info(ctx, _levelup_payload(5))
        events = self.state.drain_activity()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "level")
        self.assertEqual(events[0]["detail"], {"to": 5})
        self.assertLess(abs(events[0]["t"] - before), 60)  # wall clock, not time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            _record(AuditLogger("Jev1", base_dir=tmp), events=events)
            line = _only_line(tmp)
        self.assertEqual(line["events"], [{"kind": "level", "t": events[0]["t"], "detail": {"to": 5}}])

    def test_turn_in_makes_one_quest_event_on_the_next_line(self):
        ctx = SimpleNamespace(state=self.state)
        before = time.time()
        quest_handlers.handle_questgiver_quest_complete(ctx, quest_complete_payload(8325, 45, 30))
        events = self.state.drain_activity()
        # The quest, then its money reward (ADR 0020 slice 4): 30 copper.
        self.assertEqual([(e["kind"], e["detail"]) for e in events],
                         [("quest", {"quest_id": 8325}), ("money", {"copper": 30})])
        self.assertLess(abs(events[0]["t"] - before), 60)  # wall clock, as the level event
        self.assertEqual([e["kind"] for e in self.state.events], ["quest_turned_in"])  # the handler's own work
        with tempfile.TemporaryDirectory() as tmp:
            _record(AuditLogger("Jev1", base_dir=tmp), events=events)
            line = _only_line(tmp)
        self.assertEqual(line["events"], [{"kind": "quest", "t": events[0]["t"], "detail": {"quest_id": 8325}},
                                          {"kind": "money", "t": events[1]["t"], "detail": {"copper": 30}}])

    def test_turn_in_with_no_money_reward_makes_no_money_event(self):
        ctx = SimpleNamespace(state=self.state)
        quest_handlers.handle_questgiver_quest_complete(ctx, quest_complete_payload(8325, 45, 0))
        self.assertEqual([e["kind"] for e in self.state.drain_activity()], ["quest"])

    def test_a_quest_that_costs_money_makes_no_money_event(self):
        # SendQuestReward writes GetRewOrReqMoney as a uint32 (Player.cpp:16549), and that value is
        # negative for a quest that takes money: -100 arrives as 0xFFFFFF9C. It is a charge, not a reward.
        ctx = SimpleNamespace(state=self.state)
        quest_handlers.handle_questgiver_quest_complete(ctx, quest_complete_payload(8325, 45, 0xFFFFFF9C))
        self.assertEqual([e["kind"] for e in self.state.drain_activity()], ["quest"])

    def test_own_item_push_makes_one_item_event(self):
        self.state.player_guid = 7
        ctx = SimpleNamespace(state=self.state)
        loot_handlers.handle_item_push_result(ctx, _item_push_payload(7, 2589, 2))
        events = self.state.drain_activity()
        self.assertEqual([(e["kind"], e["detail"]) for e in events], [("item", {"item_entry": 2589, "count": 2})])
        self.assertEqual([e["kind"] for e in self.state.events], ["item_received"])  # the handler's own work

    def test_item_pushed_to_another_player_makes_no_event(self):
        # SendNewItem broadcasts to the whole group with the looter's guid (Player.cpp:13875).
        self.state.player_guid = 7
        ctx = SimpleNamespace(state=self.state)
        loot_handlers.handle_item_push_result(ctx, _item_push_payload(8, 2589, 2))
        self.assertEqual(self.state.drain_activity(), [])
        self.assertEqual([e["kind"] for e in self.state.events], ["item_received"])

    def test_loot_money_makes_one_money_event(self):
        ctx = SimpleNamespace(state=self.state)
        loot_handlers.handle_loot_money_notify(ctx, _loot_money_payload(1234))
        events = self.state.drain_activity()
        self.assertEqual([(e["kind"], e["detail"]) for e in events], [("money", {"copper": 1234})])
        self.assertEqual([e["kind"] for e in self.state.events], ["loot_money"])

    def test_zero_loot_money_makes_no_event(self):
        # A group split of 1 copper among 3 members is 0 for each (LootHandler.cpp:181).
        ctx = SimpleNamespace(state=self.state)
        loot_handlers.handle_loot_money_notify(ctx, _loot_money_payload(0))
        self.assertEqual(self.state.drain_activity(), [])

    def test_own_kill_makes_one_kill_event_with_the_victim_entry(self):
        self.state.player_guid = ME
        ctx = SimpleNamespace(state=self.state)
        before = time.time()
        spell_handlers.handle_party_kill_log(ctx, _party_kill(ME, CREATURE))
        events = self.state.drain_activity()
        detail = {"victim_guid": CREATURE, "victim_entry": 1234}
        self.assertEqual([(e["kind"], e["detail"]) for e in events], [("kill", detail)])
        self.assertLess(abs(events[0]["t"] - before), 60)  # wall clock
        self.assertEqual([e["kind"] for e in self.state.events], ["party_kill"])  # the handler's own work
        with tempfile.TemporaryDirectory() as tmp:
            _record(AuditLogger("Jev1", base_dir=tmp), events=events)
            line = _only_line(tmp)
        self.assertEqual(line["events"], [{"kind": "kill", "t": events[0]["t"], "detail": detail}])

    def test_a_party_members_kill_is_not_this_agents_kill(self):
        # SMSG_PARTYKILLLOG reaches the whole group (Unit::Kill, Unit.cpp:10968). Its first guid is the
        # tagger, the loot recipient: only a kill tagged by this character is its credit.
        self.state.player_guid = ME
        spell_handlers.handle_party_kill_log(SimpleNamespace(state=self.state), _party_kill(OTHER_PLAYER, CREATURE))
        self.assertEqual(self.state.drain_activity(), [])

    def test_a_kill_of_a_non_creature_carries_the_victim_guid(self):
        self.state.player_guid = ME
        spell_handlers.handle_party_kill_log(SimpleNamespace(state=self.state), _party_kill(ME, OTHER_PLAYER))
        self.assertEqual([(e["kind"], e["detail"]) for e in self.state.drain_activity()],
                         [("kill", {"victim_guid": OTHER_PLAYER})])

    def test_two_kills_of_the_same_entry_are_both_buffered(self):
        self.state.player_guid = ME
        ctx = SimpleNamespace(state=self.state)
        spell_handlers.handle_party_kill_log(ctx, _party_kill(ME, CREATURE))
        spell_handlers.handle_party_kill_log(ctx, _party_kill(ME, CREATURE))
        self.assertEqual([e["detail"] for e in self.state.drain_activity()],
                         [{"victim_guid": CREATURE, "victim_entry": 1234}] * 2)

    def test_death_makes_one_death_event_with_the_guessed_killer(self):
        self.state.player_guid = ME
        ctx = SimpleNamespace(state=self.state)
        death_handlers.check_death_transition(ctx, _me(100))
        self.state.record_event("attacker_state_update", attacker_guid=KILLER_GUESS, victim_guid=ME)
        death_handlers.check_death_transition(ctx, _me(0))
        self.assertEqual([(e["kind"], e["detail"]) for e in self.state.drain_activity()],
                         [("death", {"killer_guess": KILLER_GUESS})])

    def test_death_with_no_known_killer_guesses_none(self):
        self.state.player_guid = ME
        ctx = SimpleNamespace(state=self.state)
        death_handlers.check_death_transition(ctx, _me(100))
        death_handlers.check_death_transition(ctx, _me(0))
        self.assertEqual([e["detail"] for e in self.state.drain_activity()], [{"killer_guess": None}])

    def test_kill_death_and_level_drain_in_order(self):
        self.state.player_guid = ME
        ctx = SimpleNamespace(state=self.state)
        spell_handlers.handle_party_kill_log(ctx, _party_kill(ME, CREATURE))
        death_handlers.check_death_transition(ctx, _me(100))
        death_handlers.check_death_transition(ctx, _me(0))
        spell_handlers.handle_levelup_info(ctx, _levelup_payload(5))
        self.assertEqual([e["kind"] for e in self.state.drain_activity()], ["kill", "death", "level"])

    def test_think_cycle_writes_the_buffered_event_on_its_line(self):
        sess = FakeSession(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.activity_events = True
        sess.emit_activity("level", to=6)
        with tempfile.TemporaryDirectory() as tmp:
            think.think_and_act(sess, WorldState(), FakeLLMClient("idle", {}),
                                audit_logger=AuditLogger("Jev1", base_dir=tmp))
            line = _only_line(tmp)
        self.assertEqual([(e["kind"], e["detail"]) for e in line["events"]], [("level", {"to": 6})])
        self.assertEqual(sess.drain_activity(), [])  # drained by the cycle

    def test_the_21st_event_drops_the_oldest(self):
        self.assertEqual(ACTIVITY_MAXLEN, 20)
        for to in range(1, 22):
            self.state.emit_activity("level", to=to)
        self.assertEqual([e["detail"]["to"] for e in self.state.drain_activity()], list(range(2, 22)))

    def test_the_21st_event_drops_the_oldest_across_kinds(self):
        for n in range(1, 22):
            if n % 2:
                self.state.emit_activity("level", to=n)
            else:
                self.state.emit_activity("quest", quest_id=n)
        drained = self.state.drain_activity()
        self.assertEqual([(e["kind"], list(e["detail"].values())[0]) for e in drained],
                         [("quest" if n % 2 == 0 else "level", n) for n in range(2, 22)])

    def test_drain_leaves_the_buffer_empty(self):
        self.state.emit_activity("level", to=2)
        self.assertEqual(len(self.state.drain_activity()), 1)
        self.assertEqual(self.state.drain_activity(), [])


class CleanStopTests(unittest.TestCase):
    """ADR 0020 slice 5, decided: a clean stop writes no final line for what is still buffered."""

    def test_a_clean_stop_writes_no_line_for_buffered_events(self):
        sess = FakeSession(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.activity_events = True
        sess.emit_activity("death", killer_guess=None)
        stop = threading.Event()
        stop.set()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(run_think_loop(sess, None, None, time.monotonic(),
                                            audit_logger=AuditLogger("Jev1", base_dir=tmp), stop=stop))
            self.assertEqual(glob.glob(os.path.join(tmp, "Jev1", "*.jsonl")), [])
        self.assertEqual([e["kind"] for e in sess.drain_activity()], ["death"])  # never written, never drained


class ConcurrentDrainTests(unittest.TestCase):
    """The receive thread appends while the think thread drains. Nothing may be lost or duplicated."""

    def _union_of_drains(self, emit, total=5000):
        state = GameState()
        state.activity_events = True
        state._activity = collections.deque()  # unbounded: this test is about the drain, not the bound
        # Switch threads often, so a drain that copies and then clears is interrupted by an append.
        self.addCleanup(sys.setswitchinterval, sys.getswitchinterval())
        sys.setswitchinterval(1e-6)
        drained, done = [], threading.Event()

        def appender():
            for n in range(1, total + 1):
                emit(state, n)
            done.set()

        def drainer():
            while True:
                drained.extend(state.drain_activity())
                if done.is_set():
                    drained.extend(state.drain_activity())
                    return

        threads = [threading.Thread(target=appender), threading.Thread(target=drainer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        return drained

    def test_union_of_drains_is_every_event_in_order(self):
        drained = self._union_of_drains(lambda state, n: state.emit_activity("level", to=n))
        self.assertEqual([e["detail"]["to"] for e in drained], list(range(1, 5001)))

    def test_union_of_quest_drains_is_every_event_in_order(self):
        drained = self._union_of_drains(lambda state, n: state.emit_activity("quest", quest_id=n))
        self.assertEqual([e["detail"]["quest_id"] for e in drained], list(range(1, 5001)))


if __name__ == "__main__":
    unittest.main()
