"""#531: the set of actions the LLM is not offered lives in agent/action_specs.py; the generated
action catalog marks each of them, and the LLM tool list is the catalog minus that set."""

import unittest
from unittest import mock

from agent import action_specs as specs
from agent import actions as ac
from agent import api_contract
from agent import brain
from agent import death  # noqa: F401  registers release_spirit/reclaim_corpse
from agent import llm
from agent.action_specs import LLM_EXCLUDED_ACTIONS
from agent.reflexes import follow, rest  # noqa: F401  register follow/assist/stop_following/rest

MARK = "Not offered to the LLM"


def _entries(lines: list[str]) -> dict[str, str]:
    """action name -> the rendered text of its catalog entry (up to the next heading)."""
    out, name = {}, None
    for line in lines:
        if line == api_contract.CATALOG_END:
            break
        if line.startswith("### "):
            name = line[len("### "):].strip("`")
            out[name] = ""
        elif name is not None:
            out[name] += line + "\n"
    return out


class ExcludedCatalogTest(unittest.TestCase):

    def test_brain_uses_the_action_specs_set(self):
        self.assertIs(brain.LLM_EXCLUDED_ACTIONS, LLM_EXCLUDED_ACTIONS)
        self.assertEqual(LLM_EXCLUDED_ACTIONS, frozenset({specs.IDLE}))

    def test_catalog_marks_exactly_the_excluded_actions(self):
        excluded = frozenset({specs.IDLE, specs.STOP_MOVEMENT})
        with mock.patch.object(specs, "LLM_EXCLUDED_ACTIONS", excluded):
            entries = _entries(api_contract.render_catalog())
        self.assertEqual(set(entries), {s.name for s in specs.all_specs})
        self.assertEqual({n for n, text in entries.items() if MARK in text}, excluded)

    def test_catalog_marks_nothing_when_the_set_is_empty(self):
        with mock.patch.object(specs, "LLM_EXCLUDED_ACTIONS", frozenset()):
            entries = _entries(api_contract.render_catalog())
        self.assertEqual([n for n, text in entries.items() if MARK in text], [])

    def test_llm_tool_list_is_the_catalog_minus_the_set(self):
        tools = llm.build_tools(brain.llm_catalog())
        names = [t["function"]["name"] for t in tools]
        registered = [s["name"] for s in ac.catalog()]
        self.assertEqual(names, [n for n in registered if n not in LLM_EXCLUDED_ACTIONS])
        self.assertEqual(set(names), set(specs.specs_by_name) - LLM_EXCLUDED_ACTIONS)


if __name__ == "__main__":
    unittest.main()
