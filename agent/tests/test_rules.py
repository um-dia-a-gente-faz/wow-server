"""Candidates (what the LLM is offered) and actions (what is allowed) must read the
same game-rule constants, so a future copy that drifts fails here (#348)."""

import pathlib
import re
import unittest

from agent import actions, candidates, rules
from agent.actions import combat, loot, quest, vendor
from agent.actions import mail as mail_actions


class SharedRulesTests(unittest.TestCase):
    def test_candidates_and_actions_share_the_rules_objects(self):
        self.assertIs(candidates.MELEE_RANGE_YD, rules.MELEE_RANGE_YD)
        self.assertIs(candidates.INTERACT_RANGE_YD, rules.INTERACT_RANGE_YD)
        self.assertIs(combat.MELEE_RANGE_YD, rules.MELEE_RANGE_YD)
        self.assertIs(actions.MELEE_RANGE_YD, rules.MELEE_RANGE_YD)
        self.assertIs(loot.LOOT_RANGE_YD, rules.LOOT_RANGE_YD)
        for mod in (quest, vendor, mail_actions):
            self.assertIs(mod.INTERACT_RANGE_YD, rules.INTERACT_RANGE_YD)

    def test_range_constants_are_defined_only_in_rules(self):
        agent_dir = pathlib.Path(__file__).resolve().parents[1]
        pat = re.compile(r"^\s*[A-Z_]*RANGE_YD\s*=", re.M)
        offenders = [str(p.relative_to(agent_dir)) for p in agent_dir.rglob("*.py")
                     if p.name != "rules.py" and "tests" not in p.parts and pat.search(p.read_text())]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
