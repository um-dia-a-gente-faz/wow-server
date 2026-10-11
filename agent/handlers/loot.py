"""Loot and item packets (UM-42)."""

from .. import loot as lo


def handle_loot_response(ctx, payload: bytes):
    data = lo.parse_loot_response(payload)
    ctx.state.loot = data
    ctx.state.record_event("loot_response", **data)


def handle_loot_release_response(ctx, payload: bytes):
    data = lo.parse_loot_release_response(payload)
    if ctx.state.loot is not None and ctx.state.loot.get("guid") == data["guid"]:
        ctx.state.loot = None
    ctx.state.record_event("loot_release", **data)


def handle_loot_removed(ctx, payload: bytes):
    data = lo.parse_loot_removed(payload)
    if ctx.state.loot is not None:
        ctx.state.loot["items"] = [i for i in ctx.state.loot.get("items", []) if i["slot"] != data["slot"]]
    ctx.state.record_event("loot_removed", **data)


def handle_loot_money_notify(ctx, payload: bytes):
    data = lo.parse_loot_money_notify(payload)
    if ctx.state.loot is not None:
        ctx.state.loot["coins"] = 0
    ctx.state.record_event("loot_money", **data)


def handle_item_push_result(ctx, payload: bytes):
    data = lo.parse_item_push_result(payload)
    ctx.state.record_event("item_received", **data)


def handle_inventory_change_failure(ctx, payload: bytes):
    data = lo.parse_inventory_change_failure(payload)
    ctx.state.record_event("inventory_change_failure", **data)


def handle_item_query_response(ctx, payload: bytes):
    data = lo.parse_item_query_response(payload)
    ctx.state.world_state.apply_item_query_response(data)


def send_item_queries(ctx):
    """UM-42: same idea as _send_name_queries, for
    agent.perception.WorldState.items (item template cache)."""
    for entry in ctx.state.world_state.items.drain():
        ctx.send(lo.CMSG_ITEM_QUERY_SINGLE, lo.build_item_query(entry))


def register(router):
    router.register_all({
        lo.SMSG_LOOT_RESPONSE: handle_loot_response,
        lo.SMSG_LOOT_RELEASE_RESPONSE: handle_loot_release_response,
        lo.SMSG_LOOT_REMOVED: handle_loot_removed,
        lo.SMSG_LOOT_MONEY_NOTIFY: handle_loot_money_notify,
        lo.SMSG_ITEM_PUSH_RESULT: handle_item_push_result,
        lo.SMSG_INVENTORY_CHANGE_FAILURE: handle_inventory_change_failure,
        lo.SMSG_ITEM_QUERY_SINGLE_RESPONSE: handle_item_query_response,
    })

    router.on_tick(send_item_queries)
