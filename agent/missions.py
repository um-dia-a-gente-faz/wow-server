"""The closed set of agent missions (#314). `agents/roster.py` keeps its own copy of MISSIONS,
because `agents/` may not import `agent/`; tools/tests/test_roster_accounts.py pins the two equal.

A mission is what an agent is for. Nothing writes it at runtime: it comes from the roster or
AGENT_MISSION, and the candidate generator only uses it for the fallback option.
"""

MISSIONS = ("level_to_cap", "explore", "make_gold")
DEFAULT_MISSION = "level_to_cap"
