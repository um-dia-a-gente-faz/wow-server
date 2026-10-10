"""Prompt text as versioned assets (#395). The prompts live next to this file as
plain Markdown so a prompt change is a readable diff. Each asset has an id,
`<name>@<first 8 hex of sha256 of its bytes>`, and the ids of the assets that made up a
request go into the audit record as `prompt_id`, so decisions can be sliced by prompt.
"""
import hashlib
from importlib.resources import files

SYSTEM = "system"
GOAL = "goal.level_up"  # the standing goal; missions (#314) add more goal.* assets
PERSONA = "persona"     # template with a {persona} slot


def load(name: str) -> str:
    return (files(__package__) / f"{name}.md").read_text(encoding="utf-8")


def asset_id(name: str) -> str:
    return f"{name}@{hashlib.sha256(load(name).encode('utf-8')).hexdigest()[:8]}"


def system_prompt(persona: str = "", goal: str = GOAL) -> str:
    """System message: rules + standing goal + optional persona. Pure."""
    text = load(SYSTEM) + "\n" + load(goal)
    if persona:
        text += "\n" + load(PERSONA).replace("{persona}", persona)
    return text


def prompt_id(persona: str = "", goal: str = GOAL) -> str:
    names = [SYSTEM, goal] + ([PERSONA] if persona else [])
    return "+".join(asset_id(n) for n in names)
