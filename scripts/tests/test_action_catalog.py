"""#504: the action catalog section of docs/AGENT-API.md is generated from agent/action_specs.py,
and editing a spec without regenerating makes the drift check fail. The mutation tests patch the
specs in memory and compare renderings; they never write the real doc."""
import dataclasses
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent import action_specs  # noqa: E402
from agent import api_contract  # noqa: E402

DOC = ROOT / "docs" / "AGENT-API.md"


def _mutated(name, change):
    """all_specs with the spec called `name` replaced by change(spec)."""
    return tuple(change(s) if s.name == name else s for s in action_specs.all_specs)


class CatalogSectionTest(unittest.TestCase):
    def setUp(self):
        self.doc = DOC.read_text("utf-8")

    def test_catalog_section_is_in_the_doc(self):
        self.assertIn(api_contract.CATALOG_BEGIN, self.doc)
        self.assertIn(api_contract.CATALOG_END, self.doc)
        self.assertIn("## Action catalog", self.doc)

    def test_every_spec_has_an_entry(self):
        for spec in action_specs.all_specs:
            self.assertIn(f"### `{spec.name}`", self.doc, spec.name)


class DriftTest(unittest.TestCase):
    """render_markdown() is what `scripts/generate.py --check` compares the committed doc with."""

    def setUp(self):
        self.doc = DOC.read_text("utf-8")

    def test_untouched_specs_render_the_committed_doc(self):
        self.assertEqual(api_contract.render_markdown(), self.doc)

    def test_param_description_edit_fails_the_check(self):
        def change(s):
            params = {**s.params, "guid": {**s.params["guid"], "description": "edited in a spec"}}
            return dataclasses.replace(s, params=params)

        with mock.patch.object(action_specs, "all_specs", _mutated("set_target", change)):
            self.assertNotEqual(api_contract.render_markdown(), self.doc)

    def test_param_type_edit_fails_the_check(self):
        def change(s):
            params = {**s.params, "guid": {**s.params["guid"], "type": "integer"}}
            return dataclasses.replace(s, params=params)

        with mock.patch.object(action_specs, "all_specs", _mutated("set_target", change)):
            self.assertNotEqual(api_contract.render_markdown(), self.doc)

    def test_added_param_fails_the_check(self):
        def change(s):
            return dataclasses.replace(s, params={**s.params, "extra": {"type": "integer", "description": "x"}})

        with mock.patch.object(action_specs, "all_specs", _mutated("stop_movement", change)):
            self.assertNotEqual(api_contract.render_markdown(), self.doc)

    def test_required_change_fails_the_check(self):
        with mock.patch.object(action_specs, "all_specs",
                               _mutated("set_target", lambda s: dataclasses.replace(s, required=()))):
            self.assertNotEqual(api_contract.render_markdown(), self.doc)

    def test_cap_change_fails_the_check(self):
        with mock.patch.object(action_specs, "all_specs",
                               _mutated("cast_spell", lambda s: dataclasses.replace(s, cap=99))):
            self.assertNotEqual(api_contract.render_markdown(), self.doc)


if __name__ == "__main__":
    unittest.main()
