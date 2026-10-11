"""Names of the registered actions: the one place an action name is spelled outside its own class.
Candidates (and anything else that refers to an action) use these constants, so a typo is an
AttributeError at import instead of a silent miss at runtime. The constants are defined next to the
specs in agent.action_specs (gh-419) and re-exported here unchanged; a test pins them against
agent.actions.REGISTRY in both directions."""

from .action_specs import *  # noqa: F401,F403
