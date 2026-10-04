"""Operator walk command for one agent (#178).

The agent's observability API is read-only (agent/http_api.py). This module is the
single, opt-in exception: `POST /control/walk`, enabled only when AGENT_CONTROL_TOKEN
is set and called by tools/agent-runner (never by a browser). It moves the character
the only way a player can, with the character's own session: the same `move_to` /
`move_towards` actions the think loop uses (agent/actions.py -> agent/movement.py),
which send MSG_MOVE_START_FORWARD / heartbeats / MSG_MOVE_STOP as the client would.
There is no teleport, no GM command and no database write anywhere in this file.

Request body (exactly one target form):
    {"x": 1.0, "y": 2.0, "z": 3.0}     z optional; coordinates on the character's current map
    {"near_player": "Rubens"}          a player this agent currently perceives
Optional: "map" (refuse unless it is the character's current map), "stop_distance"
(yards, default 1 for a point and 3 next to a player), "timeout_s" (default 60, max 120).
`dry_run` resolves and validates everything and returns the plan without moving.

Concurrency: the think loop holds `observer.action_lock` for the whole of each cycle, so
a walk waits for the cycle in flight and then blocks the next one until it ends. An
explicit move replaces whatever the follow reflex was doing (actions._pause_follow_reflex).

Result: `outcome` is one of arrived | blocked | timeout | stopped | target_lost | failed;
`ok` is true only for `arrived`. Positions are the session's own view (what its movement
simulation last sent to the server), not a database read.
"""

import math
import threading
import time

from . import actions as ac
from . import movement

DEFAULT_TIMEOUT_S = 60.0
MAX_TIMEOUT_S = 120.0
LOCK_WAIT_S = 45.0
POINT_STOP_YD = 1.0
PLAYER_STOP_YD = 3.0
MIN_STOP_YD = 0.5
MAX_STOP_YD = 20.0
MAX_WALK_YD = 1500.0   # straight-line cap: there is no pathfinding, a far target only ever gets stuck
COORD_LIMIT = 40000.0  # WoW world coordinates are well inside +-17066 (MAP_SIZE / 2)
_KEYS = {"x", "y", "z", "near_player", "map", "stop_distance", "timeout_s"}


class ControlError(Exception):
    """A command refused before anything moved. `code` is stable for callers."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra

    def body(self) -> dict:
        return {"ok": False, "outcome": "refused", "code": self.code, "error": str(self), **self.extra}


def _number(body: dict, key: str, lo: float, hi: float, required: bool = False):
    if key not in body or body[key] is None:
        if required:
            raise ControlError(400, "bad_request", f"{key} is required")
        return None
    v = body[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ControlError(400, "bad_request", f"{key} must be a finite number")
    if not lo <= v <= hi:
        raise ControlError(400, "bad_request", f"{key} must be between {lo:g} and {hi:g}")
    return float(v)


def _pos(session):
    pos = getattr(session, "player_position", None)
    return None if pos is None else {"map": pos[0], "x": round(pos[1], 2), "y": round(pos[2], 2),
                                     "z": round(pos[3], 2)}


def validate(body) -> dict:
    """Shape-check a request body -> {"kind", "x","y","z" | "name", "map", "stop_distance", "timeout_s"}."""
    if not isinstance(body, dict):
        raise ControlError(400, "bad_request", "body must be a JSON object")
    unknown = sorted(set(body) - _KEYS)
    if unknown:
        raise ControlError(400, "bad_request", f"unknown field(s): {', '.join(unknown)}")
    has_point = "x" in body or "y" in body
    has_player = body.get("near_player") is not None
    if has_point == has_player:
        raise ControlError(400, "bad_request",
                           "give either x and y (a point) or near_player (a player), not both and not neither")
    out = {"map": None, "timeout_s": _number(body, "timeout_s", 1, MAX_TIMEOUT_S) or DEFAULT_TIMEOUT_S}
    if body.get("map") is not None:
        m = _number(body, "map", 0, 100000)
        if m != int(m):
            raise ControlError(400, "bad_request", "map must be an integer map id")
        out["map"] = int(m)
    if has_point:
        out.update(kind="point", x=_number(body, "x", -COORD_LIMIT, COORD_LIMIT, True),
                   y=_number(body, "y", -COORD_LIMIT, COORD_LIMIT, True),
                   z=_number(body, "z", -COORD_LIMIT, COORD_LIMIT))
        default_stop = POINT_STOP_YD
    else:
        err = ac.player_name_error(body["near_player"], "near_player")
        if err:
            raise ControlError(400, "bad_request", err)
        out.update(kind="player", name=body["near_player"].strip())
        default_stop = PLAYER_STOP_YD
    out["stop_distance"] = _number(body, "stop_distance", MIN_STOP_YD, MAX_STOP_YD) or default_stop
    return out


def resolve(session, req: dict) -> dict:
    """Resolve a validated request against the session's own snapshot -> plan dict, or ControlError."""
    if session is None or getattr(session, "unexpected_disconnect", False):
        raise ControlError(409, "not_connected", "the agent is not logged in to the world")
    pos = getattr(session, "player_position", None)
    if pos is None:
        raise ControlError(409, "no_position", "the agent does not know its own position yet")
    world = session.world_state
    me = world.get_my_object()
    if me is not None and me.is_dead():
        raise ControlError(409, "dead", "the character is dead; it cannot walk until it is resurrected")
    if req["map"] is not None and req["map"] != pos[0]:
        raise ControlError(409, "wrong_map",
                           f"the character is on map {pos[0]}, not map {req['map']}; walking cannot cross maps",
                           current_map=pos[0])
    plan = {"kind": req["kind"], "stop_distance": req["stop_distance"], "timeout_s": req["timeout_s"]}
    if req["kind"] == "point":
        tx, ty, tz = req["x"], req["y"], req["z"]
        plan.update(target={"map": pos[0], "x": tx, "y": ty, "z": tz}, guid=None)
    else:
        name = req["name"].lower()
        own = getattr(session, "player_guid", None)
        found = next((o for o in world.get_objects().values()
                      if o.object_type == "player" and o.guid != own and (o.name or "").lower() == name
                      and o.position is not None), None)
        if found is None:
            raise ControlError(404, "player_not_perceived",
                               f"{req['name']} is not perceived by this agent (not within view range, "
                               "offline, or the name has not resolved yet); walk to a point instead")
        _, tx, ty, tz, _ = found.position
        if found.position[0] != pos[0]:
            raise ControlError(409, "wrong_map", f"{req['name']} is on map {found.position[0]}, not map {pos[0]}")
        plan.update(target={"map": pos[0], "x": round(tx, 2), "y": round(ty, 2), "z": round(tz, 2),
                            "player": found.name}, guid=found.guid)
    distance = math.hypot(tx - pos[1], ty - pos[2])
    if distance > MAX_WALK_YD:
        raise ControlError(400, "too_far", f"the target is {distance:.0f} yd away; the limit is "
                           f"{MAX_WALK_YD:.0f} yd (straight line, no pathfinding)", distance=round(distance, 1))
    plan["distance"] = round(distance, 1)
    return plan


def _outcome(result, timed_out: bool) -> str:
    if result.ok:
        return "arrived"
    if timed_out:
        return "timeout"
    return {"stuck": "blocked", "stopped": "stopped", "target lost": "target_lost"}.get(result.error, "failed")


def _message(outcome: str, result, plan: dict, timeout_s: float) -> str | None:
    if outcome == "arrived":
        return None
    if outcome == "blocked":
        why = (result.detail or {}).get("reason")
        return "blocked: the character stopped making progress (terrain, a mob, a door or no straight path)" \
               + (f" [{why}]" if why else "")
    if outcome == "timeout":
        return f"timed out after {timeout_s:g}s before arriving"
    if outcome == "target_lost":
        return "the player left the agent's perception while it was walking"
    if outcome == "stopped":
        return "the walk was interrupted by another movement command"
    return result.error or "the walk failed"


def walk(observer, body, dry_run: bool = False, clock=time.monotonic) -> tuple[int, dict]:
    """Run (or, with dry_run, only plan) a walk -> (http status, JSON body). Never raises."""
    try:
        req = validate(body)
        session = observer.session
        plan = resolve(session, req)
        start = _pos(session)
        base = {"agent": observer.agent_name, "target": plan["target"], "start": start,
                "distance": plan["distance"], "stop_distance": plan["stop_distance"],
                "timeout_s": plan["timeout_s"],
                "method": ("move_towards" if plan["kind"] == "player" else "move_to")
                          + " (the character's own movement, straight line, no pathfinding)"}
        if dry_run:
            return 200, {**base, "ok": True, "outcome": "dry_run", "dry_run": True, "end": start}
        if not observer.action_lock.acquire(timeout=LOCK_WAIT_S):
            raise ControlError(409, "busy", f"the agent is mid-action and did not free up within {LOCK_WAIT_S:g}s")
        try:
            # The session may have changed while we waited (reconnect): resolve again, now that nothing else moves it.
            session = observer.session
            plan = resolve(session, req)
            start = _pos(session)
            return _execute(observer, session, plan, base, start, clock)
        finally:
            observer.action_lock.release()
    except ControlError as e:
        return e.status, e.body()
    except Exception as e:  # noqa: BLE001 - the HTTP layer must always answer
        return 500, {"ok": False, "outcome": "failed", "code": "internal", "error": f"{type(e).__name__}: {e}"}


def _execute(observer, session, plan: dict, base: dict, start: dict, clock) -> tuple[int, dict]:
    world = session.world_state
    timed_out = threading.Event()
    mover_holder = []

    def on_timeout():
        timed_out.set()
        for m in mover_holder:
            m.stop()

    mover = movement.get_mover(session, world)
    mover_holder.append(mover)
    timer = threading.Timer(plan["timeout_s"], on_timeout)
    timer.daemon = True
    t0 = clock()
    timer.start()
    try:
        if plan["kind"] == "point":
            t = plan["target"]
            params = {"x": t["x"], "y": t["y"], "stop_distance": plan["stop_distance"]}
            if t["z"] is not None:
                params["z"] = t["z"]
            result = ac.REGISTRY["move_to"].run(session, world, **params)
        else:
            result = ac.REGISTRY["move_towards"].run(session, world, guid=plan["guid"],
                                                     stop_distance=plan["stop_distance"])
    finally:
        timer.cancel()
    outcome = _outcome(result, timed_out.is_set())
    end = _pos(session)
    out = {**base, "ok": outcome == "arrived", "outcome": outcome, "dry_run": False, "end": end,
           "elapsed_s": round(clock() - t0, 1), "error": _message(outcome, result, plan, plan["timeout_s"])}
    if start and end:
        out["moved"] = round(math.hypot(end["x"] - start["x"], end["y"] - start["y"]), 1)
        tg = plan["target"]
        out["end_distance"] = round(math.hypot(end["x"] - tg["x"], end["y"] - tg["y"]), 1)
    if plan["kind"] == "player" and plan["guid"] is not None:
        # The player moves; report the distance to where it is now, not where it was.
        obj = world.get_object(plan["guid"])
        if obj is not None and obj.position is not None and end:
            out["end_distance"] = round(math.hypot(end["x"] - obj.position[1], end["y"] - obj.position[2]), 1)
    # Failed walks are 422 so a caller that only checks the status never reads them as success.
    return (200 if outcome == "arrived" else 422), out
