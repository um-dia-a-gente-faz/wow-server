"""The `pending_invite` and `group` sections of the snapshot, and the group
membership the nearby players are marked against (#368)."""

from typing import Any

from .. import group as group_mod


def group_section(group: dict[str, Any] | None, my_guid: int,
                  pending_invite: Any) -> dict[str, Any]:
    return {"pending_invite": pending_invite, "group": group_mod.snapshot_view(group, my_guid)}


def group_member_guids(group: dict[str, Any] | None) -> set[int]:
    """Guids of the other group members (empty when ungrouped)."""
    return {m["guid"] for m in (group or {}).get("members", ())}
