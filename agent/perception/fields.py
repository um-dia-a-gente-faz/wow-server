"""WorldState's decoding of update-object movement and field blocks onto an ObjectInfo."""

import time

from .. import update_fields as uf
from .objects import ObjectInfo


class FieldsMixin:
    def _apply_movement(self, obj: ObjectInfo, movement: dict | None):
        if not movement:
            return
        if "x" in movement:
            # Every object the server tells us about is on our current map
            # (or instance) — update-object blocks never carry a map ID of
            # their own, so we borrow the self object's.
            obj.position = (self.my_map, movement["x"], movement["y"], movement["z"], movement.get("o", 0.0))
        if "move_flags" in movement:
            obj.move_flags = movement["move_flags"]
        if "target_guid" in movement:
            obj.target_guid = movement["target_guid"]
        if "speeds" in movement:
            obj.speeds = movement["speeds"]
        spline = movement.get("spline")
        if spline is not None:
            obj.set_spline(
                (movement["x"], movement["y"], movement["z"]),
                spline["destination"],
                time.monotonic() - spline["time_passed"] / 1000.0,
                spline["duration"] / 1000.0,
            )
        elif "move_flags" in movement:
            # A fresh LIVING block without spline data (direct control, or
            # the spline finished) supersedes any earlier spline. POSITION/
            # STATIONARY_POSITION blocks never carry move_flags at all, so
            # they leave existing spline state alone rather than guess.
            obj.clear_spline()

    def update_my_position_from_simulation(self, position: tuple):
        """UM-38: agent.movement.Mover simulates our own position between
        real server updates (client-authoritative movement) — mirror it
        onto our own ObjectInfo so every perception consumer (distance_to,
        snapshot) sees it, not just session.player_position. A no-op before
        our own object exists (there's nowhere to put it yet)."""
        with self._lock:
            obj = self.objects.get(self.my_guid)
            if obj is not None:
                obj.position = position

    def _apply_fields(self, obj: ObjectInfo, object_type: int | None, raw_fields: dict | None):
        if not raw_fields:
            return
        obj.raw_fields.update(raw_fields)
        decoded = uf.decode_fields(object_type if object_type is not None else -1, raw_fields)
        for attr in ("entry", "level", "health", "max_health", "faction",
                     "unit_flags", "dynamic_flags", "npc_flags", "target_guid",
                     "player_flags", "power_type"):
            if attr in decoded:
                setattr(obj, attr, decoded[attr])
        if "power" in decoded:
            obj.power.update(decoded["power"])
        if "max_power" in decoded:
            obj.max_power.update(decoded["max_power"])
        # UM-83: the server sends non-zero baseline template values for
        # power types the unit's class never uses (e.g. a hunter's raw
        # fields include a phantom "energy" baseline alongside real mana —
        # WotLK hunters have no personal focus/energy pool). Once the unit's
        # own power_type (UNIT_FIELD_BYTES_0) is known, drop every entry
        # that isn't that one real power, so downstream consumers (the LLM
        # prompt snapshot, agent/actions/combat.py's cast_spell precondition) never see
        # resources the unit can't actually spend. Left alone until
        # power_type is known, since fields worth 0 aren't sent at all —
        # absent isn't the same as "not a real power".
        if obj.power_type is not None and 0 <= obj.power_type < len(uf.POWER_NAMES):
            real_power = uf.POWER_NAMES[obj.power_type]
            obj.power = {k: v for k, v in obj.power.items() if k == real_power}
            obj.max_power = {k: v for k, v in obj.max_power.items() if k == real_power}
