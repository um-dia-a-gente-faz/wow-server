"""WorldState.snapshot() is taken whole under the lock (#701).

The recv thread applies update blocks while the think thread calls snapshot(). Before the
fix the lock was released before the sections were composed, so one unit could show a level
from one update and a faction from the next."""

import copy
import sys
import threading
import unittest

from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo

JOIN_S = 30.0
ME = 1
UNIT = 100
POS = (0, 0.0, 0.0, 0.0, 0.0)
BLOCKS = 40000
READS = 6000


def _create(guid, kind=uo.TYPEID_UNIT, x=1.0, fields=None):
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=kind,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": 0.0, "z": 0.0, "o": 0.0},
        fields=fields or {})


def _values(guid, k):
    return uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=guid, fields={
        uf.UNIT_FIELD_LEVEL: k, uf.UNIT_FIELD_FACTIONTEMPLATE: k})


def _world():
    ws = per.WorldState()
    ws.set_my_guid(ME)
    ws.set_my_map(0)
    ws.update_object(_create(ME, uo.TYPEID_PLAYER, 0.0))
    ws.update_object(_create(UNIT, fields={uf.UNIT_FIELD_LEVEL: 0, uf.UNIT_FIELD_FACTIONTEMPLATE: 0}))
    return ws


def _run_with_writer(write, check):
    """Run write(stop) and READS snapshots on two daemon threads. check(snapshot) returns True
    for a bad one. Returns (bad snapshot count, exceptions raised by snapshot()). Both threads
    are joined on every path; a reader that does not finish (a deadlock) fails the test by name."""
    stop = threading.Event()
    bad, errors = [], []

    def writer():
        try:
            write(stop)
        except Exception as e:  # noqa: BLE001 - reported by the test
            errors.append(("writer", e))

    def reader():
        for _ in range(READS):
            try:
                if check():
                    bad.append(1)
            except Exception as e:  # noqa: BLE001 - what the think thread would die of
                errors.append(("reader", e))

    w = threading.Thread(target=writer, name="race-writer", daemon=True)
    r = threading.Thread(target=reader, name="race-reader", daemon=True)
    w.start()
    r.start()
    r.join(JOIN_S)
    stop.set()
    w.join(JOIN_S)
    if r.is_alive():
        raise AssertionError(f"thread {r.name!r} (snapshot reader) did not finish in {JOIN_S}s: deadlock?")
    if w.is_alive():
        raise AssertionError(f"thread {w.name!r} did not stop in {JOIN_S}s")
    return len(bad), errors


def _bounded(fn, name):
    """fn() on a daemon thread joined with JOIN_S: a hang fails by name instead of freezing the suite."""
    box: list = []
    t = threading.Thread(target=lambda: box.append(fn()), name=name, daemon=True)
    t.start()
    t.join(JOIN_S)
    if t.is_alive():
        raise AssertionError(f"thread {name!r} did not finish in {JOIN_S}s: deadlock?")
    return box[0]


class SnapshotRaceTest(unittest.TestCase):
    def setUp(self):
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-5)
        self.addCleanup(sys.setswitchinterval, old)

    def test_snapshot_never_shows_one_unit_from_two_updates(self):
        ws = _world()

        def write(stop):
            for k in range(1, BLOCKS):
                if stop.is_set():
                    return
                ws.update_object(_values(UNIT, k))
                if k % 50 == 0:  # creations and removals around the updated unit
                    ws.update_object(_create(500 + k % 7, x=2.0))
                    ws.remove_guids([500 + (k + 3) % 7])

        def check():
            units = ws.snapshot(my_position=POS)["nearby_units"]
            return any("level" in u and u["level"] != u.get("faction") for u in units)

        self.assertEqual(_run_with_writer(write, check), (0, []))

    def test_window_dicts_are_not_walked_while_the_recv_thread_edits_them(self):
        """encode() inside the lock: trade and mailbox are edited in place (trade_state, windows)."""
        ws = _world()
        ws.trade = {"partner_guid": 7, "my_items": {}, "my_gold": 0, "last_activity_at": 0.0}
        ws.open_mailbox_request(9)

        def write(stop):
            k = 0
            while not stop.is_set() and k < BLOCKS:
                k += 1
                n = k % 5 + 1
                ws.apply_mail_list_result({"total_records": n, "mails": [
                    {"id": i, "attachments": []} for i in range(n)]})
                for slot in range(6):
                    ws.set_my_trade_item(slot, {"entry": k})
                for slot in range(6):
                    ws.clear_my_trade_item(slot)

        def check():
            mb = ws.snapshot(my_position=POS)["mailbox"]
            return mb["total_records"] != len(mb["mails"])

        self.assertEqual(_run_with_writer(write, check), (0, []))

    def test_equipment_and_quest_log_come_from_the_same_moment_as_the_objects(self):
        """The builders inside the lock: one VALUES block sets health and a quest state together."""
        ws = _world()
        ws.update_object(_create(ME, uo.TYPEID_PLAYER, 0.0, fields={
            uf.PLAYER_QUEST_LOG_1_1: 77, uf.PLAYER_QUEST_LOG_1_1 + 1: 1, uf.UNIT_FIELD_HEALTH: 1}))

        def write(stop):
            for k in range(1, BLOCKS):
                if stop.is_set():
                    return
                ws.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=ME, fields={
                    uf.UNIT_FIELD_HEALTH: k % 2, uf.PLAYER_QUEST_LOG_1_1 + 1: k % 2}))

        def check():
            snap = ws.snapshot(my_position=POS)
            return snap["is_dead"] != (snap["quest_log"][0]["state"] == 0)

        self.assertEqual(_run_with_writer(write, check), (0, []))

    def test_returned_snapshot_shares_nothing_with_live_state(self):
        ws = _world()
        ws.ui_state = {"kind": "vendor", "items": [{"entry": 1}]}
        ws.trade = {"partner_guid": 7, "my_items": {1: {"entry": 2}}, "my_gold": 0}
        ws.mailbox = {"mails": [{"id": 1, "subject": "a"}]}
        ws.update_object(_create(ME, uo.TYPEID_PLAYER, 0.0, fields={
            uf.PLAYER_QUEST_LOG_1_1: 77, uf.PLAYER_FIELD_PACK_SLOT_1: 5,
            uf.PLAYER_FIELD_INV_SLOT_HEAD: 6}))
        ws.update_object(_create(5, uo.TYPEID_ITEM, fields={uf.OBJECT_FIELD_ENTRY: 9}))
        ws.update_object(_create(6, uo.TYPEID_ITEM, fields={uf.OBJECT_FIELD_ENTRY: 8}))
        snap = _bounded(lambda: ws.snapshot(my_position=POS), "pin-snapshot")
        for key in ("window", "trade", "mailbox", "equipment", "inventory", "quest_log"):
            self.assertIsNotNone(snap[key] or None, key)  # the sections under test are populated
        frozen = copy.deepcopy(snap)

        with ws._lock:  # the recv thread mutating live state in place
            ws.ui_state["items"].append({"entry": 2})
            ws.trade["my_items"][2] = {"entry": 3}
            ws.trade["my_gold"] = 99
            ws.mailbox["mails"].append({"id": 2})
            ws.objects[UNIT].level = 55
            ws.objects[ME].raw_fields[uf.PLAYER_QUEST_LOG_1_1] = 78
            ws.objects[5].entry = 10
            ws.objects[6].entry = 11
        ws.update_object(_values(UNIT, 60))
        ws.remove_guids([UNIT])

        self.assertEqual(snap, frozen)


if __name__ == "__main__":
    unittest.main()
