"""GH-71: SMSG_GROUP_LIST parsing, session group state, snapshot social
awareness and the decline/leave/promote actions."""

import struct
import unittest
from types import SimpleNamespace

from agent import actions as ac
from agent import group as grp
from agent import perception as per
from agent import session as sess_mod
from agent.tests.test_actions import fake_session, object_at


def member(name, guid, online=1, subgroup=0, flags=0, roles=0):
    return name.encode() + b"\x00" + struct.pack("<QBBBB", guid, online, subgroup, flags, roles)


def group_list(members, leader, loot=3, master=0, group_type=0, own_flags=0):
    # Layout per Group::SendUpdateToPlayer (see agent/group.py).
    out = struct.pack("<BBBB", group_type, 0, own_flags, 0)
    if group_type & 0x08:
        out += struct.pack("<BI", 0, 0)
    out += struct.pack("<QII", 0x1F00000000000001, 7, len(members))
    out += b"".join(members) + struct.pack("<Q", leader)
    if members:
        out += struct.pack("<BQBBBB", loot, master, 2, 0, 0, 0)
    return out


LEFT = struct.pack("<BBBB", 0x10, 0, 0, 0) + struct.pack("<QIIQ", 0x1F00000000000001, 7, 0, 0)
ME, ALICE, BOB = 0x10, 0x20, 0x30


class ParseGroupListTest(unittest.TestCase):
    def test_full_list(self):
        g = grp.parse_group_list(group_list([member("Alice", ALICE), member("Bob", BOB, online=0, flags=1)], ALICE))
        self.assertEqual([m["name"] for m in g["members"]], ["Alice", "Bob"])
        self.assertEqual(g["leader_guid"], ALICE)
        self.assertEqual(g["loot_method"], "group_loot")
        self.assertFalse(g["members"][1]["online"])
        self.assertTrue(g["members"][1]["assistant"])
        self.assertFalse(g["raid"])

    def test_master_loot_and_raid_flag(self):
        g = grp.parse_group_list(group_list([member("Alice", ALICE)], ME, loot=2, master=ALICE, group_type=0x02))
        self.assertTrue(g["raid"])
        self.assertEqual(g["loot_method"], "master_loot")
        self.assertEqual(g["master_looter_guid"], ALICE)

    def test_lfg_header_is_skipped(self):
        g = grp.parse_group_list(group_list([member("Alice", ALICE)], ALICE, group_type=0x08))
        self.assertEqual(g["leader_guid"], ALICE)

    def test_left_form_is_none(self):
        self.assertIsNone(grp.parse_group_list(LEFT))

    def test_view_hides_nothing_but_marks_leadership(self):
        g = grp.parse_group_list(group_list([member("Alice", ALICE)], ALICE))
        view = grp.snapshot_view(g, ME)
        self.assertFalse(view["is_leader"])
        self.assertEqual(view["leader_name"], "Alice")
        self.assertIsNone(grp.snapshot_view(None, ME))


class SessionStateTest(unittest.TestCase):
    def make(self):
        s = SimpleNamespace(group=None, pending_invite={"inviter_name": "Alice"}, events=[])
        s._record_event = lambda kind, **f: s.events.append({"kind": kind, **f})
        for n in ("_handle_group_list", "_handle_group_gone", "_handle_group_decline"):
            setattr(s, n, getattr(sess_mod.WoWSession, n).__get__(s))
        return s

    def test_list_sets_group_and_clears_invite(self):
        s = self.make()
        s._handle_group_list(group_list([member("Alice", ALICE)], ALICE))
        self.assertEqual(s.group["leader_guid"], ALICE)
        self.assertIsNone(s.pending_invite)

    def test_leave_form_and_destroy_clear_group(self):
        s = self.make()
        s._handle_group_list(group_list([member("Alice", ALICE)], ALICE))
        s._handle_group_list(LEFT)
        self.assertIsNone(s.group)
        s._handle_group_list(group_list([member("Alice", ALICE)], ALICE))
        s._handle_group_gone(b"")
        self.assertIsNone(s.group)

    def test_truncated_list_keeps_state(self):
        s = self.make()
        s._handle_group_list(group_list([member("Alice", ALICE)], ALICE))
        s._handle_group_list(b"\x00\x00")
        self.assertIsNotNone(s.group)

    def test_decline_event(self):
        s = self.make()
        s._handle_group_decline(b"Bob\x00")
        self.assertEqual(s.events[-1], {"kind": "group_invite_declined", "target_name": "Bob"})


class SnapshotTest(unittest.TestCase):
    def test_nearby_players_flag_group_members_and_group_exposed(self):
        w = per.WorldState()
        w.my_guid = ME
        w.update_object(object_at(ME, 0, 0, 0, "player"))
        w.update_object(object_at(ALICE, 3, 0, 0, "player"))
        w.update_object(object_at(BOB, 5, 0, 0, "player"))
        g = grp.parse_group_list(group_list([member("Alice", ALICE)], ALICE))
        snap = w.snapshot(my_position=(None, 0, 0, 0, 0), group=g)
        flags = {p["name"] or p["distance"]: p["in_group"] for p in snap["nearby_players"]}
        self.assertEqual(sorted(flags.values()), [False, True])
        self.assertEqual(snap["group"]["leader_name"], "Alice")
        self.assertEqual(snap["group"]["loot_method"], "group_loot")
        self.assertIsNone(w.snapshot(my_position=(None, 0, 0, 0, 0))["group"])


class ActionsTest(unittest.TestCase):
    def test_decline_sends_opcode_and_clears_invite(self):
        s = fake_session()
        r = ac.REGISTRY["decline_group"].run(s, None)
        self.assertTrue(r.ok)
        self.assertEqual(s._sent, [(0x073, b"")])
        self.assertIsNone(s.pending_invite)

    def test_decline_without_invite_fails(self):
        s = fake_session()
        s.pending_invite = None
        self.assertFalse(ac.REGISTRY["decline_group"].run(s, None).ok)

    def test_leave_requires_group_and_waits_for_confirmation(self):
        s = fake_session()
        s.group = None
        self.assertFalse(ac.REGISTRY["leave_group"].run(s, None).ok)
        s.group = {"leader_guid": ALICE, "members": []}
        action = ac.LeaveGroupAction()
        action.confirm_timeout = 0.05
        r = action.run(s, None)  # server never answers -> not ok
        self.assertFalse(r.ok)
        self.assertEqual(s._sent[-1], (0x07B, b""))
        s.group = {"leader_guid": ALICE, "members": []}
        s._send_packet = lambda op, payload=b"": setattr(s, "group", None)
        self.assertTrue(action.run(s, None).ok)

    def test_promote_leader(self):
        s = fake_session(player_guid=ME)
        action = ac.PromoteLeaderAction()
        action.confirm_timeout = 0.05
        self.assertIn("not in a group", action.run(s, None, name="Alice").error)
        s.group = {"leader_guid": ALICE, "members": [{"name": "Alice", "guid": ALICE}]}
        self.assertIn("only the group leader", action.run(s, None, name="Alice").error)
        s.group = {"leader_guid": ME, "members": [{"name": "Alice", "guid": ALICE}]}
        self.assertIn("not in your group", action.run(s, None, name="Zed").error)
        r = action.run(s, None, name="alice")
        self.assertFalse(r.ok)  # unconfirmed
        self.assertEqual(s._sent[-1], (0x078, struct.pack("<Q", ALICE)))
        s._send_packet = lambda op, payload=b"": s.group.update(leader_guid=ALICE)
        self.assertTrue(action.run(s, None, name="Alice").ok)


if __name__ == "__main__":
    unittest.main()
