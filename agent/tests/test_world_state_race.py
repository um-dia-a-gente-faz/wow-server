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


def _run_with_writer(write, read):
    """Run write() on a thread and read() READS times meanwhile; return read()'s results."""
    stop, started = threading.Event(), threading.Event()
    errors: list = []

    def body():
        started.set()
        try:
            write(stop)
        except Exception as e:  # noqa: BLE001 - reported by the test
            errors.append(e)

    t = threading.Thread(target=body)
    t.start()
    try:
        started.wait(JOIN_S)
        results = [read() for _ in range(READS)]
    finally:
        stop.set()
        t.join(JOIN_S)
    assert not t.is_alive(), "writer thread did not stop"
    assert not errors, errors
    return results


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

        def read():
            return ws.snapshot(my_position=POS)["nearby_units"]

        torn = 0
        for units in _run_with_writer(write, read):
            for u in units:
                if "level" in u and u["level"] != u.get("faction"):
                    torn += 1
        self.assertEqual(torn, 0)

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
        snap = ws.snapshot(my_position=POS)
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
