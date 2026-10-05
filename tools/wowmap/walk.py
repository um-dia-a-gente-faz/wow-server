"""#178: "walk this agent here" from the console.

Two halves, like fleet.py:

* `build_runner_body` turns what the page sends (a click on a zone map, or a player's name)
  into the agent runner's `POST /agents/<name>/walk` body: world x/y on the zone's map, or
  `near_player`. The runner, and behind it the agent's own session, do the walking; this
  site never moves anything itself, and there is no teleport, GM command or database write
  anywhere on this path (CLAUDE.md, "Never").
* `static/walk.css` / `static/walk.js` are the page half: a block in the inspect
  drawer with "Walk to a point" (then click the map) and "Walk to a player" (then click a
  player in the list or on the map). It is disabled, with the reason, for a human character,
  an agent whose container is not running, one that is not logged in, and when no runner is
  configured.

Everything the runner returns is untrusted text and is rendered with textContent.
"""
import math

MAX_BODY_BYTES = 2048


class WalkError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _num(payload, key):
    v = payload.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise WalkError(400, f"{key} must be a finite number")
    return float(v)


def build_runner_body(payload, tables):
    """The page's walk request -> the runner's body. Raises WalkError(400/404)."""
    if not isinstance(payload, dict):
        raise WalkError(400, "body must be a JSON object")
    unknown = sorted(set(payload) - {"near_player", "area_id", "nx", "ny"})
    if unknown:
        raise WalkError(400, f"unknown field(s): {', '.join(unknown)}")
    if payload.get("near_player") is not None:
        if any(k in payload for k in ("area_id", "nx", "ny")):
            raise WalkError(400, "give either a map point or near_player, not both")
        name = payload["near_player"]
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 12 or not name.strip().isalpha():
            raise WalkError(400, "near_player must be a character name (2-12 letters)")
        return {"near_player": name.strip()}
    area = payload.get("area_id")
    if isinstance(area, bool) or not isinstance(area, int) or area <= 0:
        raise WalkError(400, "area_id must be a zone id")
    nx, ny = _num(payload, "nx"), _num(payload, "ny")
    if not (0 <= nx <= 1 and 0 <= ny <= 1):
        raise WalkError(400, "the point is outside the zone map")
    world = tables.from_normalised(area, nx, ny)
    map_id = tables.zone_map.get(area)
    if world is None or map_id is None:
        raise WalkError(404, "unknown zone")
    # z is left out on purpose: a map click carries no height, and the character keeps its own.
    return {"x": round(world[0], 2), "y": round(world[1], 2), "map": map_id}
