#!/usr/bin/env python3
"""Known-good live-test targets: the Sunstrider Isle start area.

One place for the NPCs and mobs a scripted check may walk to, so the
capability probe (agent/tools/probe.py, #139) and the Phase 3 work (#98) agree
on them. Data only. Sources: .claude/skills/live-agent-test/SKILL.md ("Who is
where") and docs/AGENT-RUN-1-10.md.

Targets are matched by *name* among the objects the agent already perceives;
nothing here carries coordinates on purpose. A check whose target is not
perceived reports "skipped" instead of walking to a guessed position.
"""

from dataclasses import dataclass

# Sunstrider Isle is part of Eversong Woods, which lives on map 530 (Outland map
# id, shared with Silvermoon City). Used only to give a clearer skip reason.
START_AREA_MAP_ID = 530
START_AREA_NAME = "Sunstrider Isle"

# Farthest a probe step may walk to reach a fixture. Perception reaches about
# 100 yd; anything farther is not a fixture we can vouch for.
MAX_WALK_YD = 120.0


@dataclass(frozen=True)
class QuestGiver:
    name: str
    quest_id: int
    quest_title: str


QUEST_GIVER = QuestGiver("Magistrix Erona", 8325, "Reclaiming Sunstrider Isle")
VENDOR_NAME = "Shara Sunwing"

# Level 1, attackable, 40-100 yd out from the start area.
MOB_NAMES = ("Springpaw Cub", "Mana Wyrm")
MOB_LEVEL = 1
