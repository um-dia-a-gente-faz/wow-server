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


class _NamesDb:
    """Stands in for the connect() of DbPoller: a name query (the table in its SQL) answers its rows,
    any other query answers nothing."""

    def __init__(self, names):
        self.names = names

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql):
        self.rows = next((rows for table, rows in self.names.items() if table in sql), [])

    def fetchall(self):
        return self.rows


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

    def test_item_and_money_lines_become_agent_events(self):
        rec = {"events": [{"kind": "item", "t": 1.0, "detail": {"item_entry": 2589, "count": 2}},
                          {"kind": "money", "t": 2.0, "detail": {"copper": 1234}}]}
        item, money = activity.agent_events(rec, "Jev1", item_title=lambda e: "Linen Cloth")
        self.assertEqual((item["text"], item["source"], item["uid"]),
                         ("Got Linen Cloth ×2", "agent", "agent:Jev1:item:1.000:2589"))
        self.assertEqual(activity.agent_events(rec, "Jev1")[0]["text"], "Got Item 2589 ×2")
        self.assertEqual((money["text"], money["source"], money["uid"]),
                         ("Money +12s 34c", "agent", "agent:Jev1:money:2.000:1234"))
        self.assertEqual(activity.fact_key(item), ("jev1", "item", 2589))
        self.assertEqual(activity.fact_key(money), ("jev1", "money", 1234))
        # The DB poller's rows of the same facts: its item detail and its money delta.
        self.assertEqual(activity.fact_key(activity.event("Jev1", 1.5, "item", "Got Linen Cloth ×2", "db", "u",
                                                          {"item": 2589, "count": 2})), ("jev1", "item", 2589))
        self.assertEqual(activity.fact_key(activity.event("Jev1", 1.5, "money", "Money +12s 34c", "db", "u",
                                                          {"delta": 1234, "total": 9})), ("jev1", "money", 1234))

    def test_bad_item_and_money_elements_are_skipped(self):
        bad = [{"kind": "item", "t": 1.0, "detail": {"item_entry": 2589}},                # no count
               {"kind": "item", "t": 1.0, "detail": {"item_entry": "2589", "count": 1}},  # a string
               {"kind": "money", "t": 1.0, "detail": {"copper": True}},                   # a bool
               {"kind": "money", "t": 1.0, "detail": {"delta": 5}}]                       # the db key
        self.assertEqual(activity.agent_events({"events": bad}, "Jev1"), [])

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

    def agent_item(self, entry, count=1, t=10.0):
        return activity.agent_events({"events": [{"kind": "item", "t": t,
                                                  "detail": {"item_entry": entry, "count": count}}]}, "Jev1")

    def agent_money(self, copper, t=10.0):
        return activity.agent_events({"events": [{"kind": "money", "t": t, "detail": {"copper": copper}}]}, "Jev1")

    def db_item(self, entry, t=10.5, count=1):
        return activity.event("Jev1", t, "item", f"Got Item {entry}", "db", f"db:1:{t:.3f}:item:+{entry}",
                              {"item": entry, "count": count})

    def db_money(self, delta, t=10.5):
        return activity.event("Jev1", t, "money", f"Money +{delta}c", "db", f"db:1:{t:.3f}:money:",
                              {"delta": delta, "total": 500})

    def test_agent_item_first_then_db_leaves_one_agent_row(self):
        self.store.add(self.agent_item(2589))
        self.assertEqual(self.store.add([self.db_item(2589)]), 0)
        self.assertEqual([(r["source"], r["kind"]) for r in self.rows()], [("agent", "item")])

    def test_db_item_first_then_agent_leaves_one_agent_row(self):
        self.store.add([self.db_item(2589)])
        self.assertEqual(self.store.add(self.agent_item(2589)), 1)
        self.assertEqual([(r["source"], r["kind"]) for r in self.rows()], [("agent", "item")])

    def test_agent_money_first_then_db_leaves_one_agent_row(self):
        self.store.add(self.agent_money(50))
        self.assertEqual(self.store.add([self.db_money(50)]), 0)
        self.assertEqual([(r["source"], r["kind"]) for r in self.rows()], [("agent", "money")])

    def test_db_money_first_then_agent_leaves_one_agent_row(self):
        self.store.add([self.db_money(50)])
        self.assertEqual(self.store.add(self.agent_money(50)), 1)
        self.assertEqual([(r["source"], r["kind"]) for r in self.rows()], [("agent", "money")])

    def test_an_item_and_money_with_the_same_number_do_not_delete_each_other(self):
        # The item 50 and the 50 copper are different facts: each statement names its kind.
        self.store.add(self.agent_money(50))
        self.assertEqual(self.store.add([self.db_item(50)]), 1)
        self.assertEqual(sorted((r["source"], r["kind"]) for r in self.rows()), [("agent", "money"), ("db", "item")])
        other = activity.ActivityStore(":memory:")
        other.add([self.db_money(50)])
        self.assertEqual(other.add(self.agent_item(50)), 1)
        self.assertEqual(sorted((r["source"], r["kind"]) for r in other.recent("Jev1", 50)),
                         [("agent", "item"), ("db", "money")])

    def test_a_db_item_a_week_away_from_the_agent_pickup_survives(self):
        self.store.add([self.db_item(2589, t=10.5)])
        self.assertEqual(self.store.add(self.agent_item(2589, t=10.5 + 7 * 86400)), 1)
        self.assertEqual(sorted((r["source"], r["kind"]) for r in self.rows()), [("agent", "item"), ("db", "item")])

    def test_the_store_writes_the_fact_key_of_item_and_money_rows_as_text(self):
        self.store.add(self.agent_item(2589, count=3))
        self.store.add([self.db_money(-50)])
        rows = self.store._conn.execute("SELECT kind, source, fact_key FROM events").fetchall()
        self.assertEqual(sorted(rows), [("item", "agent", "2589"), ("money", "db", "-50")])

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

    def test_an_idle_line_with_item_and_money_events_stores_both_and_no_audit_row(self):
        idle = {"schema": 3, "ts": 1731000021.0, "agent": "Jev1", "cycle": 31, "valid": True,
                "tool_call": {"name": "idle", "args": {}}, "result": {"ok": True, "error": None},
                "events": [{"kind": "item", "t": 1731000020.5, "detail": {"item_entry": 2589, "count": 2}},
                           {"kind": "money", "t": 1731000020.6, "detail": {"copper": 1234}}]}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(idle) + "\n")
        tailer = activity.AuditTailer(self.store, self.base)
        tailer.scan_once()
        rows = self.store.recent("Jev1", 50)
        self.assertEqual(sorted((r["source"], r["kind"]) for r in rows),
                         [("agent", "item"), ("agent", "level"), ("agent", "money"), ("audit", "loot")])
        self.assertEqual(self.store.count("Jev1"), 4)
        activity.AuditTailer(self.store, self.base).scan_once()  # a rebuilt tailer reads it again
        self.assertEqual(self.store.count("Jev1"), 4)

    def test_activity_names_a_turn_in_from_the_db_before_the_poll_saw_it(self):
        # Through Activity, as the app wires it: the DB answers the quest-name query only.
        with open(V3_QUEST, encoding="utf-8") as f:
            line = f.readline()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
        act = activity.Activity(self.store, connect=_NamesDb({"quest_template": [(8325, "Defias Pillager")]}),
                                audit_dir=self.base)
        act.audit.scan_once()
        quests = [r for r in self.store.recent("Jev1", 50) if r["kind"] == "quest"]
        self.assertEqual([(r["source"], r["text"]) for r in quests],
                         [("agent", "Turned in quest: Defias Pillager")])

    def test_activity_names_an_item_pickup_from_the_db_before_the_poll_saw_it(self):
        line = {"schema": 3, "ts": 1731000021.0, "agent": "Jev1", "cycle": 32, "valid": True,
                "tool_call": {"name": "idle", "args": {}}, "result": {"ok": True, "error": None},
                "events": [{"kind": "item", "t": 1731000020.5, "detail": {"item_entry": 2589, "count": 2}}]}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        act = activity.Activity(self.store, connect=_NamesDb({"item_template": [(2589, "Linen Cloth", 5)]}),
                                audit_dir=self.base)
        act.audit.scan_once()
        items = [r for r in self.store.recent("Jev1", 50) if r["kind"] == "item"]
        self.assertEqual([(r["source"], r["text"]) for r in items], [("agent", "Got Linen Cloth ×2")])

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
