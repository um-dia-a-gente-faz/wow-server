"""ADR 0020 slices 1 and 2 (#390): wowmap ingests the agent's `level` and `quest` activity events from
the audit line, and an agent row replaces the database row for the same fact."""
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

# #670: point CALIBRATION_FILE at a temp file before any wowmap import
import _calibration_env  # noqa: F401
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "wowmap"))
import activity  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parents[3] / "agent" / "tests" / "fixtures" / "audit"
V3 = FIXTURES / "v3.jsonl"
V3_QUEST = FIXTURES / "v3-quest.jsonl"
V3_KILL = FIXTURES / "v3-kill.jsonl"
V3_DEATH = FIXTURES / "v3-death.jsonl"


def _v3_record():
    with open(V3, encoding="utf-8") as f:
        return json.loads(f.readline())


def _level_event(character, t, to):
    return {"kind": "level", "t": t, "detail": {"to": to}}


def _quest_event(character, t, quest_id):
    return {"kind": "quest", "t": t, "detail": {"quest_id": quest_id}}


def _db_level(character, t, to):
    return activity.event(character, t, "level", f"Reached level {to}", "db",
                          f"db:{character}:{t:.3f}:level:{to}", {"from": to - 1, "to": to})


def _db_quest(character, t, quest):
    return activity.event(character, t, "quest", f"Turned in quest: Quest {quest}", "db",
                          f"db:{character}:{t:.3f}:quest:{quest}", {"quest": quest})


class AgentEventsTests(unittest.TestCase):
    def test_v3_line_becomes_one_agent_level_event(self):
        (e,) = activity.agent_events(_v3_record(), "Jev1")
        self.assertEqual(e["character"], "Jev1")
        self.assertEqual(e["kind"], "level")
        self.assertEqual(e["text"], "Reached level 11")
        self.assertEqual(e["source"], "agent")
        self.assertFalse(e["inferred"])
        self.assertEqual(e["t"], 1731000009.5)
        self.assertEqual(e["uid"], "agent:Jev1:level:1731000009.500:11")
        self.assertEqual(e["detail"], {"to": 11})

    def test_quest_line_becomes_one_agent_quest_event(self):
        with open(V3_QUEST, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        (e,) = activity.agent_events(rec, "Jev1", lambda q: f"Quest {q}")
        self.assertEqual((e["kind"], e["source"], e["inferred"]), ("quest", "agent", False))
        self.assertEqual(e["text"], "Turned in quest: Quest 8325")
        self.assertEqual(e["uid"], "agent:Jev1:quest:1731000011.750:8325")
        self.assertEqual(e["detail"], {"quest_id": 8325})
        (titled,) = activity.agent_events(rec, "Jev1", lambda q: "Defias Pillager" if q == 8325 else "?")
        self.assertEqual(titled["text"], "Turned in quest: Defias Pillager")

    def test_v3_kill_line_becomes_one_agent_kill_event_with_no_fact(self):
        with open(V3_KILL, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        (e,) = activity.agent_events(rec, "Jev1")
        self.assertEqual((e["kind"], e["source"], e["inferred"]), ("kill", "agent", False))
        self.assertEqual(e["text"], "Killed creature 1234")
        self.assertEqual(e["uid"], "agent:Jev1:kill:1731000013.250:17379390982725828761")
        self.assertEqual(e["detail"], {"victim_guid": 17379390982725828761, "victim_entry": 1234})
        self.assertIsNone(activity.fact_key(e))  # no de-duplication (ADR 0020 slice 5)

    def test_v3_death_line_becomes_one_agent_death_event_with_no_fact(self):
        with open(V3_DEATH, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        (e,) = activity.agent_events(rec, "Jev1")
        self.assertEqual((e["kind"], e["source"], e["inferred"]), ("death", "agent", False))
        self.assertEqual(e["text"], "Died")
        self.assertEqual(e["uid"], "agent:Jev1:death:1731000015.500:17379390963314589701")
        self.assertEqual(e["detail"], {"killer_guess": 17379390963314589701})
        self.assertIsNone(activity.fact_key(e))

    def test_kill_by_guid_and_death_without_a_killer_guess(self):
        rec = {"events": [{"kind": "kill", "t": 1.0, "detail": {"victim_guid": 9}},
                          {"kind": "death", "t": 2.0, "detail": {"killer_guess": None}}]}
        out = activity.agent_events(rec, "Jev1")
        self.assertEqual([(e["text"], e["uid"]) for e in out],
                         [("Killed an unknown target", "agent:Jev1:kill:1.000:9"),
                          ("Died", "agent:Jev1:death:2.000:None")])

    def test_bad_kill_and_death_details_are_skipped(self):
        bad = [
            {"kind": "kill", "t": 1.0, "detail": {}},                             # no victim
            {"kind": "kill", "t": 1.0, "detail": {"victim_entry": 1234}},         # no guid: no uid
            {"kind": "kill", "t": 1.0, "detail": {"victim_guid": "9"}},           # string guid
            {"kind": "kill", "t": 1.0, "detail": {"victim_entry": True}},         # bool
            {"kind": "death", "t": 1.0, "detail": {"killer_guess": "17"}},        # string
            {"kind": "death", "t": 1.0, "detail": {"killer_guess": 1.5}},         # float
        ]
        self.assertEqual(activity.agent_events({"events": bad}, "Jev1"), [])

    def test_bad_elements_are_skipped_and_the_rest_kept(self):
        bad = [
            {"kind": "loot_from_the_future", "t": 1.0, "detail": {"to": 3}},  # unknown kind
            {"kind": "level", "t": 1.0, "detail": {"to": "3"}},               # string
            {"kind": "level", "t": 1.0, "detail": {"to": True}},              # bool is not an int
            {"kind": "level", "t": 1.0},                                      # no detail
            {"kind": ["level"], "t": 1.0, "detail": {"to": 3}},               # unhashable kind
            {"kind": "level", "t": "now", "detail": {"to": 3}},               # no timestamp
            {"kind": "quest", "t": 1.0, "detail": {"quest_id": "8325"}},      # string
            {"kind": "quest", "t": 1.0, "detail": {"quest_id": False}},       # bool is not an int
            {"kind": "quest", "t": 1.0, "detail": {"to": 8325}},              # the level key
            "level",                                                          # not a dict
        ]
        rec = {"events": bad + [_level_event("Jev1", 2.0, 4), _quest_event("Jev1", 3.0, 8325)]}
        out = activity.agent_events(rec, "Jev1")
        self.assertEqual([(e["kind"], e["text"]) for e in out],
                         [("level", "Reached level 4"), ("quest", "Turned in quest: Quest 8325")])

    def test_events_that_are_not_a_list_give_nothing(self):
        for value in ("level", {"kind": "level"}, 7, None):
            with self.subTest(value=value):
                self.assertEqual(activity.agent_events({"events": value}, "Jev1"), [])
        self.assertEqual(activity.agent_events({}, "Jev1"), [])

    def test_fact_key_is_the_kind_and_its_own_key(self):
        (e,) = activity.agent_events(_v3_record(), "Jev1")
        self.assertEqual(activity.fact_key(e), ("jev1", "level", 11))
        self.assertEqual(activity.fact_key(_db_level("Jev1", 1.0, 11)), ("jev1", "level", 11))
        # The DB poller names a turn-in's key `quest`, the agent names it `quest_id` (feed_events.FACT_KEYS).
        (q,) = activity.agent_events({"events": [_quest_event("Jev1", 1.0, 8325)]}, "Jev1")
        self.assertEqual(activity.fact_key(q), ("jev1", "quest", 8325))
        self.assertEqual(activity.fact_key(_db_quest("Jev1", 1.0, 8325)), ("jev1", "quest", 8325))
        self.assertIsNone(activity.fact_key(activity.event("Jev1", 1.0, "money", "x", "db", "u")))
        # A hand-in is a `quest` row with no quest key: no fact.
        hand_in = activity.event("Jev1", 1.0, "quest", "Handed in Linen Cloth", "db", "u",
                                 {"item": 2589, "count": 1}, inferred=True)
        self.assertIsNone(activity.fact_key(hand_in))


class StoreRulesTests(unittest.TestCase):
    def setUp(self):
        self.store = activity.ActivityStore(":memory:")

    def rows(self, character="Jev1"):
        return self.store.recent(character, 50)

    def agent_level(self, to, t=10.0):
        return activity.agent_events({"events": [_level_event("Jev1", t, to)]}, "Jev1")

    def test_agent_first_then_db_leaves_one_agent_row(self):
        self.store.add(self.agent_level(11))
        self.assertEqual(self.store.add([_db_level("Jev1", 10.5, 11)]), 0)
        self.assertEqual([(r["source"], r["text"]) for r in self.rows()], [("agent", "Reached level 11")])

    def test_db_first_then_agent_leaves_one_agent_row(self):
        self.store.add([_db_level("Jev1", 10.5, 11)])
        self.assertEqual(self.store.add(self.agent_level(11)), 1)
        self.assertEqual([r["source"] for r in self.rows()], ["agent"])

    def test_other_character_level_kind_and_item_are_untouched(self):
        self.store.add(self.agent_level(11))
        others = [_db_level("Thrall", 10.5, 11),                  # another character
                  _db_level("Jev1", 10.5, 12),                    # another level
                  activity.event("Jev1", 10.5, "item", "Got Linen Cloth", "db", "db:1:10.500:item:+1",
                                 {"item": 2589, "count": 1})]     # another kind
        self.assertEqual(self.store.add(others), 3)
        self.assertEqual(sorted((r["source"], r["kind"]) for r in self.rows()),
                         [("agent", "level"), ("db", "item"), ("db", "level")])
        self.assertEqual(len(self.rows("Thrall")), 1)

    def test_db_first_then_agent_deletes_only_that_level_of_that_character(self):
        # The db rows that must survive the agent's delete: another level and other kinds (one with
        # the same `to`) of the same character, and the same level of another character.
        self.store.add([_db_level("Thrall", 10.5, 11), _db_level("Jev1", 10.5, 12),
                        activity.event("Jev1", 10.5, "item", "Got Linen Cloth", "db", "db:1:10.500:item:+1",
                                       {"item": 2589, "count": 1}),
                        activity.event("Jev1", 10.5, "zone", "Entered Wetlands", "db", "db:1:10.500:zone:11",
                                       {"from": 1, "to": 11}, inferred=True)])
        self.assertEqual(self.store.add(self.agent_level(11)), 1)
        self.assertEqual(sorted((r["source"], r["kind"]) for r in self.rows()),
                         [("agent", "level"), ("db", "item"), ("db", "level"), ("db", "zone")])
        self.assertEqual([(r["source"], r["text"]) for r in self.rows("Thrall")], [("db", "Reached level 11")])

    def agent_quest(self, quest, t=10.0):
        return activity.agent_events({"events": [_quest_event("Jev1", t, quest)]}, "Jev1")

    def test_agent_quest_first_then_db_leaves_one_agent_row(self):
        self.store.add(self.agent_quest(8325))
        self.assertEqual(self.store.add([_db_quest("Jev1", 10.5, 8325)]), 0)
        self.assertEqual([(r["source"], r["kind"]) for r in self.rows()], [("agent", "quest")])

    def test_db_quest_first_then_agent_leaves_one_agent_row(self):
        self.store.add([_db_quest("Jev1", 10.5, 8325)])
        self.assertEqual(self.store.add(self.agent_quest(8325)), 1)
        self.assertEqual([(r["source"], r["kind"]) for r in self.rows()], [("agent", "quest")])

    def test_a_level_and_a_quest_with_the_same_number_do_not_delete_each_other(self):
        # Agent first: the DB quest 11 is not a level 11, so it is stored; the agent level 11 keeps it.
        self.store.add(self.agent_level(11))
        self.assertEqual(self.store.add([_db_quest("Jev1", 10.5, 11)]), 1)
        self.assertEqual(sorted((r["source"], r["kind"]) for r in self.rows()),
                         [("agent", "level"), ("db", "quest")])
        # DB first: the DB level 11 survives the agent quest 11, and the other way round.
        other = activity.ActivityStore(":memory:")
        other.add([_db_level("Jev1", 10.5, 11)])
        self.assertEqual(other.add(self.agent_quest(11)), 1)
        self.assertEqual(sorted((r["source"], r["kind"]) for r in other.recent("Jev1", 50)),
                         [("agent", "quest"), ("db", "level")])

    def test_agent_quest_deletes_only_that_quest_of_that_character(self):
        self.store.add([_db_quest("Thrall", 10.5, 8325), _db_quest("Jev1", 10.5, 8326),
                        activity.event("Jev1", 10.5, "item", "Got Linen Cloth", "db", "db:1:10.500:item:+1",
                                       {"item": 2589, "count": 1})])
        self.assertEqual(self.store.add(self.agent_quest(8325)), 1)
        self.assertEqual(sorted((r["source"], r["kind"]) for r in self.rows()),
                         [("agent", "quest"), ("db", "item"), ("db", "quest")])
        self.assertEqual([(r["source"], r["kind"]) for r in self.rows("Thrall")], [("db", "quest")])

    def test_a_db_quest_a_week_before_the_agent_turn_in_survives(self):
        # The same quest id turned in again a week later: a different fact, so the db row stays.
        self.store.add([_db_quest("Jev1", 10.5, 8325)])
        self.assertEqual(self.store.add(self.agent_quest(8325, t=10.5 + 7 * 86400)), 1)
        self.assertEqual(sorted((r["source"], r["kind"]) for r in self.rows()),
                         [("agent", "quest"), ("db", "quest")])

    def test_a_db_quest_a_week_after_the_agent_turn_in_is_stored(self):
        self.store.add(self.agent_quest(8325, t=10.5))
        self.assertEqual(self.store.add([_db_quest("Jev1", 10.5 + 7 * 86400, 8325)]), 1)
        self.assertEqual(sorted((r["source"], r["kind"]) for r in self.rows()),
                         [("agent", "quest"), ("db", "quest")])

    def test_the_store_writes_the_fact_key_of_every_row(self):
        # The column (ADR 0020 slice 3): the key value as text for a fact, NULL for a row with none.
        self.store.add(self.agent_level(11))
        self.store.add([_db_level("Thrall", 10.5, 11), _db_quest("Jev1", 10.5, 8325),
                        activity.event("Jev1", 10.5, "zone", "Entered Wetlands", "db", "db:1:10.500:zone:11",
                                       {"from": 1, "to": 11}, inferred=True)])
        rows = self.store._conn.execute("SELECT character, kind, source, fact_key FROM events").fetchall()
        self.assertEqual(sorted(rows, key=str), [("Jev1", "level", "agent", "11"), ("Jev1", "quest", "db", "8325"),
                                                 ("Jev1", "zone", "db", None), ("Thrall", "level", "db", "11")])

    def test_two_kills_of_the_same_entry_within_the_window_are_both_stored(self):
        # No de-duplication for kill (ADR 0020 slice 5): each kill is a fact of its own.
        kill = {"victim_entry": 1234, "victim_guid": 9}
        kills = activity.agent_events({"events": [{"kind": "kill", "t": 10.0, "detail": kill},
                                                  {"kind": "kill", "t": 10.5, "detail": {**kill, "victim_guid": 10}}]},
                                      "Jev1")
        self.assertEqual(self.store.add(kills), 2)
        self.assertEqual([(r["source"], r["text"]) for r in self.rows()],
                         [("agent", "Killed creature 1234")] * 2)

    def test_two_kills_of_the_same_entry_in_the_same_millisecond_are_both_stored(self):
        # Two kills at one t: the uid tells them apart by victim_guid, so INSERT OR IGNORE keeps both.
        kills = activity.agent_events({"events": [
            {"kind": "kill", "t": 10.0, "detail": {"victim_entry": 1234, "victim_guid": 9}},
            {"kind": "kill", "t": 10.0, "detail": {"victim_entry": 1234, "victim_guid": 10}}]}, "Jev1")
        self.assertEqual(len({e["uid"] for e in kills}), 2)
        self.assertEqual(self.store.add(kills), 2)
        self.assertEqual(len(self.rows()), 2)

    def test_a_kill_and_a_death_are_stored_beside_a_db_level(self):
        self.store.add([_db_level("Jev1", 10.5, 11)])
        death = activity.agent_events({"events": [{"kind": "death", "t": 10.5, "detail": {"killer_guess": None}}]},
                                      "Jev1")
        self.assertEqual(self.store.add(death), 1)
        self.assertEqual(sorted(r["kind"] for r in self.rows()), ["death", "level"])

    def test_a_db_level_for_another_name_case_is_the_same_character(self):
        self.store.add(self.agent_level(11))
        self.assertEqual(self.store.add([_db_level("jev1", 10.5, 11)]), 0)


class TailerTests(unittest.TestCase):
    """AuditTailer reads the audit files: the agent event lands once, with its decision event."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name
        os.makedirs(os.path.join(self.base, "Jev1"))
        # The tailer reads day files from yesterday on, so the file is named for today.
        self.path = os.path.join(self.base, "Jev1", time.strftime("%Y-%m-%d") + ".jsonl")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(V3.read_text(encoding="utf-8"))
        self.store = activity.ActivityStore(":memory:")

    def test_scan_stores_the_decision_and_the_agent_event(self):
        activity.AuditTailer(self.store, self.base).scan_once()
        kinds = sorted(r["kind"] for r in self.store.recent("Jev1", 50))
        self.assertEqual(kinds, ["level", "loot"])

    def test_reading_twice_and_rebuilding_the_tailer_adds_no_row(self):
        tailer = activity.AuditTailer(self.store, self.base)
        tailer.scan_once()
        self.assertEqual(tailer.scan_once(), [])
        self.assertEqual(activity.AuditTailer(self.store, self.base).scan_once(), [])
        self.assertEqual(self.store.count("Jev1"), 2)

    def test_an_idle_line_with_a_level_event_stores_the_agent_row_and_no_audit_row(self):
        # idle is no feed row (audit_event returns None), but its level-up is still the agent's.
        idle = {"schema": 3, "ts": 1731000020.0, "agent": "Jev1", "cycle": 30, "valid": True,
                "tool_call": {"name": "idle", "args": {}}, "result": {"ok": True, "error": None},
                "events": [{"kind": "level", "t": 1731000019.5, "detail": {"to": 12}}]}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(idle) + "\n")
        tailer = activity.AuditTailer(self.store, self.base)
        tailer.scan_once()
        rows = self.store.recent("Jev1", 50)
        self.assertEqual(sorted((r["source"], r["text"]) for r in rows),
                         [("agent", "Reached level 11"), ("agent", "Reached level 12"),
                          ("audit", "Loot guid=7")])
        self.assertEqual(self.store.count("Jev1"), 3)
        # A rebuilt tailer reads the idle line again (no audit row moves _since past it): nothing new.
        activity.AuditTailer(self.store, self.base).scan_once()
        self.assertEqual(self.store.count("Jev1"), 3)

    def test_a_quest_line_is_read_once_and_titled_by_the_db_names(self):
        with open(V3_QUEST, encoding="utf-8") as f:
            line = f.readline()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
        tailer = activity.AuditTailer(self.store, self.base)
        tailer.quest_title = lambda q: "Defias Pillager" if q == 8325 else f"Quest {q}"
        tailer.scan_once()
        quests = [r for r in self.store.recent("Jev1", 50) if r["kind"] == "quest"]
        self.assertEqual([(r["source"], r["text"]) for r in quests], [("agent", "Turned in quest: Defias Pillager")])
        self.assertEqual(tailer.scan_once(), [])
        # A rebuilt tailer reads the idle quest line again and adds nothing.
        activity.AuditTailer(self.store, self.base).scan_once()
        self.assertEqual(self.store.count("Jev1"), 3)

    def test_activity_names_a_turn_in_from_the_db_before_the_poll_saw_it(self):
        # Through Activity, as the app wires it: the fake DB answers the quest-name query only.
        class Cursor:
            def execute(self, sql):
                self.rows = [(8325, "Defias Pillager")] if "quest_template" in sql else []

            def fetchall(self):
                return self.rows

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Conn:
            def cursor(self):
                return Cursor()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with open(V3_QUEST, encoding="utf-8") as f:
            line = f.readline()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
        act = activity.Activity(self.store, connect=Conn, audit_dir=self.base)
        act.audit.scan_once()
        quests = [r for r in self.store.recent("Jev1", 50) if r["kind"] == "quest"]
        self.assertEqual([(r["source"], r["text"]) for r in quests],
                         [("agent", "Turned in quest: Defias Pillager")])

    def test_a_malformed_events_field_is_counted_and_the_decision_still_stored(self):
        with open(self.path, "a", encoding="utf-8") as f:
            rec = _v3_record()
            rec["cycle"], rec["ts"], rec["events"] = 23, 1731000011.0, "level"
            f.write(json.dumps(rec) + "\n")
        tailer = activity.AuditTailer(self.store, self.base)
        tailer.scan_once()
        self.assertEqual(tailer.skipped_agent_events, 1)
        self.assertEqual(self.store.count("Jev1"), 3)

    def test_status_reports_the_skipped_count(self):
        act = activity.Activity(self.store, audit_dir=self.base)
        act.audit.scan_once()
        self.assertEqual(act.status()["agent"], {"skipped": 0})


if __name__ == "__main__":
    unittest.main()
