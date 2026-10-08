"""Combat/spell/XP packets (UM-39): spellbook state and combat events."""

from .. import spells as sp
from ..router import ROUTER

# Combat / spells (UM-39)
from ..opcodes import (
    SMSG_INITIAL_SPELLS,
    SMSG_LEARNED_SPELL,
    SMSG_CAST_FAILED,
    SMSG_SPELL_START,
    SMSG_SPELL_GO,
    SMSG_ATTACK_START,
    SMSG_ATTACK_STOP,
    SMSG_ATTACKERSTATEUPDATE,
    SMSG_LOG_XPGAIN,
    SMSG_LEVELUP_INFO,
    SMSG_PARTYKILLLOG,
    SMSG_REMOVED_SPELL,
    SMSG_SPELLNONMELEEDAMAGELOG,
)


def handle_initial_spells(ctx, payload: bytes):
    info = sp.parse_initial_spells(payload)
    ctx.state.spellbook = set(info["spell_ids"])
    ctx.state.spell_cooldowns = {c["spell_id"]: c for c in info["cooldowns"]}


def handle_learned_spell(ctx, payload: bytes):
    ctx.state.spellbook.add(sp.parse_learned_spell(payload))


def handle_removed_spell(ctx, payload: bytes):
    spell_id = sp.parse_removed_spell(payload)
    ctx.state.spellbook.discard(spell_id)
    ctx.state.spell_cooldowns.pop(spell_id, None)


def handle_cast_failed(ctx, payload: bytes):
    ctx.state.record_event("cast_failed", **sp.parse_cast_failed(payload))


def handle_spell_start(ctx, payload: bytes):
    ctx.state.record_event("spell_start", **sp.parse_spell_cast_prefix(payload))


def handle_spell_go(ctx, payload: bytes):
    ctx.state.record_event("spell_go", **sp.parse_spell_cast_prefix(payload))


def handle_attack_start(ctx, payload: bytes):
    ctx.state.record_event("attack_start", **sp.parse_attack_start(payload))


def handle_attack_stop(ctx, payload: bytes):
    ctx.state.record_event("attack_stop", **sp.parse_attack_stop(payload))


def handle_attacker_state_update(ctx, payload: bytes):
    ctx.state.record_event("attacker_state_update", **sp.parse_attacker_state_update(payload))


def handle_spell_non_melee_damage_log(ctx, payload: bytes):
    ctx.state.record_event("spell_damage", **sp.parse_spell_non_melee_damage_log(payload))


def handle_party_kill_log(ctx, payload: bytes):
    ctx.state.record_event("party_kill", **sp.parse_party_kill_log(payload))


def handle_log_xp_gain(ctx, payload: bytes):
    ctx.state.record_event("xp_gain", **sp.parse_log_xp_gain(payload))


def handle_levelup_info(ctx, payload: bytes):
    ctx.state.record_event("levelup", **sp.parse_levelup_info(payload))


ROUTER.register_all({
    SMSG_INITIAL_SPELLS: handle_initial_spells,
    SMSG_LEARNED_SPELL: handle_learned_spell,
    SMSG_REMOVED_SPELL: handle_removed_spell,
    SMSG_CAST_FAILED: handle_cast_failed,
    SMSG_SPELL_START: handle_spell_start,
    SMSG_SPELL_GO: handle_spell_go,
    SMSG_ATTACK_START: handle_attack_start,
    SMSG_ATTACK_STOP: handle_attack_stop,
    SMSG_ATTACKERSTATEUPDATE: handle_attacker_state_update,
    SMSG_SPELLNONMELEEDAMAGELOG: handle_spell_non_melee_damage_log,
    SMSG_PARTYKILLLOG: handle_party_kill_log,
    SMSG_LOG_XPGAIN: handle_log_xp_gain,
    SMSG_LEVELUP_INFO: handle_levelup_info,
})
