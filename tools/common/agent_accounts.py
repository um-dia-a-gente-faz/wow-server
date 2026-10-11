"""Which account names belong to agents (#451). Agent characters live on AGENT01..AGENT25
(agents/roster.json); agent-runner validates requests with it and wowmap labels a character
agent or human with it. Stdlib only."""
import re

_AGENT_ACCOUNT = r"AGENT(?:0[1-9]|1[0-9]|2[0-5])"
_EXACT = re.compile(_AGENT_ACCOUNT)
_EXACT_ANY_CASE = re.compile(_AGENT_ACCOUNT, re.IGNORECASE)


def is_agent_account(name, ignore_case=False):
    """True for exactly AGENT01..AGENT25. Anything that is not a str is False."""
    if not isinstance(name, str):
        return False
    return (_EXACT_ANY_CASE if ignore_case else _EXACT).fullmatch(name) is not None
