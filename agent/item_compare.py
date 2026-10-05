#!/usr/bin/env python3
"""Item comparison + upgrade scoring (UM-69), pure functions, no I/O — see
agent/actions/loot.py for the compare_items/equip_item Action wrappers that use
this module, and agent/loot.py's parse_item_query_response() for the shape
of the item dicts consumed here (cached by entry in agent.items.ItemCache,
same pattern as agent.names.NameCache).

v1 scoring heuristic (deliberately simple, documented here so it's obvious
where to extend later):

    score(item, class_id) = primary_stat_value(item, class_id) * PRIMARY_STAT_WEIGHT
                             + item.item_level

`primary_stat_value` sums every ItemStat entry on the item matching the
character's class's single "primary stat" (CLASS_PRIMARY_STAT below — one
stat per class, e.g. Strength for Warrior/Paladin, Agility for Hunter/
Rogue, Intellect for the caster classes). Item level only breaks
near-ties in that stat.

This is a v1 simplification with NO talent/spec awareness: a feral or
guardian druid actually wants Agility, not Intellect; an enhancement
shaman wants Agility/Strength, not Intellect; a fire vs. frost mage make
no difference here. Revisit once the agent tracks talent specs.
"""

# ItemModType (ItemTemplate.h) — only the stats v1 scoring cares about.
ITEM_MOD_STRENGTH = 4
ITEM_MOD_AGILITY = 3
ITEM_MOD_INTELLECT = 5
ITEM_MOD_SPIRIT = 6
ITEM_MOD_STAMINA = 7

STAT_NAMES = {
    ITEM_MOD_STRENGTH: "strength",
    ITEM_MOD_AGILITY: "agility",
    ITEM_MOD_INTELLECT: "intellect",
    ITEM_MOD_SPIRIT: "spirit",
    ITEM_MOD_STAMINA: "stamina",
}

# Chr classes (ChrClasses.dbc / SharedDefines.h CLASS_*).
CLASS_WARRIOR = 1
CLASS_PALADIN = 2
CLASS_HUNTER = 3
CLASS_ROGUE = 4
CLASS_PRIEST = 5
CLASS_DEATH_KNIGHT = 6
CLASS_SHAMAN = 7
CLASS_MAGE = 8
CLASS_WARLOCK = 9
CLASS_DRUID = 11

# v1 simplification (see module docstring): one "primary stat" per class.
CLASS_PRIMARY_STAT = {
    CLASS_WARRIOR: ITEM_MOD_STRENGTH,
    CLASS_PALADIN: ITEM_MOD_STRENGTH,
    CLASS_DEATH_KNIGHT: ITEM_MOD_STRENGTH,
    CLASS_HUNTER: ITEM_MOD_AGILITY,
    CLASS_ROGUE: ITEM_MOD_AGILITY,
    CLASS_PRIEST: ITEM_MOD_INTELLECT,
    CLASS_MAGE: ITEM_MOD_INTELLECT,
    CLASS_WARLOCK: ITEM_MOD_INTELLECT,
    CLASS_SHAMAN: ITEM_MOD_INTELLECT,
    CLASS_DRUID: ITEM_MOD_INTELLECT,
}

PRIMARY_STAT_WEIGHT = 100  # primary stat dominates; item_level only tiebreaks near-equal stats

# ItemClass (ItemTemplate.h) — only the ones the usability check below needs.
ITEM_CLASS_WEAPON = 2
ITEM_CLASS_ARMOR = 4

# ItemSubclassArmor (ItemTemplate.h).
ARMOR_SUBCLASS_CLOTH = 1
ARMOR_SUBCLASS_LEATHER = 2
ARMOR_SUBCLASS_MAIL = 3
ARMOR_SUBCLASS_PLATE = 4

# v1 simplification: each class's *highest* wearable armor subclass (real
# proficiency also covers shields/relics — not modeled here). A class
# missing from this table can still wear cloth (every class can).
CLASS_MAX_ARMOR_SUBCLASS = {
    CLASS_WARRIOR: ARMOR_SUBCLASS_PLATE,
    CLASS_PALADIN: ARMOR_SUBCLASS_PLATE,
    CLASS_DEATH_KNIGHT: ARMOR_SUBCLASS_PLATE,
    CLASS_HUNTER: ARMOR_SUBCLASS_MAIL,
    CLASS_SHAMAN: ARMOR_SUBCLASS_MAIL,
    CLASS_ROGUE: ARMOR_SUBCLASS_LEATHER,
    CLASS_DRUID: ARMOR_SUBCLASS_LEATHER,
    CLASS_PRIEST: ARMOR_SUBCLASS_CLOTH,
    CLASS_MAGE: ARMOR_SUBCLASS_CLOTH,
    CLASS_WARLOCK: ARMOR_SUBCLASS_CLOTH,
}


def primary_stat_value(item: dict, class_id: int) -> int:
    """Sum of every ItemStat entry on `item` (an agent.loot.
    parse_item_query_response() dict) matching class_id's primary stat.
    0 if the class is unknown or the item carries none of that stat."""
    stat_id = CLASS_PRIMARY_STAT.get(class_id)
    if stat_id is None:
        return 0
    return sum(s["value"] for s in item.get("stats", ()) if s["type"] == stat_id)


def score_item(item: dict, class_id: int) -> float:
    """v1 heuristic score — see module docstring."""
    return primary_stat_value(item, class_id) * PRIMARY_STAT_WEIGHT + item.get("item_level", 0)


def _class_bit(class_id: int) -> int:
    """ChrClasses bitmask bit for AllowableClass, per SharedDefines.h's
    CLASSMASK_ALL_PLAYABLE convention (bit = 1 << (class_id - 1))."""
    return 1 << (class_id - 1)


def usability_error(item: dict, class_id: int) -> str | None:
    """None if `item` can plausibly be equipped by `class_id`, else a
    short human-readable reason. v1 checks: ItemTemplate.AllowableClass
    bitmask (0/-1 means "no restriction" — TrinityCore's convention, not
    "no class can use it"), and armor-subclass proficiency
    (CLASS_MAX_ARMOR_SUBCLASS above). Does not check required_level,
    faction, or unique-equipped — those surface as an
    inventory_change_failure from the server on the actual equip attempt,
    same as any other equip failure this agent doesn't pre-validate."""
    allowable_class = item.get("allowable_class", -1)
    if allowable_class not in (0, -1) and not (allowable_class & _class_bit(class_id)):
        return f"item is not usable by class {class_id} (allowable_class mask {allowable_class:#x})"
    if item.get("class_") == ITEM_CLASS_ARMOR:
        subclass = item.get("subclass", ARMOR_SUBCLASS_CLOTH)
        max_subclass = CLASS_MAX_ARMOR_SUBCLASS.get(class_id, ARMOR_SUBCLASS_CLOTH)
        if subclass > max_subclass:
            return f"armor subclass {subclass} exceeds class {class_id}'s proficiency (max {max_subclass})"
    return None


def compare(item_a: dict, item_b: dict, class_id: int) -> dict:
    """Compare two items (agent.loot.parse_item_query_response() dicts,
    both for the same equip slot) for `class_id`. Returns {"winner":
    "a"|"b"|"tie", "score_a": float, "score_b": float, "reason": str}.
    Does not itself check equip-slot compatibility or usability — callers
    (agent.actions.CompareItemsAction) resolve both items for the same
    slot and check usability_error() separately."""
    score_a = score_item(item_a, class_id)
    score_b = score_item(item_b, class_id)
    stat_name = STAT_NAMES.get(CLASS_PRIMARY_STAT.get(class_id), "primary stat")
    name_a = item_a.get("name") or "item A"
    name_b = item_b.get("name") or "item B"

    if score_a == score_b:
        return {
            "winner": "tie", "score_a": score_a, "score_b": score_b,
            "reason": f"{name_a} and {name_b} score equally ({stat_name} + item level)",
        }

    if score_a > score_b:
        winner, winner_name, loser_name = "a", name_a, name_b
        winner_score, loser_score = score_a, score_b
    else:
        winner, winner_name, loser_name = "b", name_b, name_a
        winner_score, loser_score = score_b, score_a

    return {
        "winner": winner, "score_a": score_a, "score_b": score_b,
        "reason": (f"{winner_name} scores higher than {loser_name} "
                   f"({winner_score:.0f} vs {loser_score:.0f}, by {stat_name} + item level)"),
    }
