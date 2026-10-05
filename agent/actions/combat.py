"""Combat actions: auto_attack, stop_attack, idle, cast_spell.

Split out of the former agent/actions.py (issue #248).
"""

import struct
import time

from .. import spells
from .. import update_fields as uf
from ..opcodes import (
    CMSG_ATTACKSTOP,
    CMSG_ATTACKSWING,
    CMSG_CAST_SPELL,
)
from .base import (
    Action,
    ActionResult,
    DEFAULT_CONFIRM_POLL_S,
    DEFAULT_CONFIRM_TIMEOUT_S,
    _wait_for,
    _wait_for_value,
    register,
    send,
)
from .movement import (FaceAction, send_target)


MELEE_RANGE_YD = 5.0  # ~ melee weapon range + average combat reach


def send_attack(session, guid: int):
    """Start auto-attack on target. AttackSwing::Read (CombatPackets.cpp):
    a single raw (not packed) uint64 victim guid — previously sent with no
    payload at all (a malformed packet; found in review, fixed by UM-36)."""
    send_target(session, guid)
    send(session, CMSG_ATTACKSWING, struct.pack("<Q", guid))


@register
class AutoAttackAction(Action):
    name = "auto_attack"
    description = "Target and start melee auto-attack on a nearby hostile unit."
    params = {
        "guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the unit to attack."},
    }
    required = ("guid",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, guid: int, **_) -> str | None:
        target = world.get_object(guid)
        if target is None:
            return f"guid {guid:#x} is not currently perceived"
        if target.is_dead():
            return f"guid {guid:#x} is already dead"
        me = world.get_my_object()
        if me is not None and target.is_hostile_to(me.faction) is False:
            return f"guid {guid:#x} is not hostile"
        if session.player_position is None:
            return "own position unknown"
        distance = target.distance_to(session.player_position)
        if distance is not None and distance > MELEE_RANGE_YD:
            return (f"guid {guid:#x} is {distance:.1f} yd away, out of melee range "
                    f"({MELEE_RANGE_YD} yd) — try move_towards first")
        return None

    def execute(self, session, world, guid: int, **_) -> ActionResult:
        # The server won't swing unless the victim is in our 120-degree front arc:
        # Player::Update (Player.cpp, TC 3.3.5 @092eb27) checks
        # HasInArc(2*M_PI/3, victim), else SMSG_ATTACKSWING_BADFACING. Attack()
        # still sends SMSG_ATTACK_START, so that alone doesn't prove a swing.
        target = world.get_object(guid)
        if target is not None and target.position is not None and session.player_position is not None:
            FaceAction().execute(session, world, guid=guid)
        sent_at = time.monotonic()
        send_target(session, guid)
        send_attack(session, guid)
        confirmed = _wait_for(
            lambda: any(e.get("kind") == "attack_start" and e.get("victim_guid") == guid
                        and e.get("t", 0) >= sent_at for e in session.events),
            timeout=self.confirm_timeout, interval=self.confirm_interval)
        if not confirmed:
            return ActionResult(ok=False, error="no attack_start event seen", detail={"guid": guid})
        return ActionResult(ok=True, detail={"guid": guid})


@register
class StopAttackAction(Action):
    name = "stop_attack"
    description = "Stop melee auto-attack."
    params = {}
    required = ()

    def execute(self, session, world, **_) -> ActionResult:
        send(session, CMSG_ATTACKSTOP)
        return ActionResult(ok=True)


@register
class IdleAction(Action):
    """UM-97: an explicit "do nothing this cycle" choice, so a brain that
    picks from a fixed candidate list (agent/candidates.py, for Jev) always
    has a safe option that maps to a real registered action. Sends nothing."""
    name = "idle"
    description = "Do nothing this cycle (wait and watch). Always a valid choice."
    params = {}
    required = ()

    def execute(self, session, world, **_) -> ActionResult:
        return ActionResult(ok=True, detail={"idle": True})


@register
class CastSpellAction(Action):
    name = "cast_spell"
    description = "Cast a known spell, optionally on a target (defaults to self if target_guid is omitted)."
    params = {
        "spell_id": {"type": "integer", "description": "Spell ID to cast — must be in the agent's spellbook."},
        "target_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the unit to cast on. Omit to cast on self."},
    }
    required = ("spell_id",)
    confirm_timeout = DEFAULT_CONFIRM_TIMEOUT_S
    confirm_interval = DEFAULT_CONFIRM_POLL_S

    def check(self, session, world, spell_id: int, target_guid: int | None = None, **_) -> str | None:
        if spell_id not in session.spellbook:
            return f"spell {spell_id} is not known"
        cooldown = session.spell_cooldowns.get(spell_id)
        if cooldown and cooldown.get("recovery_time", 0) > 0:
            return f"spell {spell_id} is on cooldown"
        info = spells.get_spell_info(spell_id)
        if info is not None and info.power_cost:
            me = world.get_my_object()
            power_name = uf.POWER_NAMES[info.power_type] if 0 <= info.power_type < len(uf.POWER_NAMES) else None
            have = me.power.get(power_name) if me is not None and power_name is not None else None
            if have is not None and have < info.power_cost:
                return (f"not enough power for spell {spell_id} "
                        f"(need {info.power_cost}, have {have})")
        if target_guid is not None and world.get_object(target_guid) is None:
            return f"guid {target_guid:#x} is not currently perceived"
        return None

    def execute(self, session, world, spell_id: int, target_guid: int | None = None, **_) -> ActionResult:
        if target_guid is not None:
            target = world.get_object(target_guid)
            if target is not None and target.position is not None and session.player_position is not None:
                FaceAction().execute(session, world, guid=target_guid)

        sent_at = time.monotonic()
        send(session, CMSG_CAST_SPELL, spells.build_cast_spell(spell_id, target_guid=target_guid))

        def find_start_or_failure():
            for e in session.events:
                if e.get("t", 0) < sent_at or e.get("spell_id") != spell_id:
                    continue
                if e.get("kind") in ("cast_failed", "spell_start", "spell_go"):
                    return e
            return None

        # The server always answers with SMSG_SPELL_START right away (even
        # for instant casts — TrinityCore's Spell::prepare sends it before
        # checking cast time), then SMSG_SPELL_GO only after the spell's own
        # cast_time (ms) elapses. A fixed confirm_timeout would time out on
        # any cast_time above ~1.9s (e.g. Holy Light's 2.5s) even on success
        # — found live testing against a training dummy — so the wait for
        # the real outcome is extended by the cast time this spell reports.
        event = _wait_for_value(find_start_or_failure, timeout=self.confirm_timeout,
                                 interval=self.confirm_interval)
        if event is None:
            return ActionResult(ok=False, error="no cast confirmation seen (timed out)",
                                 detail={"spell_id": spell_id})
        if event["kind"] in ("cast_failed", "spell_go"):
            if event["kind"] == "cast_failed":
                return ActionResult(ok=False, error=event["reason_name"], detail=event)
            return ActionResult(ok=True, detail=event)

        # event["kind"] == "spell_start": cast is in flight — wait out its
        # reported cast time (plus the normal confirm margin) for the
        # actual outcome.
        started_at = event["t"]
        cast_time_s = event.get("cast_time", 0) / 1000.0

        def find_outcome():
            for e in session.events:
                if e.get("t", 0) <= started_at or e.get("spell_id") != spell_id:
                    continue
                if e.get("kind") in ("cast_failed", "spell_go"):
                    return e
            return None

        outcome = _wait_for_value(find_outcome, timeout=cast_time_s + self.confirm_timeout,
                                   interval=self.confirm_interval)
        if outcome is None:
            return ActionResult(ok=False, error="no cast confirmation seen (timed out)",
                                 detail={"spell_id": spell_id, "cast_time_ms": event.get("cast_time")})
        if outcome["kind"] == "cast_failed":
            return ActionResult(ok=False, error=outcome["reason_name"], detail=outcome)
        return ActionResult(ok=True, detail=outcome)
