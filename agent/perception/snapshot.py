"""Composes the JSON-serialisable snapshot the LLM sees from the per-section builders."""

from .. import group as group_mod
from ..model import Snapshot
from .nearby import nearby_sections, position_dict


def compose_snapshot(view: dict, my_position, max_range, limit, corpse_position,
                     pending_invite, chat_inbox, group) -> Snapshot:
    """`view` is what WorldState.snapshot() copied out under its lock: `objects`
    (list), `me`, `my_guid`, `window`, `trade`, `mailbox`, `has_new_mail`,
    `channels`, `equipment`, `inventory`, `quest_log`. Returns the raw dict
    (GUIDs not yet turned into handles)."""
    me = view["me"]
    pos = my_position or (me.position if me else None)
    out: Snapshot = {
        "position": position_dict(pos),
        "is_dead": bool(me is not None and me.is_dead()),
        "is_ghost": bool(me is not None and me.is_ghost()),
        "corpse_position": position_dict(corpse_position) if corpse_position is not None else None,
        "nearby_units": [],
        "nearby_players": [],
        "nearby_objects": [],
        "window": view["window"],
        "trade": view["trade"],
        "mailbox": view["mailbox"],
        "has_new_mail": view["has_new_mail"],
        "equipment": view["equipment"],
        "inventory": view["inventory"],
        "pending_invite": pending_invite,
        "group": group_mod.snapshot_view(group, view["my_guid"]),
        "chat_inbox": list(chat_inbox) if chat_inbox is not None else [],
        "channels": view["channels"],  # UM-93: joined chat channels, for channel_say
        "quest_log": view["quest_log"],
    }
    if pos is not None:
        group_guids = {m["guid"] for m in (group or {}).get("members", ())}
        near = nearby_sections(view["objects"], view["my_guid"], pos, max_range, limit, group_guids)
        out["nearby_units"] = near["nearby_units"]
        out["nearby_players"] = near["nearby_players"]
        out["nearby_objects"] = near["nearby_objects"]
    return out
