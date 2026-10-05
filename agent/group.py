"""Party/group state (GH-71): parsing SMSG_GROUP_LIST and shaping it for the
snapshot. Stdlib only.

Wire format verified against TrinityCore 3.3.5, src/server/game/Groups/Group.cpp,
Group::SendUpdateToPlayer (the full list) and Group::RemoveMember /
Group::Disband (the "you are no longer in a group" form):

    uint8  group_type            GroupType flags (Group.h): 0x02 raid, 0x08 LFG, 0x10 = left
    uint8  subgroup
    uint8  member_flags          GroupMemberFlags (assistant/main tank/main assist)
    uint8  roles
    [if group_type & GROUPTYPE_LFG (0x08): uint8 lfg_state, uint32 lfg_dungeon]
    uint64 group_guid            raw 8-byte ObjectGuid, not packed
    uint32 counter
    uint32 member_count          EXCLUDING the receiving player
    member_count x { cstring name, uint64 guid, uint8 online, uint8 subgroup,
                     uint8 flags, uint8 roles }
    uint64 leader_guid
    if member_count:
        uint8 loot_method, uint64 master_looter_guid, uint8 loot_threshold,
        uint8 dungeon_difficulty, uint8 raid_difficulty, uint8 heroic_flag

The leave form is `0x10 0 0 0, guid, counter, uint32 0, uint64 0` (no loot tail),
which parses to a zero leader — that is how "not in a group" is recognised.
"""

import struct

from agent import packets as pk

GROUPTYPE_RAID = 0x02
GROUPTYPE_LFG = 0x08

MEMBER_STATUS_ONLINE = 0x01  # GroupMemberOnlineStatus (Group.h)

# LootMethod (src/server/game/Loot/Loot.h)
LOOT_METHOD_NAMES = {0: "free_for_all", 1: "round_robin", 2: "master_loot",
                     3: "group_loot", 4: "need_before_greed"}


def parse_group_list(payload: bytes) -> dict | None:
    """Decode SMSG_GROUP_LIST. Returns None for the "left the group" form
    (zero leader); otherwise a dict with raw guids (the snapshot turns them
    into handles) and the member list. Raises struct.error/IndexError on a
    truncated payload — the caller logs and keeps the previous state."""
    group_type, _subgroup, _flags, _roles = struct.unpack_from("<BBBB", payload, 0)
    off = 4
    if group_type & GROUPTYPE_LFG:
        off += 5  # uint8 state + uint32 dungeon
    group_guid, counter, count = struct.unpack_from("<QII", payload, off)
    off += 16
    members = []
    for _ in range(count):
        name, off = pk.cstring(payload, off)
        guid, online, subgroup, mflags, roles = struct.unpack_from("<QBBBB", payload, off)
        off += 12
        members.append({"name": name, "guid": guid, "online": bool(online & MEMBER_STATUS_ONLINE),
                        "subgroup": subgroup, "assistant": bool(mflags & 0x01), "roles": roles})
    (leader_guid,) = struct.unpack_from("<Q", payload, off)
    off += 8
    if not leader_guid:
        return None
    group = {"guid": group_guid, "counter": counter, "raid": bool(group_type & GROUPTYPE_RAID),
             "leader_guid": leader_guid, "members": members, "loot_method": None,
             "loot_threshold": None, "master_looter_guid": None}
    if count:
        loot, master, threshold = struct.unpack_from("<BQB", payload, off)
        group["loot_method"] = LOOT_METHOD_NAMES.get(loot, f"loot_{loot}")
        group["loot_threshold"] = threshold
        group["master_looter_guid"] = master or None
    return group


def snapshot_view(group: dict | None, my_guid: int) -> dict | None:
    """The `group` entry of snapshot(): None when ungrouped, else leader,
    loot method and members (the receiving player is not in the packet's
    member list, so it is not in `members` either)."""
    if not group:
        return None
    leader_guid = group["leader_guid"]
    leader_name = next((m["name"] for m in group["members"] if m["guid"] == leader_guid), None)
    return {
        "is_leader": leader_guid == my_guid,
        "leader_guid": leader_guid,
        "leader_name": leader_name,  # None when the agent itself leads
        "raid": group["raid"],
        "loot_method": group["loot_method"],
        "members": [dict(m) for m in group["members"]],
    }
