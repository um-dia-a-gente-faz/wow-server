"""Targeting and movement actions: set_target, face, move_to, move_towards, stop_movement.

Split out of the former agent/actions.py (issue #248).
"""

import math
import struct

from .. import movement
from ..opcodes import (
    CMSG_SET_SELECTION,
)
from .base import (
    Action,
    ActionResult,
    DEFAULT_CONFIRM_POLL_S,
    DEFAULT_CONFIRM_TIMEOUT_S,
    _pause_follow_reflex,
    _wait_for,
    register,
    send,
)


def send_target(session, guid: int):
    """Target a unit or object by GUID. SetSelection::Read (MiscPackets.cpp):
    a single raw (not packed) uint64 guid."""
    send(session, CMSG_SET_SELECTION, struct.pack("<Q", guid))


# ── Registered actions (UM-36) ────────────────────────────────────────────

@register
class SetTargetAction(Action):
    name = "set_target"
    description = "Target a nearby unit, player, or object by its handle from the perception snapshot."
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the object to target."},
    }
    required = ("guid",)
    # Overridable (class or instance attribute) so tests don't have to block
    # for the production default — real confirmation normally lands within
    # one or two update-object ticks.
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, guid: int, **_) -> str | None:
        if world.get_object(guid) is None:
            return f"guid {guid:#x} is not currently perceived"
        return None

    def execute(self, session, world, guid: int, **_) -> ActionResult:
        send_target(session, guid)
        confirmed = _wait_for(lambda: (me := world.get_my_object()) is not None and me.target_guid == guid,
                               timeout=self.confirm_timeout, interval=self.confirm_interval)
        if not confirmed:
            return ActionResult(ok=False, error="target field did not update in time",
                                 detail={"guid": guid})
        return ActionResult(ok=True, detail={"guid": guid})


@register
class FaceAction(Action):
    name = "face"
    description = ("Turn in place to face a nearby object by handle, or a specific x,y position, "
                    "without otherwise moving.")
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the object to face. Mutually exclusive with x/y."},
        "x": {"type": "number", "description": "Target X coordinate. Requires y; mutually exclusive with guid."},
        "y": {"type": "number", "description": "Target Y coordinate. Requires x; mutually exclusive with guid."},
    }
    required = ()  # exactly one of guid or (x and y) — enforced in check(), not expressible as a flat "required" list

    def check(self, session, world, guid: int | None = None, x: float | None = None,
              y: float | None = None, **_) -> str | None:
        if guid is None and (x is None or y is None):
            return "face needs either guid or both x and y"
        if guid is not None and (x is not None or y is not None):
            return "face takes either guid or x/y, not both"
        if session.player_position is None:
            return "own position unknown"
        if guid is not None:
            target = world.get_object(guid)
            if target is None or target.position is None:
                return f"guid {guid:#x} has no known position"
        return None

    def execute(self, session, world, guid: int | None = None, x: float | None = None,
                y: float | None = None, **_) -> ActionResult:
        _, my_x, my_y, my_z, _my_o = session.player_position
        if guid is not None:
            target = world.get_object(guid)
            _, tx, ty, _tz, _ = target.position
        else:
            tx, ty = x, y
        orientation = math.atan2(ty - my_y, tx - my_x) % (2 * math.pi)

        movement.send_set_facing(session, my_x, my_y, my_z, orientation)
        # The server doesn't echo MSG_MOVE_* back to the sender, so mirror
        # the new orientation locally — matches how session.py mirrors self
        # position/stats from perception for cheap access (_sync_self_from_block).
        map_id = session.player_position[0]
        session.player_position = (map_id, my_x, my_y, my_z, orientation)

        return ActionResult(ok=True, detail={"orientation": orientation})


@register
class MoveToAction(Action):
    name = "move_to"
    description = ("Walk in a straight line to a point on the ground, stopping within "
                    "stop_distance yards. No pathfinding — obstacles will block it (returns "
                    "ok=False, error='stuck'). Blocks until arrival, stuck, or stopped.")
    params = {
        "x": {"type": "number", "description": "Destination X coordinate."},
        "y": {"type": "number", "description": "Destination Y coordinate."},
        "z": {"type": "number", "description": "Destination Z coordinate. Optional — "
                                                 "omit to stay level with the current height (no terrain data in v1)."},
        "stop_distance": {"type": "number", "description": "How close counts as arrived, in yards. Default 1.0."},
    }
    required = ("x", "y")

    def check(self, session, world, x: float, y: float, z: float | None = None,
              stop_distance: float = movement.ARRIVE_STOP_DISTANCE_YD, **_) -> str | None:
        if session.player_position is None:
            return "own position unknown"
        return None

    def execute(self, session, world, x: float, y: float, z: float | None = None,
                stop_distance: float = movement.ARRIVE_STOP_DISTANCE_YD, **_) -> ActionResult:
        _pause_follow_reflex(session, world)
        mover = movement.get_mover(session, world)
        result = mover.move_to(x, y, z, stop_distance=stop_distance)
        return ActionResult(**result)


@register
class MoveTowardsAction(Action):
    name = "move_towards"
    description = ("Chase a nearby unit or player by handle, re-targeting its position every "
                    "tick, stopping within stop_distance yards. Blocks until arrival, stuck, "
                    "the target leaving perception, or stopped.")
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the object to move towards."},
        "stop_distance": {"type": "number", "description": "How close counts as arrived, in yards. Default 1.0."},
    }
    required = ("guid",)

    def check(self, session, world, guid: int,
              stop_distance: float = movement.ARRIVE_STOP_DISTANCE_YD, **_) -> str | None:
        if session.player_position is None:
            return "own position unknown"
        target = world.get_object(guid)
        if target is None or target.position is None:
            return f"guid {guid:#x} has no known position"
        return None

    def execute(self, session, world, guid: int,
                stop_distance: float = movement.ARRIVE_STOP_DISTANCE_YD, **_) -> ActionResult:
        _pause_follow_reflex(session, world)
        mover = movement.get_mover(session, world)
        result = mover.move_towards(guid, stop_distance=stop_distance)
        return ActionResult(**result)


@register
class StopMovementAction(Action):
    name = "stop_movement"
    description = "Stop any in-progress move_to/move_towards immediately."
    params = {}
    required = ()

    def execute(self, session, world, **_) -> ActionResult:
        _pause_follow_reflex(session, world)
        mover = movement.get_mover(session, world)
        was_moving = mover.stop()
        return ActionResult(ok=True, detail={"was_moving": was_moving})
