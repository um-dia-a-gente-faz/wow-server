"""The `position`, `is_dead`, `is_ghost` and `corpse_position` sections of the snapshot (#368)."""

from typing import Any

from .nearby import position_dict
from .objects import ObjectInfo


def resolved_position(me: ObjectInfo | None, my_position: tuple | None) -> tuple | None:
    """The position the snapshot reports for the agent: `my_position` when the
    caller passes one (right after login), else the self object's own position."""
    return my_position or (me.position if me else None)


def self_section(me: ObjectInfo | None, my_position: tuple | None,
                 corpse_position: tuple | None) -> dict[str, Any]:
    pos = resolved_position(me, my_position)
    return {
        "position": position_dict(pos),
        "is_dead": bool(me is not None and me.is_dead()),
        "is_ghost": bool(me is not None and me.is_ghost()),
        "corpse_position": position_dict(corpse_position) if corpse_position is not None else None,
    }
