"""Composes the JSON-serialisable snapshot the LLM sees from the per-section builders."""

from ..model import Snapshot
from .caps import SECTION_CAPS, apply_caps
from .group_state import group_member_guids, group_section
from .nearby import nearby_sections
from .self_state import resolved_position, self_section


def compose_snapshot(view: dict, my_position, max_range, limit, corpse_position,
                     pending_invite, chat_inbox, group, zone=None) -> Snapshot:
    """`view` is what WorldState.snapshot() copied out under its lock: `objects`
    (list), `me`, `my_guid`, `window`, `trade`, `mailbox`, `has_new_mail`,
    `channels`, `equipment`, `inventory`, `quest_log`. Returns the raw dict
    (GUIDs not yet turned into handles)."""
    me = view["me"]
    pos = resolved_position(me, my_position)
    out: Snapshot = {
        **self_section(me, my_position, corpse_position, zone),
        "nearby_units": [],
        "nearby_players": [],
        "nearby_objects": [],
        "window": view["window"],
        "trade": view["trade"],
        "mailbox": view["mailbox"],
        "has_new_mail": view["has_new_mail"],
        "equipment": view["equipment"],
        "inventory": view["inventory"],
        **group_section(group, view["my_guid"], pending_invite),
        "chat_inbox": list(chat_inbox) if chat_inbox is not None else [],
        "channels": view["channels"],  # UM-93: joined chat channels, for channel_say
        "quest_log": view["quest_log"],
    }
    if pos is not None:
        near = nearby_sections(view["objects"], view["my_guid"], pos, max_range, limit,
                               group_member_guids(group))
        out["nearby_units"] = near["nearby_units"]
        out["nearby_players"] = near["nearby_players"]
        out["nearby_objects"] = near["nearby_objects"]
    # The nearby_* sections are already cut to `limit` by nearby_sections, so they are not capped here.
    caps = {k: v for k, v in SECTION_CAPS.items() if not k.startswith("nearby_")}
    return apply_caps(out, caps)
