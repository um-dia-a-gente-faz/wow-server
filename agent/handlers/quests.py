"""Quest packets (UM-41)."""

import struct

from .. import perception as per
from .. import quests as qu
from ..router import ROUTER


def handle_quest_query_response(ctx, payload: bytes):
    try:
        data = qu.parse_quest_query_response(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_QUEST_QUERY_RESPONSE ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_quest_query_response(data)


def handle_questgiver_status(ctx, payload: bytes):
    try:
        data = qu.parse_questgiver_status(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_STATUS ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_questgiver_status(data)


def handle_questgiver_quest_list(ctx, payload: bytes):
    try:
        data = qu.parse_questgiver_quest_list(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_QUEST_LIST ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_questgiver_quest_list(data)


def handle_questgiver_quest_details(ctx, payload: bytes):
    try:
        data = qu.parse_questgiver_quest_details(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_QUEST_DETAILS ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_questgiver_quest_details(data)


def handle_questgiver_request_items(ctx, payload: bytes):
    try:
        data = qu.parse_questgiver_request_items(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_REQUEST_ITEMS ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_questgiver_request_items(data)


def handle_questgiver_offer_reward(ctx, payload: bytes):
    try:
        data = qu.parse_questgiver_offer_reward(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_OFFER_REWARD ({len(payload)} B): {e}") from e
    ctx.state.world_state.apply_questgiver_offer_reward(data)


def handle_questgiver_quest_complete(ctx, payload: bytes):
    try:
        data = qu.parse_questgiver_quest_complete(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_QUEST_COMPLETE ({len(payload)} B): {e}") from e
    ctx.state.world_state.close_window()
    ctx.state.record_event("quest_turned_in", **data)


def handle_questgiver_quest_failed(ctx, payload: bytes):
    try:
        data = qu.parse_questgiver_quest_failed(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_QUEST_FAILED ({len(payload)} B): {e}") from e
    ctx.state.record_event("quest_failed", **data)


def handle_questupdate_add_kill(ctx, payload: bytes):
    try:
        data = qu.parse_questupdate_add_kill(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_QUESTUPDATE_ADD_KILL ({len(payload)} B): {e}") from e
    ctx.state.record_event("quest_progress", objective="kill", **data)


def handle_questupdate_add_item(ctx, payload: bytes):
    try:
        data = qu.parse_questupdate_add_item(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_QUESTUPDATE_ADD_ITEM ({len(payload)} B): {e}") from e
    ctx.state.record_event("quest_progress", objective="item", **data)


def handle_questupdate_complete(ctx, payload: bytes):
    try:
        data = qu.parse_questupdate_complete(payload)
    except (IndexError, struct.error) as e:
        raise per.PerceptionParseError(f"malformed SMSG_QUESTUPDATE_COMPLETE ({len(payload)} B): {e}") from e
    ctx.state.record_event("quest_complete", **data)


def send_quest_queries(ctx):
    """UM-41: send whatever agent.perception.WorldState.quest_texts has
    queued, like _send_name_queries/_send_npc_text_queries — called
    once per recv-loop tick."""
    for quest_id, guid in ctx.state.world_state.quest_texts.drain():
        ctx.send(qu.CMSG_QUEST_QUERY, qu.build_quest_query(quest_id, guid))
    for guid in ctx.state.world_state.drain_quest_giver_status_queries():
        ctx.send(qu.CMSG_QUESTGIVER_STATUS_QUERY, qu.build_questgiver_status_query(guid))


ROUTER.register_all({
    qu.SMSG_QUEST_QUERY_RESPONSE: handle_quest_query_response,
    qu.SMSG_QUESTGIVER_STATUS: handle_questgiver_status,
    qu.SMSG_QUESTGIVER_QUEST_LIST: handle_questgiver_quest_list,
    qu.SMSG_QUESTGIVER_QUEST_DETAILS: handle_questgiver_quest_details,
    qu.SMSG_QUESTGIVER_REQUEST_ITEMS: handle_questgiver_request_items,
    qu.SMSG_QUESTGIVER_OFFER_REWARD: handle_questgiver_offer_reward,
    qu.SMSG_QUESTGIVER_QUEST_COMPLETE: handle_questgiver_quest_complete,
    qu.SMSG_QUESTGIVER_QUEST_FAILED: handle_questgiver_quest_failed,
    qu.SMSG_QUESTUPDATE_ADD_KILL: handle_questupdate_add_kill,
    qu.SMSG_QUESTUPDATE_ADD_ITEM: handle_questupdate_add_item,
    qu.SMSG_QUESTUPDATE_COMPLETE: handle_questupdate_complete,
})

ROUTER.on_tick(send_quest_queries)
