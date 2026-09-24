"""UM-89: short string handles instead of raw 64-bit GUIDs in what the LLM
sees, and handle -> GUID resolution before a tool call reaches an action."""

import json
import unittest
from types import SimpleNamespace

from agent import actions as ac
from agent import handles as hd
from agent import npc
from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo
from agent.think import think_and_act

# Magistrix Erona's GUID from the 2026-09-19 live run — above 2^53, so a
# float64 round-trip turns it into 17379391218345058000.
ERONA = 17379391218345059262
ERONA_MANGLED = int(float(ERONA))


def create_block(guid, object_type=uo.TYPEID_UNIT, x=0.0, y=0.0, z=0.0, fields=None):
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=object_type,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": 0.0},
        fields=fields or {},
    )


def _ints(value):
    """Every int anywhere in a JSON-ish structure (keys included)."""
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _ints(k)
            yield from _ints(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _ints(v)


class HandleMapTest(unittest.TestCase):
    def test_prefix_follows_high_guid(self):
        m = hd.HandleMap()
        self.assertTrue(m.handle_for(ERONA).startswith("u"))            # 0xF130 creature
        self.assertTrue(m.handle_for(0xF1100000000000AA).startswith("o"))  # gameobject
        self.assertTrue(m.handle_for(0x4000000000000123).startswith("i"))  # item
        self.assertTrue(m.handle_for(1).startswith("p"))                 # player (small guid)
        self.assertTrue(m.handle_for(0xF101000000000001).startswith("c"))  # corpse

    def test_stable_and_distinct(self):
        m = hd.HandleMap()
        a, b = m.handle_for(ERONA), m.handle_for(2)
        self.assertNotEqual(a, b)
        self.assertEqual(m.handle_for(ERONA), a)
        self.assertEqual(m.handle_for(2), b)
        self.assertEqual(m.resolve(a), ERONA)
        self.assertEqual(m.resolve(a.upper()), ERONA)

    def test_zero_guid_is_none(self):
        self.assertIsNone(hd.HandleMap().handle_for(0))

    def test_unknown_handle_raises(self):
        m = hd.HandleMap()
        m.handle_for(ERONA)
        with self.assertRaises(hd.UnknownHandle):
            m.resolve("u99")

    def test_encode_does_not_mutate_input(self):
        m = hd.HandleMap()
        window = {"kind": "vendor", "vendor_guid": ERONA, "items": [{"entry": 1, "slot": 0}]}
        out = m.encode({"window": window, "item_guids": [ERONA, 0]})
        self.assertEqual(window["vendor_guid"], ERONA)
        self.assertEqual(out["window"]["vendor_guid"], m.handle_for(ERONA))
        self.assertEqual(out["item_guids"], [m.handle_for(ERONA), None])
        self.assertEqual(out["window"]["items"], [{"entry": 1, "slot": 0}])

    def test_resolve_params_maps_strings_and_passes_ints(self):
        m = hd.HandleMap()
        h = m.handle_for(ERONA)
        self.assertEqual(m.resolve_params({"guid": h, "stop_distance": 2.0}),
                         {"guid": ERONA, "stop_distance": 2.0})
        self.assertEqual(m.resolve_params({"vendor_guid": ERONA, "slot": 3}),
                         {"vendor_guid": ERONA, "slot": 3})
        self.assertEqual(m.resolve_params({"target_guid": None}), {"target_guid": None})
        with self.assertRaisesRegex(hd.UnknownHandle, "target_guid"):
            m.resolve_params({"target_guid": "u42"})


class SnapshotHandlesTest(unittest.TestCase):
    def _world(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER))
        ws.update_object(create_block(ERONA, x=2.0, fields={uf.UNIT_NPC_FLAGS: per.UNIT_NPC_FLAG_GOSSIP}))
        ws.update_object(create_block(0x0000000000000007, object_type=uo.TYPEID_PLAYER, x=3.0))
        ws.update_object(create_block(0xF1100000000000AA, object_type=uo.TYPEID_GAMEOBJECT, x=4.0))
        return ws

    def test_no_64bit_guid_anywhere_in_snapshot(self):
        ws = self._world()
        ws.apply_list_inventory({"vendor_guid": ERONA, "items": [], "reason": None})
        ws.start_trade_request(0x0000000000000007, initiated_by_me=True)
        snap = ws.snapshot(my_position=(530, 0.0, 0.0, 0.0, 0.0),
                           chat_inbox=[{"kind": "say", "sender_guid": 7, "sender_name": "Bob",
                                        "channel": None, "text": "hi"}])
        big = [n for n in _ints(snap) if n > 0xFFFFFFFF]
        self.assertEqual(big, [])
        self.assertNotIn(str(ERONA), json.dumps(snap))
        erona = snap["nearby_units"][0]
        self.assertEqual(ws.handles.resolve(erona["guid"]), ERONA)
        self.assertEqual(snap["window"]["vendor_guid"], erona["guid"])
        self.assertEqual(ws.handles.resolve(snap["trade"]["partner_guid"]), 7)
        self.assertEqual(snap["chat_inbox"][0]["sender_guid"], snap["nearby_players"][0]["guid"])
        # live state was not rewritten by the encoding
        self.assertEqual(ws.get_ui_state()["vendor_guid"], ERONA)

    def test_handle_stable_across_snapshots_and_despawn(self):
        ws = self._world()
        pos = (530, 0.0, 0.0, 0.0, 0.0)
        h1 = ws.snapshot(my_position=pos)["nearby_units"][0]["guid"]
        ws.update_object(create_block(0xF130000000000099, x=1.0))  # a new, closer unit
        snap2 = ws.snapshot(my_position=pos)
        self.assertEqual([u["guid"] for u in snap2["nearby_units"]][1], h1)
        ws.remove_guids([ERONA])
        ws.snapshot(my_position=pos)
        ws.update_object(create_block(ERONA, x=2.0))
        self.assertIn(h1, [u["guid"] for u in ws.snapshot(my_position=pos)["nearby_units"]])


class CatalogTest(unittest.TestCase):
    def test_every_guid_param_is_a_string_handle(self):
        found = 0
        for tool in ac.catalog():
            for name, spec in tool["parameters"]["properties"].items():
                if hd.is_guid_key(name):
                    found += 1
                    self.assertEqual(spec["type"], "string", f"{tool['name']}.{name}")
                    self.assertIn("handle", spec["description"].lower(), f"{tool['name']}.{name}")
        self.assertGreaterEqual(found, 15)


class FakeLLMClient:
    def __init__(self, action_name, params):
        self.action_name, self.params = action_name, params
        self.snapshots = []

    def choose_action(self, snapshot, catalog, persona=""):
        self.snapshots.append(snapshot)
        return self.action_name, self.params


def fake_session():
    sent = []
    sess = SimpleNamespace(race=10, player_guid=1, player_position=(530, 0.0, 0.0, 0.0, 0.0))
    sess._send_packet = lambda opcode, payload=b'': sent.append((opcode, payload))
    sess._sent = sent
    return sess


class ThinkResolutionTest(unittest.TestCase):
    def _world(self):
        ws = per.WorldState()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER))
        ws.update_object(create_block(ERONA, x=2.0, fields={uf.UNIT_NPC_FLAGS: per.UNIT_NPC_FLAG_GOSSIP}))
        return ws

    def test_interact_by_handle_sends_the_real_guid(self):
        ws, sess = self._world(), fake_session()
        handle = ws.snapshot(my_position=sess.player_position)["nearby_units"][0]["guid"]
        llm = FakeLLMClient("interact", {"guid": handle})
        result = think_and_act(sess, ws, llm, my_position=sess.player_position)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.params, {"guid": handle})  # what the model said, for the audit log
        self.assertEqual(sess._sent, [(npc.CMSG_GOSSIP_HELLO, npc.build_gossip_hello(ERONA))])
        # the prompt the LLM saw carried the handle, not the GUID
        self.assertNotIn(str(ERONA), json.dumps(llm.snapshots[0]))

    def test_unknown_handle_is_a_clear_validation_error(self):
        ws, sess = self._world(), fake_session()
        result = think_and_act(sess, ws, FakeLLMClient("interact", {"guid": "u999"}),
                               my_position=sess.player_position)
        self.assertFalse(result.ok)
        self.assertIn("unknown handle", result.error)
        self.assertIn("u999", result.error)
        self.assertEqual(sess._sent, [])

    def test_mangled_float_guid_still_fails_as_before(self):
        ws, sess = self._world(), fake_session()
        result = think_and_act(sess, ws, FakeLLMClient("interact", {"guid": ERONA_MANGLED}),
                               my_position=sess.player_position)
        self.assertFalse(result.ok)
        self.assertIn("not currently perceived", result.error)

    def test_direct_python_callers_with_int_guids_still_work(self):
        ws, sess = self._world(), fake_session()
        result = ac.REGISTRY["interact"].run(sess, ws, guid=ERONA)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(sess._sent, [(npc.CMSG_GOSSIP_HELLO, npc.build_gossip_hello(ERONA))])


if __name__ == "__main__":
    unittest.main()
