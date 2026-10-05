"""NPC interaction packets (UM-40): gossip, vendors, trainers."""

import struct

from .. import npc as npc_mod
from .. import perception as per
from ..router import ROUTER


def handle_gossip_message(ctx, payload: bytes):
    try:
        data = npc_mod.parse_gossip_message(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_GOSSIP_MESSAGE ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_gossip_message(data)


def handle_gossip_complete(ctx, payload: bytes):
    ctx.state.world_state.apply_gossip_complete()


def handle_npc_text_update(ctx, payload: bytes):
    try:
        data = npc_mod.parse_npc_text_update(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_NPC_TEXT_UPDATE ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_npc_text_update(data)


def handle_list_inventory(ctx, payload: bytes):
    try:
        data = npc_mod.parse_list_inventory(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_LIST_INVENTORY ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_list_inventory(data)


def handle_trainer_list(ctx, payload: bytes):
    try:
        data = npc_mod.parse_trainer_list(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_TRAINER_LIST ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_trainer_list(data)


def handle_buy_item(ctx, payload: bytes):
    """SMSG_BUY_ITEM (0x1A4): a passive nearby broadcast of the vendor's
    updated stock slot(s) after any successful purchase (by anyone
    nearby, not necessarily us) — not a personal purchase ack. We still
    parse and record it (useful for updating vendor stock/quantity), but
    BuyItemAction relies on the absence of SMSG_BUY_FAILED, not on this
    event, to decide success."""
    try:
        data = npc_mod.parse_buy_item(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_BUY_ITEM ({len(payload)} B): {e}") from e
    ctx.state.record_event("buy_item", **data)


def handle_buy_failed(ctx, payload: bytes):
    try:
        data = npc_mod.parse_buy_failed(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_BUY_FAILED ({len(payload)} B): {e}") from e
    ctx.state.record_event("buy_failed", **data)


def handle_sell_item(ctx, payload: bytes):
    try:
        data = npc_mod.parse_sell_item(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_SELL_ITEM ({len(payload)} B): {e}") from e
    ctx.state.record_event("sell_item", **data)


def handle_train_succeeded(ctx, payload: bytes):
    try:
        data = npc_mod.parse_trainer_buy_succeeded(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_TRAINER_BUY_SUCCEEDED ({len(payload)} B): {e}") from e
    ctx.state.record_event("train_succeeded", **data)


def handle_train_failed(ctx, payload: bytes):
    try:
        data = npc_mod.parse_trainer_buy_failed(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_TRAINER_BUY_FAILED ({len(payload)} B): {e}") from e
    ctx.state.record_event("train_failed", **data)


def send_npc_text_queries(ctx):
    """UM-40: send whatever agent.perception.WorldState.npc_texts has
    queued, like _send_name_queries — called once per recv-loop tick."""
    for text_id, guid in ctx.state.world_state.npc_texts.drain():
        ctx.send(npc_mod.CMSG_NPC_TEXT_QUERY, npc_mod.build_npc_text_query(text_id, guid))


ROUTER.register_all({
    npc_mod.SMSG_GOSSIP_MESSAGE: handle_gossip_message,
    npc_mod.SMSG_GOSSIP_COMPLETE: handle_gossip_complete,
    npc_mod.SMSG_NPC_TEXT_UPDATE: handle_npc_text_update,
    npc_mod.SMSG_LIST_INVENTORY: handle_list_inventory,
    npc_mod.SMSG_TRAINER_LIST: handle_trainer_list,
    npc_mod.SMSG_BUY_ITEM: handle_buy_item,
    npc_mod.SMSG_BUY_FAILED: handle_buy_failed,
    npc_mod.SMSG_SELL_ITEM: handle_sell_item,
    npc_mod.SMSG_TRAINER_BUY_SUCCEEDED: handle_train_succeeded,
    npc_mod.SMSG_TRAINER_BUY_FAILED: handle_train_failed,
})

ROUTER.on_tick(send_npc_text_queries)
