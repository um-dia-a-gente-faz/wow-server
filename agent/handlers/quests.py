"""Quest packets (UM-41)."""

from .. import quests as qu


def handle_quest_query_response(ctx, payload: bytes):
    data = qu.parse_quest_query_response(payload)
    ctx.state.world_state.apply_quest_query_response(data)


def handle_questgiver_status(ctx, payload: bytes):
    data = qu.parse_questgiver_status(payload)
    ctx.state.world_state.apply_questgiver_status(data)


def handle_questgiver_quest_list(ctx, payload: bytes):
    data = qu.parse_questgiver_quest_list(payload)
    ctx.state.world_state.apply_questgiver_quest_list(data)


def handle_questgiver_quest_details(ctx, payload: bytes):
    data = qu.parse_questgiver_quest_details(payload)
    ctx.state.world_state.apply_questgiver_quest_details(data)


def handle_questgiver_request_items(ctx, payload: bytes):
    data = qu.parse_questgiver_request_items(payload)
    ctx.state.world_state.apply_questgiver_request_items(data)


def handle_questgiver_offer_reward(ctx, payload: bytes):
    data = qu.parse_questgiver_offer_reward(payload)
    ctx.state.world_state.apply_questgiver_offer_reward(data)


def handle_questgiver_quest_complete(ctx, payload: bytes):
    data = qu.parse_questgiver_quest_complete(payload)
    ctx.state.world_state.close_window()
    ctx.state.record_event("quest_turned_in", **data)
    # ADR 0020 (#390). SMSG_QUESTGIVER_QUEST_COMPLETE goes to the turner-in only: TrinityCore 92127cd,
    # Player::RewardQuest (Entities/Player/Player.cpp:14860) calls SendQuestReward (:14993), which
    # writes the packet and sends it with SendDirectMessage (:16546-16553).
    ctx.state.emit_activity("quest", quest_id=data["quest_id"])
    if data["money_reward"] > 0:  # the reward money is the agent's own (ADR 0020 slice 4)
        ctx.state.emit_activity("money", copper=data["money_reward"])


def handle_questgiver_quest_failed(ctx, payload: bytes):
    data = qu.parse_questgiver_quest_failed(payload)
    ctx.state.record_event("quest_failed", **data)


def handle_questupdate_add_kill(ctx, payload: bytes):
    data = qu.parse_questupdate_add_kill(payload)
    ctx.state.record_event("quest_progress", objective="kill", **data)


def handle_questupdate_add_item(ctx, payload: bytes):
    data = qu.parse_questupdate_add_item(payload)
    ctx.state.record_event("quest_progress", objective="item", **data)


def handle_questupdate_complete(ctx, payload: bytes):
    data = qu.parse_questupdate_complete(payload)
    ctx.state.record_event("quest_complete", **data)


def send_quest_queries(ctx):
    """UM-41: send whatever agent.perception.WorldState.quest_texts has
    queued, like _send_name_queries/_send_npc_text_queries — called
    once per recv-loop tick."""
    for quest_id, guid in ctx.state.world_state.quest_texts.drain():
        ctx.send(qu.CMSG_QUEST_QUERY, qu.build_quest_query(quest_id, guid))
    for guid in ctx.state.world_state.drain_quest_giver_status_queries():
        ctx.send(qu.CMSG_QUESTGIVER_STATUS_QUERY, qu.build_questgiver_status_query(guid))


def register(router):
    router.register_all({
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

    router.on_tick(send_quest_queries)
