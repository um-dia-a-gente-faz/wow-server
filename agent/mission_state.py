"""The mission in force and the automatic transitions between missions (#316). Stdlib only.

Rules (the #316 Decision):
- level_to_cap at or past the level cap goes to explore;
- explore below the cap goes back to level_to_cap after explore_new_zones newly visited zones;
- make_gold never moves; a zone already visited does not count.
set_by_owner() sets the mission and pins it: no automatic transition until unpin().
"""

from .missions import MISSIONS

LEVEL_TO_CAP = "level_to_cap"
EXPLORE = "explore"


class MissionState:
    def __init__(self, base: str, explore_new_zones: int, level_cap: int):
        self.current = base              # the mission in force
        self.explore_new_zones = explore_new_zones
        self.level_cap = level_cap
        self.pinned = False              # set by the owner; stops the automatic transitions
        self.visited: set[int] = set()   # zone ids seen so far
        self.new_since_switch = 0        # zones newly visited since the last switch

    def observe(self, level: int | None, zone: int | None) -> str | None:
        """Count a zone not seen before, then apply the rules. Returns the new mission when a
        transition fires, else None. An unknown level or zone (None) never moves the mission."""
        if zone is not None and zone not in self.visited:
            self.visited.add(zone)
            self.new_since_switch += 1
        if self.pinned or level is None:
            return None
        if self.current == LEVEL_TO_CAP and level >= self.level_cap:
            return self._switch(EXPLORE)
        if (self.current == EXPLORE and level < self.level_cap
                and self.new_since_switch >= self.explore_new_zones):
            return self._switch(LEVEL_TO_CAP)
        return None

    def set_by_owner(self, mission: str) -> None:
        """An owner's choice (#817): it is the mission in force until unpin()."""
        if mission not in MISSIONS:
            raise ValueError(f"unknown mission {mission!r}")
        self.current = mission
        self.pinned = True
        self.new_since_switch = 0

    def unpin(self) -> None:
        self.pinned = False

    def _switch(self, mission: str) -> str:
        self.current = mission
        self.new_since_switch = 0
        return mission
