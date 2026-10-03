"""Spell description templates ("Restores $o1 health over $d.") -> plain text.

The client shows the `Description` column of Spell.dbc with `$` variables filled in
from the spell's own fields. This module resolves the subset whose meaning is
verified (see below) and refuses everything else: `render` raises `Unresolved`
instead of guessing, and callers leave the tooltip line out. A wrong number on a
tooltip is worse than a missing line.

Field meanings come from TrinityCore 3.3.5 `SpellEntry` / `SpellEffectInfo::CalcValue`
(src/server/shared/DataStores/DBCStructure.h, src/server/game/Spells/SpellInfo.cpp):
the DBC stores `EffectBasePoints` one below the displayed value, and
`EffectDieSides` says how: 0 shows the base points as they are, 1 adds one, more
than 1 is the range `base + 1 .. base + dice`. Checked against the build-12340
client data: Blessed Sunfruit (spell 18125, base points 9, one die side) is the
in-game "Increases Strength by 10", and Tough Jerky's spell 433 ("Food": regen
aura 84, base points 16, 18 s) is "Restores 61 health over 18 sec" (17 * 18 / 5,
the aura ticks every 5 seconds).

Variables resolved (letter, then the effect number 1-3, optionally preceded by the
id of another spell):

    $sN               effect value (absolute); a die range prints as "70 to 90"
    $mN $MN           min / max of a die range (equal when there is no range)
    $oN               total over the duration: value * duration / period
    $tN               period in seconds
    $d                duration, "18 sec" / "10 min" / "2 hrs" (GlobalStrings
                      SPELL_DURATION_*)
    $aN $AN           radius in yards (SpellRadius.dbc Radius / RadiusMax)
    $xN               chain targets;  $h proc chance;  $n proc charges
    $/N;s1 $*N;s1     divide / multiply the value by a whole number
    $lsingular:plural;  picks by the last number printed
    $z                the hearthstone location; shown as "your home location"

Anything else (`$?` conditionals, `${...}` expressions, `$g`, level-scaled values
with a non-zero points-per-level, ...) is `Unresolved`.
"""
import re
from typing import NamedTuple


class Unresolved(ValueError):
    """The description uses something this module doesn't resolve."""


class Effect(NamedTuple):
    base_points: int = 0     # Spell.dbc EffectBasePoints (displayed value - 1)
    die_sides: int = 0       # EffectDieSides
    per_level: float = 0.0   # EffectRealPointsPerLevel
    aura: int = 0            # EffectAura (SpellAuraType)
    period_ms: int = 0       # EffectAuraPeriod
    radius: float = 0.0      # SpellRadius.Radius of EffectRadiusIndex
    radius_max: float = 0.0  # SpellRadius.RadiusMax
    chain_targets: int = 0   # EffectChainTargets


class SpellInfo(NamedTuple):
    name: str
    description: str
    duration_ms: int = 0     # SpellDuration.Duration; 0 = no duration, -1 = until cancelled
    proc_chance: int = 0
    proc_charges: int = 0
    effects: tuple = (Effect(), Effect(), Effect())


# SPELL_AURA_MOD_REGEN / SPELL_AURA_MOD_POWER_REGEN: amount per 5 seconds, no
# EffectAuraPeriod of their own (the Food/Drink spells).
REGEN_AURAS = (84, 85)
REGEN_PERIOD_MS = 5000

_TOKEN = re.compile(
    r"\$(?:"
    r"(?P<plural>l(?P<one>[^:;$]*):(?P<many>[^;$]*);)"
    r"|(?P<op>[/*])(?P<opn>\d+);(?P<opref>\d*)(?P<opvar>[a-zA-Z])(?P<opidx>\d?)"
    r"|(?P<ref>\d*)(?P<var>[a-zA-Z])(?P<idx>\d?)"
    r")")


def _trim(x):
    """18.0 -> "18", 1.5 -> "1.5" (the client's "%.2f" with the zeros dropped)."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def format_duration(ms):
    """GlobalStrings SPELL_DURATION_SEC/MIN/HOURS/DAYS ("%.2f sec", "%.2f min",
    "%.2f |4hour:hrs;", "%.2f |4day:days;")."""
    if ms < 0:
        return "until cancelled"  # SPELL_DURATION_UNTIL_CANCELLED
    secs = ms / 1000
    if secs >= 86400:
        n, one, many = secs / 86400, "day", "days"
    elif secs >= 3600:
        n, one, many = secs / 3600, "hour", "hrs"
    elif secs >= 60:
        return f"{_trim(secs / 60)} min"
    else:
        return f"{_trim(secs)} sec"
    text = _trim(n)
    return f"{text} {one if text == '1' else many}"


def _value(effect, hi=False):
    """The effect's displayed value (min, or max for a die range). The DBC base
    points are one below what the game shows when a die is rolled."""
    if effect.per_level:
        raise Unresolved("value scales with level")
    die = effect.die_sides
    if hi and die > 1:
        value = effect.base_points + die
    else:
        value = effect.base_points + (1 if die >= 1 else 0)
    return abs(value)


def _number(n):
    return str(int(n)) if n == int(n) else _trim(n)


def render(text, spell, lookup=None):
    """The description `text` of `spell` (a SpellInfo) with variables filled in.
    `lookup(spell_id)` returns a SpellInfo for `$<id>s1` references. Raises
    Unresolved; the result has its whitespace collapsed to single spaces."""
    last = [None]  # the last number printed, for $l

    def sub(m):
        if m["plural"]:
            if last[0] is None:
                raise Unresolved("$l without a number before it")
            return m["one"] if last[0] == 1 else m["many"]
        if m["op"]:
            ref, var, idx, scale = m["opref"], m["opvar"], m["opidx"], int(m["opn"])
            if not scale:
                raise Unresolved("division by zero")
        else:
            ref, var, idx, scale = m["ref"], m["var"], m["idx"], None
        src = spell
        if ref:
            src = lookup(int(ref)) if lookup else None
            if src is None:
                raise Unresolved(f"unknown spell {ref}")
        i = int(idx or 1) - 1
        effect = src.effects[i] if 0 <= i <= 2 else None
        if effect is None and var in "sSmMoOtTaAxX":
            raise Unresolved(f"effect number {idx}")

        if var == "s" and effect.die_sides > 1 and not scale:
            # A die range prints as a range: Minor Healing Potion's spell (base points
            # 69, 21 die sides, description "Restores $s1 health.") is "Restores 70 to
            # 90 health." in game, and Fireball's "$s1 Fire damage" is "14 to 22".
            n = _value(effect)
            last[0] = _value(effect, hi=True)
            return f"{_number(n)} to {_number(last[0])}"
        elif var in "sSmM":
            if var == "S" and effect.die_sides > 1:
                raise Unresolved("$S of a die range")
            n = _value(effect, hi=var == "M" or var == "S")
        elif var in "oO":
            period = effect.period_ms or (REGEN_PERIOD_MS if effect.aura in REGEN_AURAS else 0)
            if not period or src.duration_ms <= 0:
                raise Unresolved("total without a period or duration")
            n = int(_value(effect) * src.duration_ms / period)
        elif var in "tT":
            if not effect.period_ms:
                raise Unresolved("no period")
            n = effect.period_ms / 1000
        elif var in "aA":
            n = effect.radius_max if var == "A" else effect.radius
            if not n:
                raise Unresolved("no radius")
        elif var in "xX":
            n = effect.chain_targets
            if not n:
                raise Unresolved("no chain targets")
        elif var in "dD" and not scale:
            if not src.duration_ms:
                raise Unresolved("no duration")
            return format_duration(src.duration_ms)
        elif var == "h" and not scale:
            n = src.proc_chance
        elif var == "n" and not scale:
            n = src.proc_charges
            if not n:
                raise Unresolved("no charges")
        elif var == "z" and not scale:
            return "your home location"
        else:
            raise Unresolved(f"${var}")

        if scale is not None:
            n = n / scale if m["op"] == "/" else n * scale
        last[0] = n
        return _number(n)

    try:
        out = _TOKEN.sub(sub, text)
    except RecursionError:  # pragma: no cover - defensive
        raise Unresolved("too deep") from None
    if "$" in out:
        raise Unresolved("unsupported variable")
    return " ".join(out.split())
