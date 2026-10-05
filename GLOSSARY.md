# WoW agent server

A private World of Warcraft 3.3.5a realm on the LAN where AI-driven characters play alongside the owner, plus the site used to watch and control them.

## Language

**Agent**:
An AI-driven character that plays on the realm like any other player and makes its own decisions.
_Avoid_: bot, NPC

**Brain**:
The part of an agent that chooses its next action each think cycle (Jev or an LLM).
_Avoid_: AI, model

**Reflex**:
A fast, mechanical behaviour that runs between brain decisions once the brain has switched it on.
_Avoid_: macro, script

**Mission**:
The long-term goal an agent works toward, chosen from a fixed set of kinds and not written as free text. It shapes which options the brain is offered and how they rank.
_Avoid_: goal, objective, task, quest (a quest is an in-game quest)

**Level to cap**:
Mission kind: reach the maximum level by questing, killing mobs and travelling between regions, finding quest givers by walking the map. The default for a newly created agent.

**Explore**:
Mission kind: discover new areas of the world.

**Make gold**:
Mission kind: accumulate money.

**Override**:
A period in which an admin drives an agent from the site: its brain is paused and its reflexes are off until the admin releases it.
_Avoid_: takeover, manual mode

**Unmet intent**:
A record that the brain wanted an action the agent does not have. Unmet intents are reviewed and may become issues for new actions.
_Avoid_: unknown action, missing action

**Follow cycle**:
A state where agents follow each other (or themselves) so none of them does anything useful. It must never be possible.
