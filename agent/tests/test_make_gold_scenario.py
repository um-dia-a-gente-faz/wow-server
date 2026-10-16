"""#317 scenario: a make_gold agent with four white trade goods and a vendor two yards away walks
the think loop: interact opens the vendor, then one sell_item per item, and the mission is still
make_gold afterwards.

Real think_and_act, real Brain (a Jev stub that takes the first non-idle candidate), real
candidate generator, real interact and sell_item actions, real WorldState. Only the wire is
faked: the session records every packet, and the server's two answers (the vendor window and
the item leaving the bag) are applied to the world directly. World-mock has no vendor, so this
runs in process."""

import struct
import unittest
from unittest import mock

from agent import actions as ac
from agent import brain as br
from agent import npc
from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo
from agent.http_api import AgentObserver
from agent.tests.builders import FakeSession
from agent.tests.test_perception import create_block
from agent.think import ThinkState, think_and_act

ME, VENDOR = 0x1, 0x2000
ITEM_GUIDS = {23: 0xF120000000000023, 24: 0xF120000000000024, 25: 0xF120000000000025, 26: 0xF120000000000026}
TRADE_GOODS = 7   # ItemTemplate.h ITEM_CLASS_TRADE_GOODS
WHITE = 1         # ITEM_QUALITY_NORMAL
POS = (530, 0.0, 0.0, 0.0, 0.0)
CYCLES = 5        # interact, then four sells


class FirstChoiceJev:
    """Scripted Jev: takes the first option that is not idle."""
    model = "scripted"

    def choose_action(self, snapshot, options, persona="", history=None):
        pick = next(c for c in options if c["action"] != "idle")
        return pick["action"], pick["params"]


class MakeGoldScenarioTest(unittest.TestCase):
    def setUp(self):
        self.world = per.WorldState()
        self.world.set_my_guid(ME)
        self.world.set_my_map(POS[0])
        self.bag = dict(ITEM_GUIDS)  # slot -> item guid, what is in the backpack
        self.sent = []
        self.session = FakeSession(player_guid=ME, player_name="Gold", player_position=POS, events=[])
        self.session._send_packet = self.on_send
        self.world.update_object(create_block(VENDOR, x=2.0, fields={uf.UNIT_NPC_FLAGS: 0x80}))  # vendor flag
        for n, (_slot, guid) in enumerate(sorted(self.bag.items())):
            entry = 2001 + n
            self.world.update_object(create_block(guid, object_type=uo.TYPEID_ITEM,
                                                  fields={uf.OBJECT_FIELD_ENTRY: entry}))
            self.world.items.items[entry] = {"found": True, "name": f"Trade {n}", "quality": WHITE,
                                             "class_": TRADE_GOODS, "inventory_type": 0, "stats": [],
                                             "spells": [], "sell_price": 100}
        self.give_bag()

    def give_bag(self):
        """The backpack as the server sends it: every slot 23-38 set, an empty one as 0 (fields merge)."""
        fields = {}
        for slot in range(23, 39):
            guid = self.bag.get(slot, 0)
            base = uf.PLAYER_FIELD_PACK_SLOT_1 + (slot - 23) * 2
            fields[base], fields[base + 1] = guid & 0xFFFFFFFF, guid >> 32
        self.world.update_object(create_block(ME, object_type=uo.TYPEID_PLAYER, fields=fields))

    def on_send(self, opcode, payload=b""):
        """The server's answers: the vendor window opens, and a sold item leaves the bag."""
        self.sent.append((opcode, payload))
        if opcode == npc.CMSG_LIST_INVENTORY:
            self.world.apply_list_inventory({"vendor_guid": VENDOR, "items": [], "reason": None})
        elif opcode == npc.CMSG_SELL_ITEM:
            _vendor, item_guid, _count = struct.unpack("<QQI", payload)
            self.bag = {slot: g for slot, g in self.bag.items() if g != item_guid}
            self.give_bag()

    def test_make_gold_sells_the_loot_and_never_completes(self):
        brain = br.Brain(jev=FirstChoiceJev(), mission="make_gold")
        state = ThinkState()
        with mock.patch.object(ac.SellItemAction, "confirm_timeout", 0.01):
            results = [think_and_act(self.session, self.world, brain, my_position=POS, cycle=n,
                                     state=state) for n in range(CYCLES)]

        self.assertEqual([r.action_name for r in results], ["interact"] + ["sell_item"] * 4)
        self.assertTrue(all(r.ok for r in results), [r.error for r in results])
        self.assertIsInstance(results[0].params["guid"], str)  # the model sees a handle ("u3"), not a raw guid

        opcodes = [opcode for opcode, _ in self.sent]
        self.assertEqual(opcodes.count(npc.CMSG_LIST_INVENTORY), 1)
        sells = [struct.unpack("<QQI", p) for opcode, p in self.sent if opcode == npc.CMSG_SELL_ITEM]
        # CMSG_SELL_ITEM is vendor guid, the item's own guid, then the count (0 = whole stack), as
        # HandleSellItemOpcode reads it. One per item, in backpack slot order.
        self.assertEqual(sells, [(VENDOR, ITEM_GUIDS[slot], 0) for slot in (23, 24, 25, 26)])
        self.assertEqual(self.bag, {})
        self.assertEqual(brain.mission, "make_gold")

    def test_the_money_on_the_agent_api_follows_the_session_coinage(self):
        self.session.coinage = 12345
        observer = AgentObserver("Gold", goal="make gold", model="test-model")
        observer.attach(self.session, ThinkState(), lambda s: {})
        self.assertEqual(observer.state()["self"]["money"], 12345)
        self.session.coinage = 12400
        self.assertEqual(observer.state()["self"]["money"], 12400)


if __name__ == "__main__":
    unittest.main()
