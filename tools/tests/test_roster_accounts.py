"""The two definitions of "an agent account" agree (#391).

agents/roster.py validates the committed roster and derives account names; tools/common/
agent_accounts.py answers "is this account an agent" inside the wowmap and chat-feed images,
where the agents/ library is not shipped (and where wowmap has its own top-level module named
`agents`). They stay two files, so this test pins that they agree. Stdlib only.
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from agents import roster  # noqa: E402
from common.agent_accounts import is_agent_account  # noqa: E402


class RosterAccountTests(unittest.TestCase):
    def test_every_roster_account_is_an_agent_account(self):
        accounts = [a.account for a in roster.load_roster()]
        self.assertEqual([a for a in accounts if not is_agent_account(a)], [])

    def test_the_predicate_covers_exactly_the_roster_positions(self):
        size = len(roster.load_roster())
        self.assertTrue(is_agent_account(roster.account_name(size)))
        self.assertFalse(is_agent_account(roster.account_name(size + 1)))


if __name__ == "__main__":
    unittest.main()
