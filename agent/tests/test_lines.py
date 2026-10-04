"""Unit tests for agent.lines: the closed list of standard chat lines (#139)."""

import unittest

from agent import actions as ac
from agent import lines


class CatalogTest(unittest.TestCase):
    def test_catalog_is_clean(self):
        self.assertEqual(lines.check_catalog(), [])

    def test_every_line_fits_one_chat_packet(self):
        for text in lines.LINES.values():
            self.assertLessEqual(len(ac._encode_message(text)), 255)
            self.assertEqual(ac._encode_message(text).decode("utf-8"), text)

    def test_probe_line_exists(self):
        self.assertIn(lines.PROBE_LINE_ID, lines.LINES)

    def test_standard_lines_shape_and_order(self):
        listed = lines.standard_lines()
        self.assertEqual([entry["id"] for entry in listed], list(lines.LINES))
        for entry in listed:
            self.assertEqual(set(entry), {"id", "category", "text"})
            self.assertEqual(entry["text"], lines.LINES[entry["id"]])

    def test_check_catalog_catches_bad_edits(self):
        # check_catalog reads the module-level tuple, so patch it for the call.
        original = lines._LINES
        try:
            lines._LINES = original + (("greet_hello", "greeting", "Another one"),)
            self.assertTrue(any("duplicate id" in p for p in lines.check_catalog()))
            lines._LINES = original + (("braces", "x", "say }"),)
            self.assertTrue(any("disallowed" in p for p in lines.check_catalog()))
            lines._LINES = original + (("long", "x", "a" * 200),)
            self.assertTrue(any("longer than" in p for p in lines.check_catalog()))
            lines._LINES = original + (("dup_text", "x", "still   here."),)
            self.assertTrue(any("duplicate wording" in p for p in lines.check_catalog()))
        finally:
            lines._LINES = original


class ResolveTest(unittest.TestCase):
    def test_id_resolves_to_committed_text(self):
        self.assertEqual(lines.resolve_line("still_here"), "Still here.")

    def test_exact_text_resolves_to_itself(self):
        self.assertEqual(lines.resolve_line("Still here."), "Still here.")

    def test_free_text_is_rejected_not_sanitised(self):
        for bad in ("}", "}}dotspans", ": ", "hello world", "Still here", "still here.",
                    " Still here.", "Still here. ", "Still here.\n", "", None, 5, ["thanks"]):
            with self.subTest(bad=bad):
                self.assertIsNotNone(lines.line_error(bad))
                with self.assertRaises(lines.LineError):
                    lines.resolve_line(bad)

    def test_error_lists_the_valid_ids(self):
        self.assertIn("still_here", lines.line_error("nope"))

    def test_every_id_and_text_resolves(self):
        for line_id, text in lines.LINES.items():
            self.assertEqual(lines.resolve_line(line_id), text)
            self.assertEqual(lines.resolve_line(text), text)


if __name__ == "__main__":
    unittest.main()
