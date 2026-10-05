"""Party/group list and decline packets (GH-71). The invite itself is in handlers/chat.py."""

import logging
import struct

from .. import group as grp
from .. import packets as pk
from ..opcodes import (
    SMSG_GROUP_DECLINE,
    SMSG_GROUP_DESTROYED,
    SMSG_GROUP_LIST,
    SMSG_GROUP_UNINVITE,
)
from ..router import ROUTER

log = logging.getLogger("agent.session")


def _left_group(state):
    state.group = None
    state.record_event("group_left")


def handle_group_list(ctx, payload: bytes):
    try:
        group = grp.parse_group_list(payload)
    except (struct.error, ValueError, IndexError):
        log.warning("unparseable SMSG_GROUP_LIST (%d bytes)", len(payload))
        return
    if group is None:
        _left_group(ctx.state)
        return
    ctx.state.group = group
    ctx.state.pending_invite = None  # we are in a group, so any invite was accepted
    ctx.state.record_event("group_updated", members=len(group["members"]))


def handle_group_gone(ctx, payload: bytes):
    # SMSG_GROUP_UNINVITE / SMSG_GROUP_DESTROYED: empty payloads.
    _left_group(ctx.state)


def handle_group_decline(ctx, payload: bytes):
    name, _ = pk.cstring(payload, 0)
    ctx.state.record_event("group_invite_declined", target_name=name)


ROUTER.register_all({
    SMSG_GROUP_LIST: handle_group_list,
    SMSG_GROUP_UNINVITE: handle_group_gone,
    SMSG_GROUP_DESTROYED: handle_group_gone,
    SMSG_GROUP_DECLINE: handle_group_decline,
})
