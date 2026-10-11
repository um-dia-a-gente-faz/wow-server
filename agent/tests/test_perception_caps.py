"""Per-section size caps for the perception snapshot (#367): every list-valued
section has an explicit cap in agent.perception.caps.SECTION_CAPS, cut by
apply_caps and reported in snapshot["truncated"] when anything was dropped."""

import unittest

from agent import update_object as uo
from agent.perception import WorldState
from agent.perception.caps import KEEP_NEWEST, SECTION_CAPS, apply_caps
from agent.tests.test_perception import create_block


def _snapshot_with(key, value):
    """A minimal snapshot holding `value` at SECTION_CAPS key `key` (dotted
    keys nest it inside the parent dict)."""
    parent, _, leaf = key.rpartition(".")
    if parent:
        return {parent: {leaf: value, "other": "kept"}}
    return {key: value}


def _section(snap, key):
    parent, _, leaf = key.rpartition(".")
    return snap[parent][leaf] if parent else snap[key]


class SectionCapsTest(unittest.TestCase):
    def test_every_key_is_cut_to_its_cap_and_reported(self):
        for key, cap in SECTION_CAPS.items():
            with self.subTest(key=key):
                items = list(range(cap + 3))
                snap = apply_caps(_snapshot_with(key, items))
                expected = items[-cap:] if key in KEEP_NEWEST else items[:cap]
                self.assertEqual(_section(snap, key), expected)
                self.assertEqual(snap["truncated"], {key: 3})

    def test_at_cap_nothing_is_dropped_and_truncated_is_absent(self):
        for key, cap in SECTION_CAPS.items():
            with self.subTest(key=key):
                items = list(range(cap))
                snap = apply_caps(_snapshot_with(key, items))
                self.assertEqual(_section(snap, key), items)
                self.assertNotIn("truncated", snap)

    def test_missing_or_non_list_sections_are_skipped(self):
        for key in SECTION_CAPS:
            for value in (None, "not a list", {}):
                with self.subTest(key=key, value=value):
                    snap = apply_caps(_snapshot_with(key, value))
                    self.assertNotIn("truncated", snap)
            parent, _, _ = key.rpartition(".")
            if parent:
                with self.subTest(key=key, parent=None):
                    snap = apply_caps({parent: None})
                    self.assertNotIn("truncated", snap)
                    self.assertEqual(snap, {parent: None})
            with self.subTest(key=key, absent=True):
                self.assertNotIn("truncated", apply_caps({}))

    def test_caller_nested_objects_are_not_mutated(self):
        mailbox = {"mailbox_guid": 5, "total_records": 53, "mails": [{"i": i} for i in range(53)]}
        members = [{"guid": i} for i in range(40)]
        group = {"leader_guid": 1, "members": members}
        snap = {"mailbox": mailbox, "group": group}

        out = apply_caps(snap, {"mailbox.mails": 50, "group.members": 39})

        self.assertEqual(len(mailbox["mails"]), 53)
        self.assertEqual(len(members), 40)
        self.assertIs(group["members"], members)
        self.assertNotIn("truncated", snap)
        self.assertIsNot(out["mailbox"], mailbox)
        self.assertIsNot(out["group"], group)
        self.assertEqual(len(out["mailbox"]["mails"]), 50)
        self.assertEqual(out["mailbox"]["total_records"], 53)
        self.assertEqual(out["mailbox"]["mailbox_guid"], 5)
        self.assertEqual(len(out["group"]["members"]), 39)
        self.assertEqual(out["group"]["leader_guid"], 1)
        self.assertEqual(out["truncated"], {"mailbox.mails": 3, "group.members": 1})

    def test_non_int_or_non_positive_cap_raises(self):
        for bad in (0, -1, 2.5, "3", True):
            with self.subTest(cap=bad):
                with self.assertRaises(ValueError):
                    apply_caps({"inventory": []}, {"inventory": bad})

    def test_chat_inbox_keeps_newest_through_snapshot(self):
        ws = WorldState()
        inbox = [{"text": f"msg {i}"} for i in range(SECTION_CAPS["chat_inbox"] + 3)]
        snap = ws.snapshot(chat_inbox=inbox)
        self.assertEqual(snap["chat_inbox"], inbox[-SECTION_CAPS["chat_inbox"]:])
        self.assertEqual(snap["truncated"], {"chat_inbox": 3})


class SnapshotCapsTest(unittest.TestCase):
    def test_limit_caps_the_three_nearby_sections_through_snapshot(self):
        ws = WorldState()
        ws.set_my_guid(1)
        ws.set_my_map(530)
        ws.update_object(create_block(1, object_type=uo.TYPEID_PLAYER, x=0, y=0, z=0))
        for i in range(2, 10):
            ws.update_object(create_block(i, object_type=uo.TYPEID_UNIT, x=float(i), y=0, z=0, fields={0x03: i}))
        for i in range(10, 18):
            ws.update_object(create_block(i, object_type=uo.TYPEID_PLAYER, x=float(i), y=0, z=0))
        for i in range(20, 28):
            ws.update_object(create_block(i, object_type=uo.TYPEID_GAMEOBJECT, x=float(i), y=0, z=0))
        snap = ws.snapshot(max_range=50, limit=5)
        for section in ("nearby_units", "nearby_players", "nearby_objects"):
            self.assertEqual(len(snap[section]), 5, section)

    def test_absent_mailbox_and_group_are_not_truncated(self):
        ws = WorldState()
        snap = ws.snapshot()
        self.assertIsNone(snap["mailbox"])
        self.assertIsNone(snap["group"])
        self.assertNotIn("truncated", snap)

    def test_mailbox_awaiting_its_mail_list_is_not_truncated(self):
        ws = WorldState()
        ws.open_mailbox_request(0x13)  # mails stays None until SMSG_MAIL_LIST_RESULT
        snap = ws.snapshot()
        self.assertIsNone(snap["mailbox"]["mails"])
        self.assertNotIn("truncated", snap)


if __name__ == "__main__":
    unittest.main()
