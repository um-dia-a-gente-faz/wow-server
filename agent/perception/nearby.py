"""The `nearby_units` / `nearby_players` / `nearby_objects` sections of the snapshot."""

from ..model import Position, Unit
from .objects import ObjectInfo


def position_dict(pos) -> Position | None:
    if pos is None:
        return None
    map_id, x, y, z, *_o = pos
    return {"map": map_id, "x": x, "y": y, "z": z}


def object_dict(obj: ObjectInfo, distance: float) -> Unit:
    # in_combat is set below, after the optional keys, to keep the emitted key order.
    d: Unit = {  # type: ignore[typeddict-item]
        "guid": obj.guid,
        "entry": obj.entry,
        "name": obj.name or None,
        "type": obj.object_type or None,
        "distance": round(distance, 1),
    }
    current_position = position_dict(obj.current_position())
    if current_position is not None:
        d["position"] = current_position
    if obj.faction is not None:
        d["faction"] = obj.faction
    if obj.level is not None:
        d["level"] = obj.level
    if obj.health is not None and obj.max_health:
        d["health_pct"] = round(obj.health / obj.max_health, 2) if obj.max_health else None
    if obj.target_guid is not None:
        d["target_guid"] = obj.target_guid
    if obj.quest_giver_status is not None:
        d["quest_giver_status"] = obj.quest_giver_status_name or obj.quest_giver_status
    d["in_combat"] = bool(obj.unit_flags and (obj.unit_flags & 0x00080000))  # UNIT_FLAG_IN_COMBAT, UnitDefines.h
    # UM-97: only set when true/non-zero, so existing snapshots (and the
    # pruned LLM prompt) are unchanged for ordinary units. agent/candidates.py
    # reads these to offer loot and to keep service NPCs out of attack options.
    if obj.is_lootable():
        d["lootable"] = True
    if obj.npc_flags:
        d["npc_flags"] = obj.npc_flags
    return d


def nearby_sections(objects, my_guid, pos, max_range, limit, group_guids) -> dict[str, list[Unit]]:
    """Perceived objects within `max_range` of `pos`, nearest first, split by
    kind and capped at `limit` each. A nearby player is marked `in_group` when
    their guid is in `group_guids`."""
    out: dict[str, list[Unit]] = {"nearby_units": [], "nearby_players": [], "nearby_objects": []}
    scored = []
    for obj in objects:
        if obj.guid == my_guid:
            continue
        dist = obj.distance_to(pos)
        if dist is None or dist > max_range:
            continue
        scored.append((dist, obj))
    scored.sort(key=lambda pair: pair[0])

    for dist, obj in scored:
        entry = object_dict(obj, dist)
        if obj.object_type == "player":
            entry["in_group"] = obj.guid in group_guids
            bucket = out["nearby_players"]
        elif obj.object_type == "unit":
            bucket = out["nearby_units"]
        else:
            bucket = out["nearby_objects"]
        if len(bucket) < limit:
            bucket.append(entry)
    return out
