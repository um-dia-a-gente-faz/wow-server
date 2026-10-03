#!/usr/bin/env python3
"""WoW session manager — login, keepalive, packet dispatch.
Wraps the low-level world protocol in a background recv loop."""

import collections
import logging
import os
import socket
import struct
import time
import threading
import zlib

from . import packets as pk
from . import channels as ch_mod
from . import crypt as cr
from . import loot as lo
from . import mail as mail_mod
from . import names as nm
from . import npc as npc_mod
from . import perception as per
from . import quests as qu
from . import spells as sp
from . import trade as trade_mod
from . import update_fields as uo_fields
from . import update_object as uo
from .update_object import (
    UPDATETYPE_VALUES,
    UPDATETYPE_MOVEMENT,
    UPDATETYPE_CREATE_OBJECT,
    UPDATETYPE_CREATE_OBJECT2,
    UPDATETYPE_OUT_OF_RANGE_OBJECTS,
    UPDATETYPE_NEAR_OBJECTS,
)

log = logging.getLogger("agent.session")

# Opcodes (TrinityCore 3.3.5 Opcodes.h)
SMSG_AUTH_CHALLENGE     = 0x1EC
CMSG_AUTH_SESSION       = 0x1ED
SMSG_AUTH_RESPONSE      = 0x1EE
CMSG_CHAR_CREATE        = 0x036
SMSG_CHAR_CREATE        = 0x03A
CHAR_CREATE_SUCCESS     = 47  # SharedDefines.h ResponseCodes (0x2F)
CHAR_CREATE_NAME_IN_USE = 50  # 0x32
CMSG_CHAR_ENUM          = 0x037
SMSG_CHAR_ENUM          = 0x03B
CMSG_PLAYER_LOGIN       = 0x03D
CMSG_SET_ACTIVE_MOVER   = 0x26A
CMSG_LOGOUT_REQUEST     = 0x04B
CMSG_PING               = 0x1DC
SMSG_PONG               = 0x1DD
SMSG_LOGIN_VERIFY_WORLD = 0x236
SMSG_ADDON_INFO         = 0x2EF
SMSG_TIME_SYNC_REQ      = 0x390
CMSG_TIME_SYNC_RESP     = 0x391
CMSG_KEEP_ALIVE         = 0x407
SMSG_TUTORIAL_FLAGS     = 0x0FD

SMSG_UPDATE_OBJECT      = 0x0A9
SMSG_DESTROY_OBJECT     = 0x0AA
SMSG_COMPRESSED_UPDATE_OBJECT = 0x1F6
SMSG_LOGOUT_COMPLETE    = 0x04D
SMSG_MONSTER_MOVE       = 0x0DD
SMSG_STANDSTATE_UPDATE  = 0x29D

# Death/resurrection (UM-43). Verified against TrinityCore branch `3.3.5`:
#   src/server/game/Server/Protocol/Opcodes.h
#   src/server/game/Server/Packets/MiscPackets.h/.cpp (RepopRequest,
#     ReclaimCorpse, CorpseReclaimDelay, DeathReleaseLoc)
#   src/server/game/Server/Packets/QueryPackets.h/.cpp (CorpseLocation —
#     MSG_CORPSE_QUERY is the same opcode both ways: an empty client request,
#     a populated server response)
#   src/server/game/Handlers/MiscHandler.cpp (HandleRepopRequest,
#     HandleReclaimCorpse), src/server/game/Handlers/NPCHandler.cpp
#     (HandleSpiritHealerActivateOpcode)
#   src/server/game/Entities/Player/Player.h (enum PlayerFlags —
#     PLAYER_FLAGS_GHOST = 0x10; CORPSE_RECLAIM_RADIUS = 39)
CMSG_REPOP_REQUEST          = 0x15A  # payload: uint8 CheckInstance (always 0 from a real client)
CMSG_RECLAIM_CORPSE         = 0x1D2  # payload: uint64 CorpseGUID (raw, not packed — ObjectGuid::operator>>)
CMSG_SPIRIT_HEALER_ACTIVATE = 0x21C  # payload: uint64 guid (raw)
MSG_CORPSE_QUERY            = 0x216  # client->server: empty; server->client: see _handle_corpse_query_response
SMSG_CORPSE_RECLAIM_DELAY   = 0x269  # payload: uint32 Remaining (ms)
SMSG_DEATH_RELEASE_LOC      = 0x378  # payload: int32 MapID, float x, y, z

# MSG_MOVE_* broadcasts of another unit's movement (Opcodes.cpp:
# &WorldSession::HandleMovementOpcodes, WorldPackets::Movement::MoveUpdate) —
# packed guid + MovementInfo, no speeds/spline (agent.update_object.
# parse_movement_info). Excludes MSG_MOVE_TELEPORT/TELEPORT_ACK (different,
# ack-specific payload; UM-38) and cheat/rare opcodes not sent by a normal
# client.
MSG_MOVE_START_FORWARD      = 0x0B5
MSG_MOVE_START_BACKWARD     = 0x0B6
MSG_MOVE_STOP                = 0x0B7
MSG_MOVE_START_STRAFE_LEFT  = 0x0B8
MSG_MOVE_START_STRAFE_RIGHT = 0x0B9
MSG_MOVE_STOP_STRAFE        = 0x0BA
MSG_MOVE_JUMP                = 0x0BB
MSG_MOVE_START_TURN_LEFT    = 0x0BC
MSG_MOVE_START_TURN_RIGHT   = 0x0BD
MSG_MOVE_STOP_TURN          = 0x0BE
MSG_MOVE_SET_RUN_MODE       = 0x0C2
MSG_MOVE_SET_WALK_MODE      = 0x0C3
MSG_MOVE_FALL_LAND          = 0x0C9
MSG_MOVE_START_SWIM         = 0x0CA
MSG_MOVE_STOP_SWIM          = 0x0CB
MSG_MOVE_SET_FACING         = 0x0DA
MSG_MOVE_HEARTBEAT          = 0x0EE

MSG_MOVE_OPCODES = frozenset((
    MSG_MOVE_START_FORWARD, MSG_MOVE_START_BACKWARD, MSG_MOVE_STOP,
    MSG_MOVE_START_STRAFE_LEFT, MSG_MOVE_START_STRAFE_RIGHT, MSG_MOVE_STOP_STRAFE,
    MSG_MOVE_JUMP, MSG_MOVE_START_TURN_LEFT, MSG_MOVE_START_TURN_RIGHT, MSG_MOVE_STOP_TURN,
    MSG_MOVE_SET_RUN_MODE, MSG_MOVE_SET_WALK_MODE, MSG_MOVE_FALL_LAND,
    MSG_MOVE_START_SWIM, MSG_MOVE_STOP_SWIM, MSG_MOVE_SET_FACING, MSG_MOVE_HEARTBEAT,
))

SMSG_GROUP_INVITE       = 0x06F
SMSG_MESSAGECHAT        = 0x096
SMSG_GM_MESSAGECHAT     = 0x3B3

# Whisper/group-invite failure acks (UM-68 bugfix). Verified against
# TrinityCore branch `3.3.5`:
#   src/server/game/Server/Protocol/Opcodes.h
#   src/server/game/Handlers/ChatHandler.cpp (WorldSession::
#     SendPlayerNotFoundNotice — sent when the whisper target is
#     offline/unresolvable; payload is just the target name)
#   src/server/game/Handlers/GroupHandler.cpp (WorldSession::SendPartyResult,
#     used by HandleGroupInviteOpcode on both success (ERR_PARTY_RESULT_OK)
#     and failure (e.g. ERR_BAD_PLAYER_NAME_S, ERR_ALREADY_IN_GROUP_S) —
#     WhisperAction/InviteToGroupAction only treat a non-OK `result` as a
#     failure, per the SendPartyResult call sites)
SMSG_CHAT_PLAYER_NOT_FOUND = 0x2A9
SMSG_PARTY_COMMAND_RESULT  = 0x07F

# PartyResult (Group.h) — only the values SendPartyResult actually uses from
# HandleGroupInviteOpcode; anything else falls back to f"result_{n}".
PARTY_RESULT_NAMES = {
    0: "ok", 1: "bad_player_name", 2: "target_not_in_group",
    3: "target_not_in_instance", 4: "group_full", 5: "already_in_group",
    6: "not_in_group", 7: "not_leader", 8: "player_wrong_faction",
    9: "ignoring_you", 13: "invite_restricted", 19: "invite_in_combat",
}
ERR_PARTY_RESULT_OK = 0

# Name resolution (UM-35)
CMSG_NAME_QUERY               = 0x050
SMSG_NAME_QUERY_RESPONSE      = 0x051
CMSG_GAMEOBJECT_QUERY         = 0x05E
SMSG_GAMEOBJECT_QUERY_RESPONSE = 0x05F
CMSG_CREATURE_QUERY           = 0x060
SMSG_CREATURE_QUERY_RESPONSE  = 0x061

# NPC interaction (UM-40) — see agent/npc.py for citations.
SMSG_GOSSIP_MESSAGE        = npc_mod.SMSG_GOSSIP_MESSAGE
SMSG_GOSSIP_COMPLETE       = npc_mod.SMSG_GOSSIP_COMPLETE
CMSG_NPC_TEXT_QUERY        = npc_mod.CMSG_NPC_TEXT_QUERY
SMSG_NPC_TEXT_UPDATE       = npc_mod.SMSG_NPC_TEXT_UPDATE
SMSG_LIST_INVENTORY        = npc_mod.SMSG_LIST_INVENTORY
SMSG_TRAINER_LIST          = npc_mod.SMSG_TRAINER_LIST
SMSG_BUY_ITEM              = npc_mod.SMSG_BUY_ITEM
SMSG_BUY_FAILED            = npc_mod.SMSG_BUY_FAILED
SMSG_SELL_ITEM             = npc_mod.SMSG_SELL_ITEM
SMSG_TRAINER_BUY_SUCCEEDED = npc_mod.SMSG_TRAINER_BUY_SUCCEEDED
SMSG_TRAINER_BUY_FAILED    = npc_mod.SMSG_TRAINER_BUY_FAILED

# Quests (UM-41) — see agent/quests.py for citations/caveats.
CMSG_QUEST_QUERY               = qu.CMSG_QUEST_QUERY
SMSG_QUEST_QUERY_RESPONSE      = qu.SMSG_QUEST_QUERY_RESPONSE
CMSG_QUESTGIVER_STATUS_QUERY   = qu.CMSG_QUESTGIVER_STATUS_QUERY
SMSG_QUESTGIVER_STATUS         = qu.SMSG_QUESTGIVER_STATUS
CMSG_QUESTGIVER_HELLO          = qu.CMSG_QUESTGIVER_HELLO
SMSG_QUESTGIVER_QUEST_LIST     = qu.SMSG_QUESTGIVER_QUEST_LIST
CMSG_QUESTGIVER_QUERY_QUEST    = qu.CMSG_QUESTGIVER_QUERY_QUEST
SMSG_QUESTGIVER_QUEST_DETAILS  = qu.SMSG_QUESTGIVER_QUEST_DETAILS
CMSG_QUESTGIVER_ACCEPT_QUEST   = qu.CMSG_QUESTGIVER_ACCEPT_QUEST
CMSG_QUESTGIVER_COMPLETE_QUEST = qu.CMSG_QUESTGIVER_COMPLETE_QUEST
SMSG_QUESTGIVER_REQUEST_ITEMS  = qu.SMSG_QUESTGIVER_REQUEST_ITEMS
CMSG_QUESTGIVER_REQUEST_REWARD = qu.CMSG_QUESTGIVER_REQUEST_REWARD
SMSG_QUESTGIVER_OFFER_REWARD   = qu.SMSG_QUESTGIVER_OFFER_REWARD
CMSG_QUESTGIVER_CHOOSE_REWARD  = qu.CMSG_QUESTGIVER_CHOOSE_REWARD
SMSG_QUESTGIVER_QUEST_COMPLETE = qu.SMSG_QUESTGIVER_QUEST_COMPLETE
SMSG_QUESTGIVER_QUEST_FAILED   = qu.SMSG_QUESTGIVER_QUEST_FAILED
CMSG_QUESTLOG_REMOVE_QUEST     = qu.CMSG_QUESTLOG_REMOVE_QUEST
SMSG_QUESTUPDATE_COMPLETE      = qu.SMSG_QUESTUPDATE_COMPLETE
SMSG_QUESTUPDATE_ADD_KILL      = qu.SMSG_QUESTUPDATE_ADD_KILL
SMSG_QUESTUPDATE_ADD_ITEM      = qu.SMSG_QUESTUPDATE_ADD_ITEM

# Loot / inventory (UM-42)
CMSG_USE_ITEM                   = lo.CMSG_USE_ITEM
CMSG_DESTROYITEM                = lo.CMSG_DESTROYITEM
SMSG_INVENTORY_CHANGE_FAILURE   = lo.SMSG_INVENTORY_CHANGE_FAILURE
CMSG_AUTOSTORE_LOOT_ITEM        = lo.CMSG_AUTOSTORE_LOOT_ITEM
CMSG_LOOT                       = lo.CMSG_LOOT
CMSG_LOOT_MONEY                 = lo.CMSG_LOOT_MONEY
CMSG_LOOT_RELEASE                = lo.CMSG_LOOT_RELEASE
SMSG_LOOT_RESPONSE               = lo.SMSG_LOOT_RESPONSE
SMSG_LOOT_RELEASE_RESPONSE       = lo.SMSG_LOOT_RELEASE_RESPONSE
SMSG_LOOT_REMOVED                = lo.SMSG_LOOT_REMOVED
SMSG_LOOT_MONEY_NOTIFY           = lo.SMSG_LOOT_MONEY_NOTIFY
SMSG_ITEM_PUSH_RESULT            = lo.SMSG_ITEM_PUSH_RESULT
CMSG_ITEM_QUERY_SINGLE           = lo.CMSG_ITEM_QUERY_SINGLE
SMSG_ITEM_QUERY_SINGLE_RESPONSE  = lo.SMSG_ITEM_QUERY_SINGLE_RESPONSE

# Trade (UM-59)
CMSG_INITIATE_TRADE        = trade_mod.CMSG_INITIATE_TRADE
CMSG_BEGIN_TRADE           = trade_mod.CMSG_BEGIN_TRADE
CMSG_BUSY_TRADE            = trade_mod.CMSG_BUSY_TRADE
CMSG_IGNORE_TRADE          = trade_mod.CMSG_IGNORE_TRADE
CMSG_ACCEPT_TRADE          = trade_mod.CMSG_ACCEPT_TRADE
CMSG_UNACCEPT_TRADE        = trade_mod.CMSG_UNACCEPT_TRADE
CMSG_CANCEL_TRADE          = trade_mod.CMSG_CANCEL_TRADE
CMSG_SET_TRADE_ITEM        = trade_mod.CMSG_SET_TRADE_ITEM
CMSG_CLEAR_TRADE_ITEM      = trade_mod.CMSG_CLEAR_TRADE_ITEM
CMSG_SET_TRADE_GOLD        = trade_mod.CMSG_SET_TRADE_GOLD
SMSG_TRADE_STATUS          = trade_mod.SMSG_TRADE_STATUS
SMSG_TRADE_STATUS_EXTENDED = trade_mod.SMSG_TRADE_STATUS_EXTENDED

# Mailbox (UM-60)
CMSG_SEND_MAIL         = mail_mod.CMSG_SEND_MAIL
SMSG_SEND_MAIL_RESULT  = mail_mod.SMSG_SEND_MAIL_RESULT
CMSG_GET_MAIL_LIST     = mail_mod.CMSG_GET_MAIL_LIST
SMSG_MAIL_LIST_RESULT  = mail_mod.SMSG_MAIL_LIST_RESULT
CMSG_MAIL_TAKE_MONEY   = mail_mod.CMSG_MAIL_TAKE_MONEY
CMSG_MAIL_TAKE_ITEM    = mail_mod.CMSG_MAIL_TAKE_ITEM
CMSG_MAIL_MARK_AS_READ = mail_mod.CMSG_MAIL_MARK_AS_READ
CMSG_MAIL_DELETE       = mail_mod.CMSG_MAIL_DELETE
SMSG_RECEIVED_MAIL     = mail_mod.SMSG_RECEIVED_MAIL

# ChatMsg (uint8) — the full enum, src/server/shared/SharedDefines.h. UM-44's
# prompt builder treats *_LEADER/RAID/PARTY/GUILD/WHISPER/name-mention kinds
# as addressed to the agent and everything else (including monster_* other
# than monster_whisper) as background chatter — see docs/AGENT-DIRECTION.md
# "Communication happens in game only" > anti-loop rule 4.
CHAT_MSG_SYSTEM = 0x00
CHAT_MSG_SAY = 0x01
CHAT_MSG_PARTY = 0x02
CHAT_MSG_RAID = 0x03
CHAT_MSG_GUILD = 0x04
CHAT_MSG_OFFICER = 0x05
CHAT_MSG_YELL = 0x06
CHAT_MSG_WHISPER = 0x07
CHAT_MSG_WHISPER_FOREIGN = 0x08
CHAT_MSG_WHISPER_INFORM = 0x09
CHAT_MSG_EMOTE = 0x0A
CHAT_MSG_TEXT_EMOTE = 0x0B
CHAT_MSG_MONSTER_SAY = 0x0C
CHAT_MSG_MONSTER_PARTY = 0x0D
CHAT_MSG_MONSTER_YELL = 0x0E
CHAT_MSG_MONSTER_WHISPER = 0x0F
CHAT_MSG_MONSTER_EMOTE = 0x10

# Combat / spells (UM-39)
SMSG_INITIAL_SPELLS         = 0x12A
SMSG_LEARNED_SPELL          = 0x12B
CMSG_CAST_SPELL             = 0x12E
SMSG_CAST_FAILED            = 0x130
SMSG_SPELL_START            = 0x131
SMSG_SPELL_GO                = 0x132
SMSG_ATTACK_START           = 0x143
SMSG_ATTACK_STOP            = 0x144
SMSG_ATTACKERSTATEUPDATE    = 0x14A
SMSG_LOG_XPGAIN             = 0x1D0
SMSG_LEVELUP_INFO           = 0x1D4
SMSG_PARTYKILLLOG           = 0x1F5
SMSG_REMOVED_SPELL          = 0x203
SMSG_SPELLNONMELEEDAMAGELOG = 0x250

# Bounded so a long fight or a busy session can't grow this without limit —
# the LLM loop (UM-44) only ever needs the last N events per think cycle.
EVENTS_MAXLEN = 100

# opcode -> WoWSession handler method name, for the combat/spell opcodes
# above (kept as a table instead of a long elif chain — see _dispatch).
_SPELL_DISPATCH = {
    SMSG_INITIAL_SPELLS: "_handle_initial_spells",
    SMSG_LEARNED_SPELL: "_handle_learned_spell",
    SMSG_REMOVED_SPELL: "_handle_removed_spell",
    SMSG_CAST_FAILED: "_handle_cast_failed",
    SMSG_SPELL_START: "_handle_spell_start",
    SMSG_SPELL_GO: "_handle_spell_go",
    SMSG_ATTACK_START: "_handle_attack_start",
    SMSG_ATTACK_STOP: "_handle_attack_stop",
    SMSG_ATTACKERSTATEUPDATE: "_handle_attacker_state_update",
    SMSG_SPELLNONMELEEDAMAGELOG: "_handle_spell_non_melee_damage_log",
    SMSG_PARTYKILLLOG: "_handle_party_kill_log",
    SMSG_LOG_XPGAIN: "_handle_log_xp_gain",
    SMSG_LEVELUP_INFO: "_handle_levelup_info",
}

# NPC interaction opcodes (UM-40) -> handler method name.
_NPC_DISPATCH = {
    SMSG_GOSSIP_MESSAGE: "_handle_gossip_message",
    SMSG_GOSSIP_COMPLETE: "_handle_gossip_complete",
    SMSG_NPC_TEXT_UPDATE: "_handle_npc_text_update",
    SMSG_LIST_INVENTORY: "_handle_list_inventory",
    SMSG_TRAINER_LIST: "_handle_trainer_list",
    SMSG_BUY_ITEM: "_handle_buy_item",
    SMSG_BUY_FAILED: "_handle_buy_failed",
    SMSG_SELL_ITEM: "_handle_sell_item",
    SMSG_TRAINER_BUY_SUCCEEDED: "_handle_train_succeeded",
    SMSG_TRAINER_BUY_FAILED: "_handle_train_failed",
}

# opcode -> WoWSession handler method name, for the loot/item opcodes (UM-42).
_LOOT_DISPATCH = {
    SMSG_LOOT_RESPONSE: "_handle_loot_response",
    SMSG_LOOT_RELEASE_RESPONSE: "_handle_loot_release_response",
    SMSG_LOOT_REMOVED: "_handle_loot_removed",
    SMSG_LOOT_MONEY_NOTIFY: "_handle_loot_money_notify",
    SMSG_ITEM_PUSH_RESULT: "_handle_item_push_result",
    SMSG_INVENTORY_CHANGE_FAILURE: "_handle_inventory_change_failure",
    SMSG_ITEM_QUERY_SINGLE_RESPONSE: "_handle_item_query_response",
}

# Quest opcodes (UM-41) -> handler method name.
_QUEST_DISPATCH = {
    SMSG_QUEST_QUERY_RESPONSE: "_handle_quest_query_response",
    SMSG_QUESTGIVER_STATUS: "_handle_questgiver_status",
    SMSG_QUESTGIVER_QUEST_LIST: "_handle_questgiver_quest_list",
    SMSG_QUESTGIVER_QUEST_DETAILS: "_handle_questgiver_quest_details",
    SMSG_QUESTGIVER_REQUEST_ITEMS: "_handle_questgiver_request_items",
    SMSG_QUESTGIVER_OFFER_REWARD: "_handle_questgiver_offer_reward",
    SMSG_QUESTGIVER_QUEST_COMPLETE: "_handle_questgiver_quest_complete",
    SMSG_QUESTGIVER_QUEST_FAILED: "_handle_questgiver_quest_failed",
    SMSG_QUESTUPDATE_ADD_KILL: "_handle_questupdate_add_kill",
    SMSG_QUESTUPDATE_ADD_ITEM: "_handle_questupdate_add_item",
    SMSG_QUESTUPDATE_COMPLETE: "_handle_questupdate_complete",
}

# Trade opcodes (UM-59) -> handler method name.
_TRADE_DISPATCH = {
    SMSG_TRADE_STATUS: "_handle_trade_status",
    SMSG_TRADE_STATUS_EXTENDED: "_handle_trade_status_extended",
}

# Mailbox opcodes (UM-60) -> handler method name.
_MAIL_DISPATCH = {
    SMSG_SEND_MAIL_RESULT: "_handle_send_mail_result",
    SMSG_MAIL_LIST_RESULT: "_handle_mail_list_result",
    SMSG_RECEIVED_MAIL: "_handle_received_mail",
}

CHAT_MSG_CHANNEL = 0x11
CHAT_MSG_CHANNEL_JOIN = 0x12
CHAT_MSG_CHANNEL_LEAVE = 0x13
CHAT_MSG_CHANNEL_LIST = 0x14
CHAT_MSG_CHANNEL_NOTICE = 0x15
CHAT_MSG_CHANNEL_NOTICE_USER = 0x16
CHAT_MSG_AFK = 0x17
CHAT_MSG_DND = 0x18
CHAT_MSG_IGNORED = 0x19
CHAT_MSG_SKILL = 0x1A
CHAT_MSG_LOOT = 0x1B
CHAT_MSG_MONEY = 0x1C
CHAT_MSG_OPENING = 0x1D
CHAT_MSG_TRADESKILLS = 0x1E
CHAT_MSG_PET_INFO = 0x1F
CHAT_MSG_COMBAT_MISC_INFO = 0x20
CHAT_MSG_COMBAT_XP_GAIN = 0x21
CHAT_MSG_COMBAT_HONOR_GAIN = 0x22
CHAT_MSG_COMBAT_FACTION_CHANGE = 0x23
CHAT_MSG_BG_SYSTEM_NEUTRAL = 0x24
CHAT_MSG_BG_SYSTEM_ALLIANCE = 0x25
CHAT_MSG_BG_SYSTEM_HORDE = 0x26
CHAT_MSG_RAID_LEADER = 0x27
CHAT_MSG_RAID_WARNING = 0x28
CHAT_MSG_RAID_BOSS_EMOTE = 0x29
CHAT_MSG_RAID_BOSS_WHISPER = 0x2A
CHAT_MSG_FILTERED = 0x2B
CHAT_MSG_BATTLEGROUND = 0x2C
CHAT_MSG_BATTLEGROUND_LEADER = 0x2D
CHAT_MSG_RESTRICTED = 0x2E
CHAT_MSG_BATTLENET = 0x2F
CHAT_MSG_ACHIEVEMENT = 0x30
CHAT_MSG_GUILD_ACHIEVEMENT = 0x31
CHAT_MSG_ARENA_POINTS = 0x32
CHAT_MSG_PARTY_LEADER = 0x33
CHAT_MSG_ADDON = 0xFF

CHAT_KIND_NAMES = {
    CHAT_MSG_SYSTEM: "system", CHAT_MSG_SAY: "say", CHAT_MSG_PARTY: "party",
    CHAT_MSG_RAID: "raid", CHAT_MSG_GUILD: "guild", CHAT_MSG_OFFICER: "officer",
    CHAT_MSG_YELL: "yell", CHAT_MSG_WHISPER: "whisper",
    CHAT_MSG_WHISPER_FOREIGN: "whisper_foreign", CHAT_MSG_WHISPER_INFORM: "whisper_inform",
    CHAT_MSG_EMOTE: "emote", CHAT_MSG_TEXT_EMOTE: "text_emote",
    CHAT_MSG_MONSTER_SAY: "monster_say", CHAT_MSG_MONSTER_PARTY: "monster_party",
    CHAT_MSG_MONSTER_YELL: "monster_yell", CHAT_MSG_MONSTER_WHISPER: "monster_whisper",
    CHAT_MSG_MONSTER_EMOTE: "monster_emote", CHAT_MSG_CHANNEL: "channel",
    CHAT_MSG_CHANNEL_JOIN: "channel_join", CHAT_MSG_CHANNEL_LEAVE: "channel_leave",
    CHAT_MSG_CHANNEL_LIST: "channel_list", CHAT_MSG_CHANNEL_NOTICE: "channel_notice",
    CHAT_MSG_CHANNEL_NOTICE_USER: "channel_notice_user", CHAT_MSG_AFK: "afk",
    CHAT_MSG_DND: "dnd", CHAT_MSG_IGNORED: "ignored", CHAT_MSG_SKILL: "skill",
    CHAT_MSG_LOOT: "loot", CHAT_MSG_MONEY: "money", CHAT_MSG_OPENING: "opening",
    CHAT_MSG_TRADESKILLS: "tradeskills", CHAT_MSG_PET_INFO: "pet_info",
    CHAT_MSG_COMBAT_MISC_INFO: "combat_misc_info", CHAT_MSG_COMBAT_XP_GAIN: "combat_xp_gain",
    CHAT_MSG_COMBAT_HONOR_GAIN: "combat_honor_gain",
    CHAT_MSG_COMBAT_FACTION_CHANGE: "combat_faction_change",
    CHAT_MSG_BG_SYSTEM_NEUTRAL: "bg_system_neutral", CHAT_MSG_BG_SYSTEM_ALLIANCE: "bg_system_alliance",
    CHAT_MSG_BG_SYSTEM_HORDE: "bg_system_horde", CHAT_MSG_RAID_LEADER: "raid_leader",
    CHAT_MSG_RAID_WARNING: "raid_warning", CHAT_MSG_RAID_BOSS_EMOTE: "raid_boss_emote",
    CHAT_MSG_RAID_BOSS_WHISPER: "raid_boss_whisper", CHAT_MSG_FILTERED: "filtered",
    CHAT_MSG_BATTLEGROUND: "battleground", CHAT_MSG_BATTLEGROUND_LEADER: "battleground_leader",
    CHAT_MSG_RESTRICTED: "restricted", CHAT_MSG_BATTLENET: "battlenet",
    CHAT_MSG_ACHIEVEMENT: "achievement", CHAT_MSG_GUILD_ACHIEVEMENT: "guild_achievement",
    CHAT_MSG_ARENA_POINTS: "arena_points", CHAT_MSG_PARTY_LEADER: "party_leader",
    CHAT_MSG_ADDON: "addon",
}

# WorldPackets::Chat::Chat::Write (ChatPackets.cpp) branches on SlashCmd for
# what comes between the (sender_guid, flags) header and the chat text —
# everything not listed here uses the "default" branch (optional GM sender
# name, optional channel name, then a bare target_guid).
CHAT_KINDS_MONSTER = frozenset((
    CHAT_MSG_MONSTER_SAY, CHAT_MSG_MONSTER_PARTY, CHAT_MSG_MONSTER_YELL,
    CHAT_MSG_MONSTER_WHISPER, CHAT_MSG_MONSTER_EMOTE,
    CHAT_MSG_RAID_BOSS_EMOTE, CHAT_MSG_RAID_BOSS_WHISPER, CHAT_MSG_BATTLENET,
))
CHAT_KINDS_BG_SYSTEM = frozenset((CHAT_MSG_BG_SYSTEM_NEUTRAL, CHAT_MSG_BG_SYSTEM_ALLIANCE, CHAT_MSG_BG_SYSTEM_HORDE))
CHAT_KINDS_ACHIEVEMENT = frozenset((CHAT_MSG_ACHIEVEMENT, CHAT_MSG_GUILD_ACHIEVEMENT))

# ObjectGuid::HighGuid (ObjectGuid.h) — the high 16 bits of a raw (unpacked)
# 64-bit GUID. Only need enough of the enum to replicate IsPlayer()/IsPet()
# for the monster/bg-system branches' conditional target name.
_HIGHGUID_PLAYER = 0x0000
_HIGHGUID_PET = 0xF140


def _guid_is_player(guid: int) -> bool:
    return guid != 0 and (guid >> 48) == _HIGHGUID_PLAYER


def _guid_is_pet(guid: int) -> bool:
    return (guid >> 48) == _HIGHGUID_PET

# Bounded so a chatty channel can't grow this without limit.
CHAT_INBOX_MAXLEN = 50

# Once a packet's first byte has arrived, the rest must follow within this long.
# A timeout mid-packet would desync framing and RC4 state, so it ends the session.
MID_PACKET_TIMEOUT_S = 30.0
# Handler errors are logged at most once per opcode per this many seconds.
ERROR_LOG_INTERVAL_S = 30.0


def _read_len_string(data: bytes, off: int) -> tuple[str, int]:
    """A uint32-length-prefixed string (length includes the trailing null),
    as ChatPackets.cpp writes SenderName/ChatText — distinct from
    agent.packets.cstring's null-scan, which is for the plain cstrings used
    elsewhere in the same packet (e.g. channel name)."""
    length = struct.unpack_from('<I', data, off)[0]; off += 4
    s = data[off:off + length - 1].decode('utf-8', 'replace') if length > 0 else ""
    off += length
    return s, off


class _ErrorThrottle:
    """Rate-limits log lines per key, counting the ones it suppresses."""

    def __init__(self, interval: float, clock=time.monotonic):
        self.interval = interval
        self._clock = clock
        self._last = {}        # key -> time of last emitted line
        self._suppressed = {}  # key -> errors swallowed since then

    def check(self, key) -> tuple[bool, int]:
        """Returns (should_log, suppressed_since_last_log)."""
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self.interval:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False, 0
        self._last[key] = now
        return True, self._suppressed.pop(key, 0)


class WoWSession:
    """Manages a single character's World of Warcraft session."""

    def __init__(self, host: str, port: int, account_name: str,
                 session_key: bytes, realm_id: int, verbose_packets: bool = False,
                 dump_packets_dir: str = ""):
        self.host = host
        self.port = port
        self.account_name = account_name
        self.session_key = session_key
        self.realm_id = realm_id
        self.verbose_packets = verbose_packets
        self.dump_packets_dir = dump_packets_dir
        self.sock = None
        self.crypt = None
        self._recv_thread = None
        self._running = False
        self._in_world = False
        self._lock = threading.Lock()
        self._error_throttle = _ErrorThrottle(ERROR_LOG_INTERVAL_S)
        self.dropped_packets = 0  # packets whose handler raised

        # Game state
        self.player_guid = 0
        self.player_name = ""
        self.player_position = None  # (map_id, x, y, z, orient)
        self.level = 0
        self.race = 0  # ChrRaces.dbc ID; set by callers (e.g. __main__.py) from enum_characters()
        self.class_ = 0  # ChrClasses.dbc ID (UM-69); set by callers from enum_characters(), same as race
        self.xp = None
        self.next_level_xp = None
        self.coinage = None
        self.world_state = per.WorldState()  # nearby objects, players, etc.
        self.on_update_object = None  # callback(update_type, guid, fields)
        self.chat_inbox = collections.deque(maxlen=CHAT_INBOX_MAXLEN)
        # UM-47: optional agent.chat_relay.ChatRelay, set by the caller
        # (agent/__main__.py) — mirrors heard chat to tools/chat-feed.
        self.chat_relay = None
        self.pending_invite = None  # {"inviter_name": str} or None
        self.spellbook: set[int] = set()  # known spell IDs (UM-39)
        self.spell_cooldowns: dict[int, dict] = {}  # spell_id -> agent.spells.parse_initial_spells' cooldown entry shape
        self.events = collections.deque(maxlen=EVENTS_MAXLEN)  # combat/XP events, shaped like {"kind": str, ...}
        self.loot: dict | None = None  # current open loot session (agent.loot.parse_loot_response()'s dict), or None

        # Death/resurrection (UM-43)
        self.corpse_position = None       # (map_id, x, y, z) from MSG_CORPSE_QUERY's response, or None
        self.corpse_reclaim_ready_at = None  # time.monotonic() deadline from SMSG_CORPSE_RECLAIM_DELAY, or None
        self.graveyard_position = None    # (map_id, x, y, z) from SMSG_DEATH_RELEASE_LOC, informational only
        self._was_alive = True            # tracks health>0 -> 0 for the 'death' event (see _sync_self_from_block)

        # Reconnect supervisor (UM-43, agent/__main__.py)
        self._logout_requested = False    # set by logout() — distinguishes a clean stop from a forced one
        self.unexpected_disconnect = False  # set when the recv thread/socket dies without a requested logout

    def connect(self):
        """Connect to world server and authenticate."""
        self.sock = socket.create_connection((self.host, self.port), 15)
        self.sock.settimeout(30)

        # Read SMSG_AUTH_CHALLENGE (first packet, unencrypted)
        hdr = self._rr(4)
        size = struct.unpack('>H', hdr[:2])[0]
        opcode = struct.unpack('<H', hdr[2:4])[0]
        assert opcode == SMSG_AUTH_CHALLENGE
        payload = self._rr(size - 2)
        server_challenge = payload[4:8]

        # Build CMSG_AUTH_SESSION
        import os, hashlib, hmac
        local_challenge = os.urandom(4)
        digest = pk.sha1(self.account_name.encode(), b'\x00'*4, local_challenge,
                          server_challenge, self.session_key)
        pl = struct.pack('<I', 12340) + struct.pack('<I', 0)
        pl += self.account_name.encode('ascii') + b'\x00'
        pl += struct.pack('<I', 0)  # LoginServerType
        pl += local_challenge
        pl += struct.pack('<I', 1)  # RegionID
        pl += struct.pack('<I', 0)  # BattlegroupID
        pl += struct.pack('<I', self.realm_id)
        pl += struct.pack('<Q', 0)  # DosResponse
        pl += digest
        pl += struct.pack('<I', 0) + b'\x00'  # addon info

        # Send unencrypted CMSG_AUTH_SESSION
        self.sock.sendall(
            struct.pack('>H', len(pl) + 4) + struct.pack('<I', CMSG_AUTH_SESSION) + pl
        )

        # Init crypto
        self.crypt = cr.WorldCrypt(self.session_key)

        # Read auth response
        opcode, payload = self._recv_packet()
        assert opcode == SMSG_AUTH_RESPONSE
        result = payload[0]
        if result != 0x0C:
            raise RuntimeError(f"World auth failed: {result:#x}")

        # Drain pre-login packets
        while True:
            opcode, payload = self._recv_packet()
            if opcode == SMSG_TIME_SYNC_REQ:
                self._send_sync(payload)
            elif opcode == SMSG_TUTORIAL_FLAGS:
                break

        return True

    def create_character(self, name: str, race: int, class_: int, gender: int = 0,
                          skin: int = 0, face: int = 0, hair_style: int = 0,
                          hair_color: int = 0, facial_hair: int = 0) -> int:
        """Create a fresh level-1 character (CMSG_CHAR_CREATE; UM-55).

        Payload layout verified against TrinityCore 3.3.5a
        WorldPackets::Character::CreateCharacter::Read() (CharacterPackets.cpp):
        null-terminated name, then one uint8 each for race, class, gender
        (Sex), skin, face, hair style, hair color, facial hair, outfit id
        (always 0, deprecated). Response is SMSG_CHAR_CREATE with a single
        uint8 result code (SharedDefines.h ResponseCodes); raises on
        anything but CHAR_CREATE_SUCCESS (47).
        """
        payload = (
            name.encode() + b'\x00'
            + bytes([race, class_, gender, skin, face, hair_style, hair_color, facial_hair])
            + b'\x00'  # OutfitId, deprecated
        )
        self._send_packet(CMSG_CHAR_CREATE, payload)
        opcode, resp = self._recv_packet()
        if opcode != SMSG_CHAR_CREATE:
            raise RuntimeError(f"char create failed for {name!r}: unexpected opcode {opcode:#x}")
        code = resp[0]
        if code != CHAR_CREATE_SUCCESS:
            raise RuntimeError(f"char create failed for {name!r}: response code {code}")
        return code

    def enum_characters(self) -> list[dict]:
        """List characters on the realm. Returns [{'guid':..., 'name':...}, ...]."""
        self._send_packet(CMSG_CHAR_ENUM)
        opcode, payload = self._recv_packet()
        assert opcode == SMSG_CHAR_ENUM

        count = payload[0]
        off = 1
        chars = []
        for _ in range(count):
            guid = struct.unpack_from('<Q', payload, off)[0]; off += 8
            name = payload[off:payload.index(0, off)].decode(); off += len(name) + 1
            race = payload[off]; cls = payload[off+1]; gender = payload[off+2]
            off += 3 + 5  # gender + cosmetics
            level = payload[off]; off += 1
            off += 4  # zone
            map_id = struct.unpack_from('<I', payload, off)[0]; off += 4
            x = struct.unpack_from('<f', payload, off)[0]; off += 4
            y = struct.unpack_from('<f', payload, off)[0]; off += 4
            z = struct.unpack_from('<f', payload, off)[0]; off += 4
            off += 4  # guild
            char_flags = struct.unpack_from('<I', payload, off)[0]; off += 4
            if char_flags & 0x08: off += 4
            off += 4 + 12 + 23*4  # rest
            chars.append({
                'guid': guid, 'name': name, 'race': race, 'class_': cls,
                'gender': gender, 'level': level, 'map': map_id,
                'x': x, 'y': y, 'z': z,
            })
        return chars

    def login_character(self, guid: int):
        """Log in a character and start background recv."""
        # Set before any update-object arrives so perception can tell our
        # own blocks from everyone else's.
        self.player_guid = guid
        self.world_state.set_my_guid(guid)
        self.world_state.names.load()  # UM-35: reuse the on-disk creature/gameobject cache, if any
        self._send_packet(CMSG_KEEP_ALIVE)
        self._send_packet(CMSG_PLAYER_LOGIN, struct.pack('<Q', guid))

        t0 = time.monotonic()
        while time.monotonic() - t0 < 60:
            self.sock.settimeout(3)
            try:
                opcode, payload = self._recv_packet()
            except (socket.timeout, TimeoutError):
                self._send_packet(CMSG_KEEP_ALIVE)
                self._send_packet(CMSG_PING, struct.pack('<II', 0, 0))
                continue

            if opcode == SMSG_LOGIN_VERIFY_WORLD:
                self._handle_verify_world(payload)
                # A real client sends this right after login (CMSG_SET_ACTIVE_MOVER,
                # 0x26A) to claim itself as the unit it controls. Without it,
                # WorldSession::ValidateAndGetUnitBeingMoved (MovementHandler.cpp)
                # finds no GameClient::_activelyMovedUnit and silently drops every
                # MSG_MOVE_* we send (face, and later movement in UM-38) — found
                # while live-verifying UM-36's face action.
                self._send_packet(CMSG_SET_ACTIVE_MOVER, struct.pack('<Q', guid))
                self._in_world = True
                self._running = True
                self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
                self._recv_thread.start()
                return True
            if not self._dispatch_guarded(opcode, payload) and self.verbose_packets:
                log.info("pre-login: %#05x (%d B)", opcode, len(payload))

        raise TimeoutError("Login timed out")

    def recv_thread_alive(self) -> bool:
        return self._recv_thread is not None and self._recv_thread.is_alive()

    def logout(self):
        """Graceful logout. Safe to call at any stage, including after the
        recv thread has died."""
        self._logout_requested = True  # a SMSG_LOGOUT_COMPLETE after this is expected, not a drop (UM-43)
        if self.sock is None:
            return
        self._running = False
        if self._recv_thread:
            self._recv_thread.join(timeout=5)
        if self._in_world:
            self._in_world = False
            try:
                self._send_packet(CMSG_LOGOUT_REQUEST)
                self.sock.settimeout(5)
                while True:
                    opcode, payload = self._recv_packet()
                    if opcode == SMSG_LOGOUT_COMPLETE:
                        break
            except Exception:
                pass
        self.sock.close()
        self.sock = None

    def send_chat(self, message: str, channel: str = "say", target: str | None = None):
        """Send a chat message. Channel: 'say', 'yell', 'whisper' (needs
        `target`), 'emote'. Prefer agent.actions.say/yell/whisper/emote
        directly in new code."""
        from . import actions
        actions.send_chat_message(self, message, channel, target=target)

    # ── Internal ──────────────────────────────────────────────

    def _handle_verify_world(self, payload):
        off = 0
        map_id = struct.unpack_from('<i', payload, off)[0]; off += 4
        px = struct.unpack_from('<f', payload, off)[0]; off += 4
        py = struct.unpack_from('<f', payload, off)[0]; off += 4
        pz = struct.unpack_from('<f', payload, off)[0]; off += 4
        orient = struct.unpack_from('<f', payload, off)[0]
        self.player_position = (map_id, px, py, pz, orient)
        self.world_state.set_my_map(map_id)

    def _handle_destroy_object(self, payload: bytes):
        # Object::DestroyForPlayer (Object.cpp): uint64 guid, uint8 onDeath.
        guid = pk.u64(payload, 0)
        self.world_state.remove_guids([guid])

    def _send_name_queries(self):
        """UM-35: send whatever agent.perception.WorldState.names has queued,
        up to its per-second budget. Called once per recv-loop tick (like
        the keepalive below), so new objects get their names resolved
        within a tick or two of showing up."""
        for kind, key, sample_guid in self.world_state.names.drain():
            if kind == "player":
                self._send_packet(CMSG_NAME_QUERY, nm.build_name_query(key))
            elif kind == "creature":
                self._send_packet(CMSG_CREATURE_QUERY, nm.build_creature_query(key, sample_guid))
            elif kind == "gameobject":
                self._send_packet(CMSG_GAMEOBJECT_QUERY, nm.build_gameobject_query(key, sample_guid))

    def _send_item_queries(self):
        """UM-42: same idea as _send_name_queries, for
        agent.perception.WorldState.items (item template cache)."""
        for entry in self.world_state.items.drain():
            self._send_packet(CMSG_ITEM_QUERY_SINGLE, lo.build_item_query(entry))

    def _handle_name_query_response(self, payload: bytes):
        try:
            data = nm.parse_name_query_response(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_NAME_QUERY_RESPONSE ({len(payload)} B): {e}") from e
        self.world_state.apply_name_query_response(data)

    def _handle_creature_query_response(self, payload: bytes):
        try:
            data = nm.parse_creature_query_response(payload)
        except (IndexError, struct.error, ValueError) as e:
            raise per.PerceptionParseError(f"malformed SMSG_CREATURE_QUERY_RESPONSE ({len(payload)} B): {e}") from e
        self.world_state.apply_creature_query_response(data)

    def _handle_gameobject_query_response(self, payload: bytes):
        try:
            data = nm.parse_gameobject_query_response(payload)
        except (IndexError, struct.error, ValueError) as e:
            raise per.PerceptionParseError(f"malformed SMSG_GAMEOBJECT_QUERY_RESPONSE ({len(payload)} B): {e}") from e
        self.world_state.apply_gameobject_query_response(data)

    def _send_npc_text_queries(self):
        """UM-40: send whatever agent.perception.WorldState.npc_texts has
        queued, like _send_name_queries — called once per recv-loop tick."""
        for text_id, guid in self.world_state.npc_texts.drain():
            self._send_packet(CMSG_NPC_TEXT_QUERY, npc_mod.build_npc_text_query(text_id, guid))

    def _handle_gossip_message(self, payload: bytes):
        try:
            data = npc_mod.parse_gossip_message(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_GOSSIP_MESSAGE ({len(payload)} B): {e}") from e
        self.world_state.apply_gossip_message(data)

    def _handle_gossip_complete(self, payload: bytes):
        self.world_state.apply_gossip_complete()

    def _handle_npc_text_update(self, payload: bytes):
        try:
            data = npc_mod.parse_npc_text_update(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_NPC_TEXT_UPDATE ({len(payload)} B): {e}") from e
        self.world_state.apply_npc_text_update(data)

    def _handle_list_inventory(self, payload: bytes):
        try:
            data = npc_mod.parse_list_inventory(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_LIST_INVENTORY ({len(payload)} B): {e}") from e
        self.world_state.apply_list_inventory(data)

    def _handle_trainer_list(self, payload: bytes):
        try:
            data = npc_mod.parse_trainer_list(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_TRAINER_LIST ({len(payload)} B): {e}") from e
        self.world_state.apply_trainer_list(data)

    def _handle_buy_item(self, payload: bytes):
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
        self._record_event("buy_item", **data)

    def _handle_buy_failed(self, payload: bytes):
        try:
            data = npc_mod.parse_buy_failed(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_BUY_FAILED ({len(payload)} B): {e}") from e
        self._record_event("buy_failed", **data)

    def _handle_sell_item(self, payload: bytes):
        try:
            data = npc_mod.parse_sell_item(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_SELL_ITEM ({len(payload)} B): {e}") from e
        self._record_event("sell_item", **data)

    def _handle_train_succeeded(self, payload: bytes):
        try:
            data = npc_mod.parse_trainer_buy_succeeded(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_TRAINER_BUY_SUCCEEDED ({len(payload)} B): {e}") from e
        self._record_event("train_succeeded", **data)

    def _handle_train_failed(self, payload: bytes):
        try:
            data = npc_mod.parse_trainer_buy_failed(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_TRAINER_BUY_FAILED ({len(payload)} B): {e}") from e
        self._record_event("train_failed", **data)

    def _handle_item_query_response(self, payload: bytes):
        try:
            data = lo.parse_item_query_response(payload)
        except (IndexError, struct.error, ValueError) as e:
            raise per.PerceptionParseError(f"malformed SMSG_ITEM_QUERY_SINGLE_RESPONSE ({len(payload)} B): {e}") from e
        self.world_state.apply_item_query_response(data)

    # ── Quests (UM-41) ─────────────────────────────────────────────────────

    def _send_quest_queries(self):
        """UM-41: send whatever agent.perception.WorldState.quest_texts has
        queued, like _send_name_queries/_send_npc_text_queries — called
        once per recv-loop tick."""
        for quest_id, guid in self.world_state.quest_texts.drain():
            self._send_packet(CMSG_QUEST_QUERY, qu.build_quest_query(quest_id, guid))
        for guid in self.world_state.drain_quest_giver_status_queries():
            self._send_packet(CMSG_QUESTGIVER_STATUS_QUERY, qu.build_questgiver_status_query(guid))

    def _handle_quest_query_response(self, payload: bytes):
        try:
            data = qu.parse_quest_query_response(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_QUEST_QUERY_RESPONSE ({len(payload)} B): {e}") from e
        self.world_state.apply_quest_query_response(data)

    def _handle_questgiver_status(self, payload: bytes):
        try:
            data = qu.parse_questgiver_status(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_STATUS ({len(payload)} B): {e}") from e
        self.world_state.apply_questgiver_status(data)

    def _handle_questgiver_quest_list(self, payload: bytes):
        try:
            data = qu.parse_questgiver_quest_list(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_QUEST_LIST ({len(payload)} B): {e}") from e
        self.world_state.apply_questgiver_quest_list(data)

    def _handle_questgiver_quest_details(self, payload: bytes):
        try:
            data = qu.parse_questgiver_quest_details(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_QUEST_DETAILS ({len(payload)} B): {e}") from e
        self.world_state.apply_questgiver_quest_details(data)

    def _handle_questgiver_request_items(self, payload: bytes):
        try:
            data = qu.parse_questgiver_request_items(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_REQUEST_ITEMS ({len(payload)} B): {e}") from e
        self.world_state.apply_questgiver_request_items(data)

    def _handle_questgiver_offer_reward(self, payload: bytes):
        try:
            data = qu.parse_questgiver_offer_reward(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_OFFER_REWARD ({len(payload)} B): {e}") from e
        self.world_state.apply_questgiver_offer_reward(data)

    def _handle_questgiver_quest_complete(self, payload: bytes):
        try:
            data = qu.parse_questgiver_quest_complete(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_QUEST_COMPLETE ({len(payload)} B): {e}") from e
        self.world_state.close_window()
        self._record_event("quest_turned_in", **data)

    def _handle_questgiver_quest_failed(self, payload: bytes):
        try:
            data = qu.parse_questgiver_quest_failed(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_QUESTGIVER_QUEST_FAILED ({len(payload)} B): {e}") from e
        self._record_event("quest_failed", **data)

    def _handle_questupdate_add_kill(self, payload: bytes):
        try:
            data = qu.parse_questupdate_add_kill(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_QUESTUPDATE_ADD_KILL ({len(payload)} B): {e}") from e
        self._record_event("quest_progress", objective="kill", **data)

    def _handle_questupdate_add_item(self, payload: bytes):
        try:
            data = qu.parse_questupdate_add_item(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_QUESTUPDATE_ADD_ITEM ({len(payload)} B): {e}") from e
        self._record_event("quest_progress", objective="item", **data)

    def _handle_questupdate_complete(self, payload: bytes):
        try:
            data = qu.parse_questupdate_complete(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_QUESTUPDATE_COMPLETE ({len(payload)} B): {e}") from e
        self._record_event("quest_complete", **data)

    # ── Trade (UM-59) ────────────────────────────────────────────────────

    def _handle_trade_status(self, payload: bytes):
        if self.dump_packets_dir:
            self._dump_packet(SMSG_TRADE_STATUS, payload)
        try:
            data = trade_mod.parse_trade_status(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_TRADE_STATUS ({len(payload)} B): {e}") from e
        # Recorded unconditionally (not just when apply_trade_status returns
        # a named event) — actions.py's offer_item/offer_gold/open_trade/
        # accept_trade_request/cancel_trade wait on this raw status_name
        # directly (e.g. "back_to_trade" confirms our own SET_TRADE_ITEM,
        # which never gets its own named event — see agent/trade.py).
        self._record_event("trade_status", **data)
        result = self.world_state.apply_trade_status(data)
        if result is not None:
            kind, fields = result
            self._record_event(kind, **fields)

    def _handle_trade_status_extended(self, payload: bytes):
        if self.dump_packets_dir:
            self._dump_packet(SMSG_TRADE_STATUS_EXTENDED, payload)
        try:
            data = trade_mod.parse_trade_status_extended(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_TRADE_STATUS_EXTENDED ({len(payload)} B): {e}") from e
        changed = self.world_state.apply_trade_status_extended(data)
        if changed is not None:
            self._record_event("trade_offer_changed", **changed)

    # ── Mailbox (UM-60) ─────────────────────────────────────────────────

    def _handle_send_mail_result(self, payload: bytes):
        """Answers send_mail, and both halves of take_mail (money then
        each item) and delete_mail — `data['command']` (MailResponseType)
        says which. Recorded as a raw 'mail_result' event; actions.py
        correlates by mail_id/command and, for send_mail specifically,
        additionally records the ticket-named mail_sent/mail_error event
        once it knows the recipient (not present in this payload)."""
        if self.dump_packets_dir:
            self._dump_packet(SMSG_SEND_MAIL_RESULT, payload)
        try:
            data = mail_mod.parse_send_mail_result(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_SEND_MAIL_RESULT ({len(payload)} B): {e}") from e
        self._record_event("mail_result", **data)

    def _handle_mail_list_result(self, payload: bytes):
        if self.dump_packets_dir:
            self._dump_packet(SMSG_MAIL_LIST_RESULT, payload)
        try:
            data = mail_mod.parse_mail_list_result(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_MAIL_LIST_RESULT ({len(payload)} B): {e}") from e
        self.world_state.apply_mail_list_result(data)

    def _handle_received_mail(self, payload: bytes):
        if self.dump_packets_dir:
            self._dump_packet(SMSG_RECEIVED_MAIL, payload)
        try:
            data = mail_mod.parse_received_mail(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_RECEIVED_MAIL ({len(payload)} B): {e}") from e
        self.world_state.apply_received_mail(data)
        self._record_event("mail_received", **data)

    # ── Loot / inventory (UM-42) ──────────────────────────────────────────

    def _handle_loot_response(self, payload: bytes):
        try:
            data = lo.parse_loot_response(payload)
        except (IndexError, struct.error, ValueError) as e:
            raise per.PerceptionParseError(f"malformed SMSG_LOOT_RESPONSE ({len(payload)} B): {e}") from e
        self.loot = data
        self._record_event("loot_response", **data)

    def _handle_loot_release_response(self, payload: bytes):
        try:
            data = lo.parse_loot_release_response(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_LOOT_RELEASE_RESPONSE ({len(payload)} B): {e}") from e
        if self.loot is not None and self.loot.get("guid") == data["guid"]:
            self.loot = None
        self._record_event("loot_release", **data)

    def _handle_loot_removed(self, payload: bytes):
        data = lo.parse_loot_removed(payload)
        if self.loot is not None:
            self.loot["items"] = [i for i in self.loot.get("items", []) if i["slot"] != data["slot"]]
        self._record_event("loot_removed", **data)

    def _handle_loot_money_notify(self, payload: bytes):
        try:
            data = lo.parse_loot_money_notify(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_LOOT_MONEY_NOTIFY ({len(payload)} B): {e}") from e
        if self.loot is not None:
            self.loot["coins"] = 0
        self._record_event("loot_money", **data)

    def _handle_item_push_result(self, payload: bytes):
        try:
            data = lo.parse_item_push_result(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_ITEM_PUSH_RESULT ({len(payload)} B): {e}") from e
        self._record_event("item_received", **data)

    def _handle_inventory_change_failure(self, payload: bytes):
        try:
            data = lo.parse_inventory_change_failure(payload)
        except (IndexError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_INVENTORY_CHANGE_FAILURE ({len(payload)} B): {e}") from e
        self._record_event("inventory_change_failure", **data)

    def _handle_monster_move(self, payload: bytes):
        """SMSG_MONSTER_MOVE (0x0DD): an NPC's new destination/path (UM-64).
        Starts or replaces that object's spline-interpolation state in
        world_state — see agent/update_object.py::parse_monster_move and
        agent/perception.py::WorldState.apply_monster_move."""
        if self.dump_packets_dir:
            self._dump_packet(SMSG_MONSTER_MOVE, payload)
        try:
            info = uo.parse_monster_move(payload)
        except (IndexError, struct.error, ValueError) as e:
            raise per.PerceptionParseError(
                f"malformed SMSG_MONSTER_MOVE payload ({len(payload)} B): {e}") from e
        self.world_state.apply_monster_move(info)

    def _handle_move_broadcast(self, opcode: int, payload: bytes):
        """A MSG_MOVE_* broadcast of another unit's movement (UM-64): packed
        guid + MovementInfo, no speeds/spline (agent.update_object.
        parse_movement_info) — see MSG_MOVE_OPCODES for which ones this
        covers and why."""
        if self.dump_packets_dir:
            self._dump_packet(opcode, payload)
        try:
            guid, off = pk.unpack_packed_guid(payload, 0)
            move_info, off = uo.parse_movement_info(payload, off)
            if off != len(payload):
                raise ValueError(f"parsed {off} of {len(payload)} bytes ({len(payload) - off} leftover)")
        except (IndexError, struct.error, ValueError) as e:
            raise per.PerceptionParseError(
                f"malformed {opcode:#05x} payload ({len(payload)} B): {e}") from e
        self.world_state.apply_movement_info(guid, move_info)

    def _handle_update_object(self, opcode: int, payload: bytes):
        if opcode == SMSG_COMPRESSED_UPDATE_OBJECT:
            unc_size = pk.u32(payload, 0)
            data = zlib.decompress(payload[4:])
            if len(data) != unc_size:
                raise per.PerceptionParseError(
                    f"inflated to {len(data)} B, header said {unc_size} B")
        else:
            data = payload
        if self.dump_packets_dir:
            self._dump_packet(opcode, data)
        self._parse_update_object(data)

    def _dump_packet(self, opcode: int, data: bytes):
        # A dump failure (disk full, bad path) must not cost us the packet.
        try:
            os.makedirs(self.dump_packets_dir, exist_ok=True)
            path = os.path.join(self.dump_packets_dir, f"{time.time_ns()}_{opcode:#06x}.bin")
            with open(path, 'wb') as f:
                f.write(data)
        except OSError as e:
            if self._error_throttle.check('dump')[0]:
                log.warning("AGENT_DUMP_PACKETS: could not write dump: %s", e)

    def _parse_update_object(self, data: bytes):
        """Parse an (inflated) SMSG_UPDATE_OBJECT payload into world state.

        Full block framing + movement parsing lives in agent/update_object.py
        (pure function, no I/O); field mapping in agent/update_fields.py. This
        wires the result into WorldState (create/merge/remove) and, for
        blocks about our own player, mirrors position and a few stats onto
        the session directly for cheap access without going through
        world_state.

        Raises PerceptionParseError on truncated or malformed data — the
        caller's per-packet safety net (_dispatch_guarded) drops just this
        packet and keeps the connection. Anything recorded from earlier
        packets is kept.
        """
        try:
            blocks = uo.parse_update_object(data)
        except (IndexError, struct.error, ValueError) as e:
            raise per.PerceptionParseError(
                f"malformed update-object payload ({len(data)} B): {e}") from e
        for block in blocks:
            if block.update_type in (UPDATETYPE_OUT_OF_RANGE_OBJECTS, UPDATETYPE_NEAR_OBJECTS):
                self.world_state.remove_guids(block.guids)
                continue
            self.world_state.update_object(block)
            if block.guid == self.player_guid:
                self._sync_self_from_block(block)

    def _sync_self_from_block(self, block):
        """Mirror our own object's position/stats from world_state onto the
        session for cheap direct access (session.player_position, .level, ...)."""
        movement = block.movement or {}
        if "x" in movement:
            map_id = self.player_position[0] if self.player_position else self.world_state.my_map
            self.player_position = (map_id, movement["x"], movement["y"], movement["z"], movement.get("o", 0.0))
        me = self.world_state.get_my_object()
        if me is not None:
            if me.level is not None:
                self.level = me.level
            xp = me.raw_fields.get(uo_fields.PLAYER_XP)
            if xp is not None:
                self.xp = xp
            next_xp = me.raw_fields.get(uo_fields.PLAYER_NEXT_LEVEL_XP)
            if next_xp is not None:
                self.next_level_xp = next_xp
            coinage = me.raw_fields.get(uo_fields.PLAYER_FIELD_COINAGE)
            coinage = me.raw_fields.get(uo_fields.PLAYER_FIELD_COINAGE)
            if coinage is None:
                # Zero-value fields aren't sent over the wire, so a coinage of
                # 0 copper looks identical to "field absent". Once the self
                # object exists we know the field would be present for any
                # nonzero value, so treat "missing" as 0 instead of leaving
                # self.coinage as None.
                coinage = 0
            if self.coinage is not None and coinage != self.coinage:
                self._record_event("money_changed", old=self.coinage, new=coinage,
                                    delta=coinage - self.coinage)
            self.coinage = coinage
            self._check_death_transition(me)

    def _check_death_transition(self, me):
        """Emit a 'death' event on the health>0 -> 0 transition (UM-43).
        There is no SMSG_PLAYER_DEAD in TrinityCore 3.3.5 — death is only
        observable via this VALUES update (UNIT_FIELD_HEALTH -> 0), per
        Player::Kill (Player.cpp). Re-arms once health is next seen > 0
        (after a resurrect), so a later death emits again."""
        if me.health is None:
            return
        if me.health == 0:
            if self._was_alive:
                self._was_alive = False
                self._record_event("death", killer_guid=self._infer_killer_guid(),
                                    position=self.player_position)
        elif me.health > 0:
            self._was_alive = True

    def _infer_killer_guid(self):
        """Best-effort killer GUID for the 'death' event: the most recent
        combat event (in agent.events, newest last) whose victim was us.
        SMSG_ATTACKERSTATEUPDATE/SMSG_PARTYKILLLOG (UM-39) are the only
        signals available — TrinityCore doesn't otherwise tell the client
        who landed the killing blow. None if nothing matches (e.g. died to
        fall damage/environment, or the killing packet hasn't arrived yet)."""
        for e in reversed(self.events):
            if e.get("kind") == "party_kill" and e.get("victim_guid") == self.player_guid:
                return e.get("killer_guid")
            if e.get("kind") == "attacker_state_update" and e.get("victim_guid") == self.player_guid:
                return e.get("attacker_guid")
        return None

    def _handle_corpse_reclaim_delay(self, payload: bytes):
        """SMSG_CORPSE_RECLAIM_DELAY (Player::SendCorpseReclaimDelay,
        Player.cpp): uint32 Remaining, milliseconds until CMSG_RECLAIM_CORPSE
        will be accepted (server enforces this too; kept here so
        reclaim_corpse's check() can fail fast instead of round-tripping)."""
        remaining_ms = pk.u32(payload, 0)
        self.corpse_reclaim_ready_at = time.monotonic() + remaining_ms / 1000.0

    def _handle_death_release_loc(self, payload: bytes):
        """SMSG_DEATH_RELEASE_LOC (WorldPackets::Misc::DeathReleaseLoc,
        MiscPackets.cpp): int32 MapID, float x, y, z. Player::ResurrectPlayer
        also sends this opcode with MapID=-1 as a "clear the release marker"
        signal (no real position follows it in that case) — skip storing
        that sentinel rather than mistake it for a graveyard position."""
        map_id = struct.unpack_from('<i', payload, 0)[0]
        if map_id < 0:
            return
        x, y, z = struct.unpack_from('<3f', payload, 4)
        self.graveyard_position = (map_id, x, y, z)

    def _handle_corpse_query_response(self, payload: bytes):
        """MSG_CORPSE_QUERY's server->client shape (WorldPackets::Query::
        CorpseLocation::Write, QueryPackets.cpp): uint8 Valid; if valid,
        int32 MapID, float x, y, z, int32 ActualMapID, uint32 Transport.
        ActualMapID (not MapID) is what a client paths to — for a corpse in
        an instance's entrance map TrinityCore substitutes the reachable
        entrance map/position there, per WorldSession::HandleQueryCorpseLocation
        (QueryHandler.cpp). Transport offsets are unused — the agent doesn't
        ride transports yet (same limitation as agent/movement.py)."""
        valid = payload[0]
        if not valid:
            self.corpse_position = None
            return
        x, y, z = struct.unpack_from('<3f', payload, 5)
        actual_map_id = struct.unpack_from('<i', payload, 17)[0]
        self.corpse_position = (actual_map_id, x, y, z)

    def _handle_messagechat(self, opcode: int, payload: bytes):
        """SMSG_MESSAGECHAT / SMSG_GM_MESSAGECHAT (WorldPackets::Chat::Chat::Write,
        ChatPackets.cpp). SlashCmd (uint8) selects one of four shapes for
        what comes between the (sender_guid, flags) header and the chat
        text:

        - CHAT_KINDS_MONSTER (NPC say/party/yell/whisper/emote, raid-boss
          emote/whisper, battle.net): uint32 name_len + sender_name (always,
          not just on the GM opcode — the client never otherwise knows an
          NPC's name), uint64 target_guid, then [uint32 name_len +
          target_name] only if target_guid is set and isn't a player or pet
          (ObjectGuid::HighGuid, top 16 bits of the raw guid).
        - CHAT_MSG_WHISPER_FOREIGN: uint32 name_len + sender_name, uint64
          target_guid (no conditional target name).
        - CHAT_KINDS_BG_SYSTEM: uint64 target_guid, then [uint32 name_len +
          target_name] only if target_guid is set and isn't a player.
        - default (say/yell/whisper/party/guild/officer/emote/channel/
          achievement/everything else): [uint32 name_len + sender_name, only
          on SMSG_GM_MESSAGECHAT], [cstring channel, only for
          CHAT_MSG_CHANNEL], uint64 target_guid.

        Then always: uint32 text_len + text, uint8 chat_tag, and — only for
        CHAT_KINDS_ACHIEVEMENT — a trailing uint32 achievement_id (not
        needed by callers yet, left unconsumed since it's the last field).

        target_guid is deliberately not exported (UM-47). It is
        Chat::Initialize's `receiver` argument, and TrinityCore passes the
        *same object* as sender and receiver for player chat —
        Player::Say/Yell/TextEmote and both halves of Player::Whisper all
        call Initialize(..., X, X, ...). Live captures confirm it: every
        payload in tests/fixtures/chat has target_guid == sender_guid. It
        never identifies the listener, and for a whisper the addressee is
        already in sender_guid (see below), so reading it only invites the
        mistake of treating it as "who this was said to".

        The whisper pair is the one place kind alone isn't enough to know
        who is who: the recipient gets CHAT_MSG_WHISPER with the whisperer
        in sender_guid, while the whisperer gets CHAT_MSG_WHISPER_INFORM
        with the *addressee* in sender_guid.
        """
        off = 0
        slash_cmd = payload[off]; off += 1
        off += 4  # language (int32) — not needed by callers yet
        sender_guid = pk.u64(payload, off); off += 8
        off += 4  # flags (uint32), always 0 in 3.3.5

        sender_name = ""
        target_name = ""
        target_guid = 0
        channel = None

        if slash_cmd in CHAT_KINDS_MONSTER:
            sender_name, off = _read_len_string(payload, off)
            target_guid = pk.u64(payload, off); off += 8
            if target_guid and not _guid_is_player(target_guid) and not _guid_is_pet(target_guid):
                target_name, off = _read_len_string(payload, off)
        elif slash_cmd == CHAT_MSG_WHISPER_FOREIGN:
            sender_name, off = _read_len_string(payload, off)
            off += 8  # target_guid — see the docstring; not exported
        elif slash_cmd in CHAT_KINDS_BG_SYSTEM:
            target_guid = pk.u64(payload, off); off += 8
            if target_guid and not _guid_is_player(target_guid):
                target_name, off = _read_len_string(payload, off)
        else:
            if opcode == SMSG_GM_MESSAGECHAT:
                sender_name, off = _read_len_string(payload, off)
            if slash_cmd == CHAT_MSG_CHANNEL:
                channel, off = pk.cstring(payload, off)
            off += 8  # target_guid — see the docstring; not exported

        text, off = _read_len_string(payload, off)
        off += 1  # chat_tag (uint8) — not needed by callers yet

        entry = {
            "kind": CHAT_KIND_NAMES.get(slash_cmd, f"type_{slash_cmd}"),
            "sender_guid": sender_guid,
            "sender_name": sender_name,
            "channel": channel,
            "text": text,
        }
        if target_name:
            entry["target_name"] = target_name
        self.chat_inbox.append(entry)
        self._relay_chat(entry)

    def _relay_chat(self, entry: dict):
        """UM-47: mirror a heard message to tools/chat-feed, if a relay is
        configured. Never raises and never blocks the recv loop — see
        agent/chat_relay.py."""
        relay = self.chat_relay
        if relay is None:
            return
        try:
            relay.submit(entry, resolve_name=self.world_state.resolve_player_name)
        except Exception:  # noqa: BLE001 — chat must keep flowing to the agent itself
            log.debug("chat relay submit failed", exc_info=True)

    def _handle_group_invite(self, payload: bytes):
        # PartyInvite::Write (PartyPackets.cpp): uint8 can_accept, cstring
        # inviter_name, uint32 proposed_roles, ... (LFG fields, ignored).
        off = 1  # skip can_accept
        inviter_name, off = pk.cstring(payload, off)
        self.pending_invite = {"inviter_name": inviter_name}
        log.info("group invite from %s", inviter_name)

    def _handle_chat_player_not_found(self, payload: bytes):
        """SMSG_CHAT_PLAYER_NOT_FOUND (0x2A9): WorldSession::
        SendPlayerNotFoundNotice (ChatHandler.cpp) — a single cstring, the
        target name that couldn't be resolved (offline, doesn't exist, or
        (for whisper) has whispers disabled/is ignoring us). Sent instead of
        any ack for the CMSG_MESSAGECHAT that triggered it, so
        WhisperAction.execute() polls session.events for this rather than a
        direct per-message ack."""
        target_name, _ = pk.cstring(payload, 0)
        self._record_event("whisper_failed", target_name=target_name)

    def join_channels(self, names, timeout: float = 5.0) -> dict:
        """UM-93: join chat channels (CMSG_JOIN_CHANNEL, agent.channels) and
        wait for each one's you_joined notice or error. The server doesn't
        auto-join General at login (a real client asks for it itself, see
        agent/channels.py), so callers do this right after
        login_character(). Returns {requested name: full joined name, or
        None if it didn't join in time / was refused}. A system channel
        the zone doesn't have (e.g. Trade outside a city) is dropped
        silently by the server, so it just times out."""
        results = {}
        for name in names:
            sent_at = time.monotonic()
            try:
                payload = ch_mod.build_join_channel(name)
            except ValueError as e:
                log.warning("not joining channel %r: %s", name, e)
                results[name] = None
                continue
            self._send_packet(ch_mod.CMSG_JOIN_CHANNEL, payload)
            cid = ch_mod.system_channel_id(name)
            deadline = sent_at + timeout
            joined = None
            while joined is None and time.monotonic() < deadline:
                for e in list(self.events):
                    if e.get("t", 0) < sent_at:
                        continue
                    if e.get("kind") == "channel_joined" and (
                            (cid and e.get("channel_id") == cid)
                            or e.get("channel", "").lower() == name.lower()):
                        joined = e["channel"]
                        break
                    if e.get("kind") == "channel_error" and e.get("channel", "").lower().startswith(name.lower()):
                        joined = False
                        break
                else:
                    time.sleep(0.1)
            results[name] = joined or None
            if joined:
                log.info("joined channel %r", joined)
            else:
                log.warning("could not join channel %r", name)
        return results

    def _handle_channel_notify(self, payload: bytes):
        """SMSG_CHANNEL_NOTIFY (0x099, agent.channels.parse_channel_notify).
        you_joined/you_left update world_state.channels; they and every
        error notice (not_member, muted, throttled, ...) are also recorded
        as channel_joined / channel_left / channel_error events so
        join_channels() and ChannelSayAction can confirm the outcome."""
        try:
            data = ch_mod.parse_channel_notify(payload)
        except (IndexError, ValueError, struct.error) as e:
            raise per.PerceptionParseError(f"malformed SMSG_CHANNEL_NOTIFY ({len(payload)} B): {e}") from e
        self.world_state.apply_channel_notify(data)
        if data["notice"] == ch_mod.CHAT_YOU_JOINED_NOTICE:
            self._record_event("channel_joined", channel=data["channel"],
                                channel_id=data["channel_id"], flags=data["flags"])
        elif data["notice"] == ch_mod.CHAT_YOU_LEFT_NOTICE:
            self._record_event("channel_left", channel=data["channel"], channel_id=data["channel_id"])
        elif data["notice"] in ch_mod.ERROR_NOTICES:
            self._record_event("channel_error", channel=data["channel"],
                                notice=data["notice"], reason=data["notice_name"])

    def _handle_party_command_result(self, payload: bytes):
        """SMSG_PARTY_COMMAND_RESULT (0x07F): WorldSession::SendPartyResult
        (GroupHandler.cpp) — uint32 operation, cstring member_name, uint32
        result (PartyResult), uint32 val (LFG-cooldown related, unused
        here). HandleGroupInviteOpcode sends this on both success
        (ERR_PARTY_RESULT_OK) and failure (e.g. a bad name or a target
        already in a group) — only record it as an event on failure, so
        InviteToGroupAction's timeout-is-success default still applies to
        the OK case without special-casing it."""
        off = 0
        operation = pk.u32(payload, off); off += 4
        member_name, off = pk.cstring(payload, off)
        result = pk.u32(payload, off); off += 4
        if result == ERR_PARTY_RESULT_OK:
            return
        self._record_event("group_invite_failed", target_name=member_name,
                            result=result,
                            result_name=PARTY_RESULT_NAMES.get(result, f"result_{result}"))

    def _handle_initial_spells(self, payload: bytes):
        info = sp.parse_initial_spells(payload)
        self.spellbook = set(info["spell_ids"])
        self.spell_cooldowns = {c["spell_id"]: c for c in info["cooldowns"]}

    def _handle_learned_spell(self, payload: bytes):
        self.spellbook.add(sp.parse_learned_spell(payload))

    def _handle_removed_spell(self, payload: bytes):
        spell_id = sp.parse_removed_spell(payload)
        self.spellbook.discard(spell_id)
        self.spell_cooldowns.pop(spell_id, None)

    def _record_event(self, kind: str, **fields):
        """Append to session.events with a time.monotonic() timestamp, so
        callers (e.g. agent.actions' _wait_for-style confirmation) can find
        "events since I sent this packet" reliably even though `events` is
        a bounded deque (older entries can fall off the left, which would
        make plain index-based slicing wrong)."""
        self.events.append({"kind": kind, "t": time.monotonic(), **fields})

    def _handle_cast_failed(self, payload: bytes):
        self._record_event("cast_failed", **sp.parse_cast_failed(payload))

    def _handle_spell_start(self, payload: bytes):
        self._record_event("spell_start", **sp.parse_spell_cast_prefix(payload))

    def _handle_spell_go(self, payload: bytes):
        self._record_event("spell_go", **sp.parse_spell_cast_prefix(payload))

    def _handle_attack_start(self, payload: bytes):
        self._record_event("attack_start", **sp.parse_attack_start(payload))

    def _handle_attack_stop(self, payload: bytes):
        self._record_event("attack_stop", **sp.parse_attack_stop(payload))

    def _handle_attacker_state_update(self, payload: bytes):
        self._record_event("attacker_state_update", **sp.parse_attacker_state_update(payload))

    def _handle_spell_non_melee_damage_log(self, payload: bytes):
        self._record_event("spell_damage", **sp.parse_spell_non_melee_damage_log(payload))

    def _handle_party_kill_log(self, payload: bytes):
        self._record_event("party_kill", **sp.parse_party_kill_log(payload))

    def _handle_log_xp_gain(self, payload: bytes):
        self._record_event("xp_gain", **sp.parse_log_xp_gain(payload))

    def _handle_levelup_info(self, payload: bytes):
        self._record_event("levelup", **sp.parse_levelup_info(payload))

    def _send_sync(self, payload):
        counter = struct.unpack_from('<I', payload, 0)[0]
        self._send_packet(CMSG_TIME_SYNC_RESP, struct.pack('<II', counter, 0))

    def _dispatch(self, opcode: int, payload: bytes) -> bool:
        """Handle one packet. Returns False if the opcode isn't handled."""
        if opcode == SMSG_TIME_SYNC_REQ:
            self._send_sync(payload)
        elif opcode in (SMSG_COMPRESSED_UPDATE_OBJECT, SMSG_UPDATE_OBJECT):
            self._handle_update_object(opcode, payload)
        elif opcode in (SMSG_MESSAGECHAT, SMSG_GM_MESSAGECHAT):
            self._handle_messagechat(opcode, payload)
        elif opcode == SMSG_GROUP_INVITE:
            self._handle_group_invite(payload)
        elif opcode == SMSG_CHAT_PLAYER_NOT_FOUND:
            self._handle_chat_player_not_found(payload)
        elif opcode == ch_mod.SMSG_CHANNEL_NOTIFY:
            self._handle_channel_notify(payload)
        elif opcode == SMSG_PARTY_COMMAND_RESULT:
            self._handle_party_command_result(payload)
        elif opcode == SMSG_DESTROY_OBJECT:
            self._handle_destroy_object(payload)
        elif opcode == SMSG_NAME_QUERY_RESPONSE:
            self._handle_name_query_response(payload)
        elif opcode == SMSG_CREATURE_QUERY_RESPONSE:
            self._handle_creature_query_response(payload)
        elif opcode == SMSG_GAMEOBJECT_QUERY_RESPONSE:
            self._handle_gameobject_query_response(payload)
        elif opcode == SMSG_MONSTER_MOVE:
            self._handle_monster_move(payload)
        elif opcode in MSG_MOVE_OPCODES:
            self._handle_move_broadcast(opcode, payload)
        elif opcode in _SPELL_DISPATCH:
            getattr(self, _SPELL_DISPATCH[opcode])(payload)
        elif opcode == SMSG_CORPSE_RECLAIM_DELAY:
            self._handle_corpse_reclaim_delay(payload)
        elif opcode == SMSG_DEATH_RELEASE_LOC:
            self._handle_death_release_loc(payload)
        elif opcode == MSG_CORPSE_QUERY:
            self._handle_corpse_query_response(payload)
        elif opcode in _NPC_DISPATCH:
            getattr(self, _NPC_DISPATCH[opcode])(payload)
        elif opcode in _LOOT_DISPATCH:
            getattr(self, _LOOT_DISPATCH[opcode])(payload)
        elif opcode in _QUEST_DISPATCH:
            getattr(self, _QUEST_DISPATCH[opcode])(payload)
        elif opcode in _TRADE_DISPATCH:
            getattr(self, _TRADE_DISPATCH[opcode])(payload)
        elif opcode in _MAIL_DISPATCH:
            getattr(self, _MAIL_DISPATCH[opcode])(payload)
        elif opcode in (SMSG_PONG, SMSG_STANDSTATE_UPDATE):
            pass
        elif opcode == SMSG_LOGOUT_COMPLETE:
            if not self._logout_requested:
                # The server ended our session without us asking (kicked,
                # duplicate login elsewhere, forced disconnect) — the
                # reconnect supervisor (agent/__main__.py, UM-43) treats
                # this the same as a dropped socket.
                self.unexpected_disconnect = True
            self._in_world = False
            self._running = False
        else:
            return False
        return True

    def _dispatch_guarded(self, opcode: int, payload: bytes) -> bool:
        """_dispatch, but a handler error drops only this packet, never the
        connection. Errors are logged at most once per opcode per interval."""
        try:
            return self._dispatch(opcode, payload)
        except Exception:
            self.dropped_packets += 1
            should_log, suppressed = self._error_throttle.check(opcode)
            if should_log:
                log.warning("dropped %#05x packet (%d B) after handler error "
                            "(%d more on this opcode suppressed since last report)",
                            opcode, len(payload), suppressed, exc_info=True)
            return True

    def _recv_loop(self):
        """Background thread: read packets and dispatch."""
        try:
            self._recv_until_stopped()
        except Exception:
            log.exception("recv thread crashed")
        finally:
            if self._running:
                log.warning("recv thread exited while the session was still running")
                self._running = False
                # Not a requested logout() (that sets _running=False itself
                # before joining this thread) — the reconnect supervisor
                # (agent/__main__.py, UM-43) treats this as a drop worth
                # retrying rather than the agent process exiting.
                self.unexpected_disconnect = True

    def _recv_until_stopped(self):
        last_keepalive = time.monotonic()
        while self._running:
            self.sock.settimeout(0.5)
            try:
                opcode, payload = self._recv_packet()
            except (socket.timeout, TimeoutError):
                now = time.monotonic()
                if now - last_keepalive > 15:
                    self._send_packet(CMSG_KEEP_ALIVE)
                    last_keepalive = now
                self._send_name_queries()
                self._send_npc_text_queries()
                self._send_item_queries()
                self._send_quest_queries()
                continue
            except ConnectionError as e:
                log.warning("world connection lost: %s", e)
                return
            self._dispatch_guarded(opcode, payload)
            self._send_name_queries()
            self._send_npc_text_queries()
            self._send_item_queries()
            self._send_quest_queries()

    def _send_packet(self, opcode: int, payload: bytes = b''):
        hdr = struct.pack('>H', len(payload) + 4) + struct.pack('<I', opcode)
        if self.crypt:
            hdr = self.crypt.encrypt_send(hdr)
        with self._lock:
            self.sock.sendall(hdr + payload)

    def _recv_packet(self) -> tuple[int, bytes]:
        # Only this first read may time out: nothing has been consumed yet.
        first = self._rr(1)
        prev_timeout = self.sock.gettimeout()
        if prev_timeout is not None and prev_timeout < MID_PACKET_TIMEOUT_S:
            self.sock.settimeout(MID_PACKET_TIMEOUT_S)
        try:
            hdr = first + self._rr(3)
            if self.crypt:
                hdr = self.crypt.decrypt_recv(hdr)
            if hdr[0] & 0x80:  # large packet: one more size byte before the opcode
                extra = self._rr(1)
                if self.crypt:
                    extra = self.crypt.decrypt_recv(extra)
                hdr += extra
            size, opcode = pk.parse_server_header(hdr)
            plen = size - 2
            payload = self._rr(plen) if plen > 0 else b''
        except (socket.timeout, TimeoutError) as e:
            raise ConnectionError("timed out mid-packet, stream out of sync") from e
        finally:
            self.sock.settimeout(prev_timeout)
        return opcode, payload

    def _rr(self, n: int) -> bytes:
        buf = b''
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise ConnectionError("disconnected")
            buf += c
        return buf