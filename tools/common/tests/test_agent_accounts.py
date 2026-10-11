"""#451: the one rule for which account names belong to agents (AGENT01..AGENT25)."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from agent_accounts import is_agent_account  # noqa: E402


class IsAgentAccount(unittest.TestCase):
    def test_the_whole_range_and_nothing_around_it(self):
        for n in range(1, 26):
            self.assertTrue(is_agent_account(f"AGENT{n:02d}"), n)
        for bad in ("AGENT00", "AGENT26", "AGENT1", "AGENT007", "AGENT07 ", " AGENT07",
                    "XAGENT07", "AGENT07X", "AGENT07\n", "", None, 7):
            self.assertFalse(is_agent_account(bad), bad)

    def test_case_matters_unless_asked(self):
        self.assertFalse(is_agent_account("agent07"))
        self.assertTrue(is_agent_account("agent07", ignore_case=True))
        self.assertTrue(is_agent_account("Agent25", ignore_case=True))


if __name__ == "__main__":
    unittest.main()
