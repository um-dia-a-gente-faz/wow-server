"""Prompt assets (#395): bytes sent to the model are unchanged, ids are recorded."""
import json
import os
import re
import unittest

from agent import llm, prompts

GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "prompts", "golden_messages.json")


class GoldenMessagesTest(unittest.TestCase):
    def test_messages_are_byte_identical_to_the_pre_asset_output(self):
        # golden_messages.json was generated from the string constants in llm.py before they moved
        with open(GOLDEN, encoding="utf-8") as f:
            cases = json.load(f)
        for c in cases:
            got = llm.build_messages(c["snapshot"], persona=c["persona"], history=c["history"])
            self.assertEqual(json.dumps(got), json.dumps(c["messages"]))

    def test_llm_module_holds_no_prompt_text(self):
        self.assertFalse(hasattr(llm, "SYSTEM_PROMPT") or hasattr(llm, "GOAL_PROMPT"))


class PromptIdTest(unittest.TestCase):
    def test_id_names_each_asset_with_an_8_hex_hash(self):
        self.assertRegex(prompts.prompt_id(), r"^system@[0-9a-f]{8}\+goal\.level_up@[0-9a-f]{8}$")

    def test_persona_adds_the_persona_asset(self):
        self.assertTrue(prompts.prompt_id("x").endswith("+" + prompts.asset_id("persona")))

    def test_hash_follows_content(self):
        a = prompts.asset_id("system")
        real = prompts.load
        prompts.load = lambda name: real(name) + " "
        try:
            self.assertNotEqual(prompts.asset_id("system"), a)
        finally:
            prompts.load = real
        self.assertTrue(re.fullmatch(r"system@[0-9a-f]{8}", a))


if __name__ == "__main__":
    unittest.main()
