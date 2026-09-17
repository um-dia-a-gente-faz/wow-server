"""Code-driven reflexes that run between LLM think cycles (UM-58).

A reflex is fast, mechanical behaviour the LLM turns on/off with a tool call
but never drives step-by-step itself — see docs/AGENT-DIRECTION.md
("Agents decide for themselves" / decision 2). `agent.reflexes.follow` is
the first one: keep formation with a party leader and assist their target.
"""
