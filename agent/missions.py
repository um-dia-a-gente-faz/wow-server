"""The closed set of agent missions (#314). `agents/roster.py` keeps its own copy of MISSIONS,
because `agents/` may not import `agent/`; tools/tests/test_roster_accounts.py pins the two equal.

A mission is what an agent is for. It comes from the roster or AGENT_MISSION; at runtime only
agent.mission_state changes it (#316 transitions, owner pins), and the candidate generator uses it
for the fallback option.
"""

MISSIONS = ("level_to_cap", "explore", "make_gold")
DEFAULT_MISSION = "level_to_cap"

# #316: an explore agent goes back to level_to_cap after this many newly visited zones (below the
# level cap); level_to_cap switches to explore at this level. Config: AGENT_EXPLORE_NEW_ZONES, AGENT_LEVEL_CAP.
DEFAULT_EXPLORE_NEW_ZONES = 5
DEFAULT_LEVEL_CAP = 80
