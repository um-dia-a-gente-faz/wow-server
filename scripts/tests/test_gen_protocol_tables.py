"""scripts/gen_protocol_tables.py (#290): parse, render, and the drift check."""
import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import gen_protocol_tables as gen  # noqa: E402

COMMIT = "ed939325374216d6a049e778d6ad864c65283ae6"
FIELDS_H = """\
// Auto generated for version 3, 3, 5, 12340
enum EObjectFields
{
    OBJECT_FIELD_GUID                         = 0x0000, // Size: 2, Type: LONG, Flags: PUBLIC
    OBJECT_END                                = 0x0006
};

enum EUnitFields
{
    UNIT_FIELD_CHARM                          = OBJECT_END + 0x0000, // Size: 2, Type: LONG, Flags: PUBLIC
    UNIT_END                                  = OBJECT_END + 0x008E
};
"""
OPCODES_H = """\
enum Opcodes : uint16
{
    CMSG_ATTACK_SWING                               = 0x141,
    CMSG_AUTOEQUIP_ITEM                             = 0x10A,
    SMSG_UPDATE_OBJECT                              = 0x0A9,
    NUM_MSG_TYPES                                   = 0x51F
};
"""


class ParseTest(unittest.TestCase):
    def test_entries_keep_enum_expression_and_comment(self):
        self.assertEqual(gen.parse(FIELDS_H), [
            ("EObjectFields", "OBJECT_FIELD_GUID", "0x0000", "Size: 2, Type: LONG, Flags: PUBLIC"),
            ("EObjectFields", "OBJECT_END", "0x0006", ""),
            ("EUnitFields", "UNIT_FIELD_CHARM", "OBJECT_END + 0x0000", "Size: 2, Type: LONG, Flags: PUBLIC"),
            ("EUnitFields", "UNIT_END", "OBJECT_END + 0x008E", ""),
        ])


class RenderTest(unittest.TestCase):
    def test_update_fields_keep_the_base_plus_offset_style(self):
        block = gen.render_update_fields(gen.parse(FIELDS_H), COMMIT)
        self.assertIn(COMMIT, block)
        self.assertIn("UNIT_FIELD_CHARM = OBJECT_END + 0x0000  # Size: 2, Type: LONG, Flags: PUBLIC", block)
        self.assertIn("UNIT_END = OBJECT_END + 0x008E\n", block)
        ns = {}
        exec(block, ns)
        self.assertEqual(ns["UNIT_END"], 0x94)

    def test_opcodes_sorted_by_value_under_the_repo_name(self):
        block = gen.render_opcodes(gen.parse(OPCODES_H), COMMIT)
        lines = [line for line in block.splitlines() if " = 0x" in line]
        self.assertEqual(lines[:3], [
            "SMSG_UPDATE_OBJECT               = 0x0A9",
            "CMSG_AUTOEQUIP_ITEM              = 0x10A",
            "CMSG_ATTACKSWING                 = 0x141  # Opcodes.h name: CMSG_ATTACK_SWING",
        ])


class ExcerptTest(unittest.TestCase):
    def test_excerpt_keeps_only_selected_names_and_round_trips(self):
        text = gen.excerpt(FIELDS_H, {"OBJECT_END", "UNIT_END"}, "UpdateFields.h", COMMIT)
        self.assertEqual(gen.commit_of(text), COMMIT)
        self.assertEqual([e[1] for e in gen.parse(text)], ["OBJECT_END", "UNIT_END"])
        self.assertEqual(gen.parse(text)[1][0], "EUnitFields")

    def test_unknown_name_is_an_error(self):
        with self.assertRaises(SystemExit):
            gen.excerpt(FIELDS_H, {"UNIT_FIELD_NOPE"}, "UpdateFields.h", COMMIT)


class SpliceTest(unittest.TestCase):
    def test_replaces_only_the_marked_block(self):
        old = f"doc\n{gen.BEGIN}\nX = 1\n{gen.END}\ntail\n"
        self.assertEqual(gen.splice(old, "Y = 2\n"), f"doc\n{gen.BEGIN}\nY = 2\n{gen.END}\ntail\n")


class UpdateFieldsSectionTest(unittest.TestCase):
    """#725: agent/update_fields.py is owned only between its BEGIN and END markers. An edit
    inside the block is drift and the write run repairs it; text after END is hand-written and
    neither reported nor touched (pinned as intended, the generator is not changed)."""

    def setUp(self):
        self.orig = (gen.ROOT / "agent/update_fields.py").read_text()
        self.block = gen.generated("UpdateFields.h")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "update_fields.py"

    def test_edit_inside_the_block_is_reported_and_repaired(self):
        inside = self.orig.replace("GAMEOBJECT_LEVEL = OBJECT_END + 0x000A", "GAMEOBJECT_LEVEL = OBJECT_END + 0x000B")
        self.assertNotEqual(inside, self.orig, "anchor GAMEOBJECT_LEVEL not found")
        self.path.write_text(inside)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gen.sync(self.path, self.block, check=True), 1)
            self.assertEqual(gen.sync(self.path, self.block, check=False), 0)
        self.assertEqual(self.path.read_text(), self.orig)

    def test_text_after_the_block_is_neither_reported_nor_touched(self):
        after = self.orig.replace(gen.END + "\n", gen.END + "\nHAND_EDIT = 1\n", 1)
        self.assertNotEqual(after, self.orig)
        self.path.write_text(after)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gen.sync(self.path, self.block, check=True), 0)
            self.assertEqual(gen.sync(self.path, self.block, check=False), 0)
        self.assertEqual(self.path.read_text(), after)


class DriftTest(unittest.TestCase):
    def test_committed_tables_match_the_vendored_excerpts(self):
        self.assertEqual(gen.main(["--check"]), 0)

    def test_hand_edit_is_reported(self):
        target = gen.ROOT / "agent/opcodes.py"
        with tempfile.TemporaryDirectory() as tmp:
            edited = pathlib.Path(tmp) / "opcodes.py"
            edited.write_text(target.read_text().replace("= 0x10A", "= 0x0A8"))
            self.assertEqual(gen.sync(edited, gen.generated("Opcodes.h"), check=True), 1)
            self.assertIn("0x0A8", edited.read_text())  # --check never writes


if __name__ == "__main__":
    unittest.main()
