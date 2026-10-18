"""The `position`, `is_dead`, `is_ghost` and `corpse_position` sections of the snapshot (#368)."""

from typing import TypedDict

from ..model import Position
from .nearby import position_dict
from .objects import ObjectInfo


class SelfSection(TypedDict):
    position: Position | None
    is_dead: bool
    is_ghost: bool
    corpse_position: Position | None


def resolved_position(me: ObjectInfo | None, my_position: tuple | None) -> tuple | None:
    """The position the snapshot reports for the agent: `my_position` when the
    caller passes one (right after login), else the self object's own position."""
    return my_position or (me.position if me else None)


def self_section(me: ObjectInfo | None, my_position: tuple | None,
                 corpse_position: tuple | None, zone: int | None = None) -> SelfSection:
    pos = resolved_position(me, my_position)
    position = position_dict(pos)
    if position is not None and zone is not None:
        position["zone"] = zone   # #316: session state (SMSG_INIT_WORLD_STATES); absent until the server sends a zone
    return {
        "position": position,
        "is_dead": bool(me is not None and me.is_dead()),
        "is_ghost": bool(me is not None and me.is_ghost()),
        "corpse_position": position_dict(corpse_position) if corpse_position is not None else None,
    }
