"""Per-domain section builders of the perception snapshot (#368): self_state and
group_state, composed by compose_snapshot in a fixed key order."""

import unittest

from agent import group as group_mod
from agent import update_object as uo
from agent.perception import WorldState
from agent.perception.objects import PLAYER_FLAGS_GHOST, ObjectInfo
from agent.perception.group_state import group_member_guids, group_section
from agent.perception.self_state import resolved_position, self_section
from agent.tests.test_perception import create_block

# The prompt order of the snapshot (#368 Decision). A builder that reorders a
# section changes what the LLM reads first, so the order is pinned here.
SNAPSHOT_KEY_ORDER = [
    "position", "is_dead", "is_ghost", "corpse_position",
    "nearby_units", "nearby_players", "nearby_objects",
    "window", "trade", "mailbox", "has_new_mail", "equipment", "inventory",
    "pending_invite", "group", "chat_inbox", "channels", "quest_log",
]


def _world_with_self_and_neighbours():
    ws = WorldState()
    ws.set_my_guid(1)
    ws.set_my_map(530)
    ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, x=0, y=0, z=0))
    ws.update_object(create_block(2, object_type=uo.TYPEID_UNIT, x=5, y=0, z=0, fields={0x03: 2}))
    ws.update_object(create_block(3, object_type=uo.TYPEID_PLAYER, x=6, y=0, z=0))
    return ws


class SnapshotKeyOrderTest(unittest.TestCase):
    def test_snapshot_keys_are_in_prompt_order(self):
        ws = _world_with_self_and_neighbours()
        snap = ws.snapshot(max_range=50, limit=5, corpse_position=(0, 1.0, 2.0, 3.0, 0.0),
                           pending_invite={"inviter": "Bob"}, chat_inbox=[],
                           group={"leader_guid": 3, "raid": False, "loot_method": 1,
                                  "members": [{"guid": 3, "name": "Bob"}]})
        self.assertEqual(list(snap), SNAPSHOT_KEY_ORDER)

    def test_key_order_holds_without_a_position_and_with_limit_zero(self):
        ws = WorldState()
        snap = ws.snapshot(limit=0)
        self.assertEqual(list(snap), SNAPSHOT_KEY_ORDER)
        self.assertEqual(snap["nearby_units"], [])
        self.assertEqual(snap["nearby_players"], [])
        self.assertEqual(snap["nearby_objects"], [])


class SelfSectionTest(unittest.TestCase):
    def test_no_self_and_no_position_gives_empty_self(self):
        d = self_section(None, None, None)
        self.assertIsNone(d["position"])
        self.assertIs(d["is_dead"], False)
        self.assertIs(d["is_ghost"], False)
        self.assertIsNone(d["corpse_position"])

    def test_my_position_wins_over_me_position(self):
        me = ObjectInfo(guid=1, position=(530, 1.0, 2.0, 3.0, 0.0))
        d = self_section(me, (0, 9.0, 8.0, 7.0, 0.0), None)
        self.assertEqual(d["position"], {"map": 0, "x": 9.0, "y": 8.0, "z": 7.0})

    def test_falls_back_to_me_position(self):
        me = ObjectInfo(guid=1, position=(530, 1.0, 2.0, 3.0, 0.0))
        d = self_section(me, None, None)
        self.assertEqual(d["position"], {"map": 530, "x": 1.0, "y": 2.0, "z": 3.0})

    def test_corpse_position_is_converted(self):
        d = self_section(None, None, (0, 1.0, 2.0, 3.0, 0.0))
        self.assertEqual(d["corpse_position"], {"map": 0, "x": 1.0, "y": 2.0, "z": 3.0})

    def test_dead_and_ghost_flags(self):
        dead = ObjectInfo(guid=1, health=0)
        self.assertIs(self_section(dead, None, None)["is_dead"], True)
        ghost = ObjectInfo(guid=1, health=1, player_flags=PLAYER_FLAGS_GHOST)
        d = self_section(ghost, None, None)
        self.assertIs(d["is_ghost"], True)
        self.assertIs(d["is_dead"], False)

    def test_key_order(self):
        me = ObjectInfo(guid=1, position=(530, 1.0, 2.0, 3.0, 0.0))
        self.assertEqual(list(self_section(me, None, None)),
                         ["position", "is_dead", "is_ghost", "corpse_position"])

    def test_resolved_position_rule(self):
        me = ObjectInfo(guid=1, position=(530, 1.0, 2.0, 3.0, 0.0))
        self.assertIsNone(resolved_position(None, None))
        self.assertEqual(resolved_position(me, (0, 9.0, 8.0, 7.0, 0.0)), (0, 9.0, 8.0, 7.0, 0.0))
        self.assertEqual(resolved_position(me, None), (530, 1.0, 2.0, 3.0, 0.0))


GROUP = {"guid": 77, "counter": 1, "raid": False, "leader_guid": 3, "loot_method": "round_robin",
         "loot_threshold": 2, "master_looter_guid": None,
         "members": [{"name": "Bob", "guid": 3, "online": True, "subgroup": 0, "assistant": False, "roles": 0},
                     {"name": "Cat", "guid": 4, "online": False, "subgroup": 0, "assistant": True, "roles": 0}]}


class GroupSectionTest(unittest.TestCase):
    def test_ungrouped_gives_no_group(self):
        self.assertEqual(group_section(None, 1, None), {"pending_invite": None, "group": None})

    def test_grouped_matches_snapshot_view(self):
        d = group_section(GROUP, 1, None)
        self.assertEqual(d["group"], group_mod.snapshot_view(GROUP, 1))
        self.assertIs(d["group"]["is_leader"], False)

    def test_pending_invite_is_passed_through(self):
        invite = {"inviter": "Bob"}
        d = group_section(None, 1, invite)
        self.assertIs(d["pending_invite"], invite)

    def test_key_order(self):
        self.assertEqual(list(group_section(None, 1, None)), ["pending_invite", "group"])


class GroupMemberGuidsTest(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(group_member_guids(None), set())
        self.assertEqual(group_member_guids({}), set())

    def test_two_member_group(self):
        self.assertEqual(group_member_guids(GROUP), {3, 4})


if __name__ == "__main__":
    unittest.main()
