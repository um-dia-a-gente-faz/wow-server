"""Shared fakes for the test_actions_*.py modules (split out of test_actions.py, issue #248).
"""

import threading
import time

from agent import update_fields as uf
from agent import update_object as uo
from agent.tests.builders import FakeSession


def fake_session(race=10, player_guid=0xF130000000000099, player_position=None, class_=0):
    """race defaults to 10 (Blood Elf, Horde)."""
    sess = FakeSession(race=race, class_=class_, pending_invite={"inviter_name": "Rubens"},
                            player_guid=player_guid, player_position=player_position,
                            events=[], spellbook=set(), spell_cooldowns={})
    return sess


def object_at(guid, x, y, z, object_type="unit"):
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT,
        guid=guid,
        object_type=uo.TYPEID_PLAYER if object_type == "player" else uo.TYPEID_UNIT,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": 0.0},
        fields={},
    )


def append_event_after(sess, delay, event):
    """Appends `event` (with a fresh t=time.monotonic()) to sess.events
    after `delay` seconds, on a background thread — used to satisfy an
    action's _wait_for/_wait_for_value confirmation loop from outside the
    single-threaded execute() call under test."""
    def worker():
        time.sleep(delay)
        sess.events.append({**event, "t": time.monotonic()})
    t = threading.Thread(target=worker)
    t.start()
    return t


def npc_object(guid, x, y, z, npc_flags=0, object_type="unit"):
    """A creature/gameobject ObjectInfo with npc_flags set via a VALUES
    merge, mirroring how real SMSG_UPDATE_OBJECT blocks land (CREATE with
    movement, then a fields-only merge)."""
    world_obj_type = uo.TYPEID_GAMEOBJECT if object_type == "gameobject" else uo.TYPEID_UNIT
    block = uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=world_obj_type,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": 0.0},
        fields={uf.UNIT_NPC_FLAGS: npc_flags} if object_type != "gameobject" else {},
    )
    return block


def item_object(guid, entry, count=1):
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=uo.TYPEID_ITEM,
        movement={"update_flags": 0},
        fields={uf.OBJECT_FIELD_ENTRY: entry, uf.ITEM_FIELD_STACK_COUNT: count},
    )


def self_player_with_slots(guid, slot_guids):
    raw = {}
    for slot, item_guid in slot_guids.items():
        if slot < 23:
            base = uf.PLAYER_FIELD_INV_SLOT_HEAD + slot * 2
        else:
            base = uf.PLAYER_FIELD_PACK_SLOT_1 + (slot - 23) * 2
        raw[base] = item_guid & 0xFFFFFFFF
        raw[base + 1] = item_guid >> 32
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=uo.TYPEID_PLAYER,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0},
        fields=raw,
    )
