"""In-game style item tooltips built from `world.item_template` (+ item_instance).

Column names are the ones TrinityCore 3.3.5 reads in ObjectMgr::LoadItemTemplates
(src/server/game/Globals/ObjectMgr.cpp, `SELECT entry, class, subclass, ...`,
branch 3.3.5 @ 63d4d28, around line 2929). Enum values come from
src/server/game/Entities/Item/ItemTemplate.h of the same commit: ItemModType
(stat types), ItemBondingType, ItemFlags, ItemFieldFlags (ITEM_FIELD_FLAG_SOULBOUND
= 1, stored in item_instance.flags), InventoryType, ItemClass and the
ItemSubclass* enums. Damage schools are SpellSchools in
src/server/shared/DataStores/SharedDefines.h.

The line wording follows the 3.3.5a client's tooltip strings as best we know them
without the client's GlobalStrings.lua at hand; spell effects ("Use:", "Equip:"
with a spell), item set names and random "of the ..." suffixes need more DBC data
and are not shown yet.

A tooltip is a list of lines, each a dict:
    {"left": str, "right": str (optional), "color": one of COLORS}
or a sell price line {"left": "Sell Price:", "money": copper, "color": "white"}.
`"quality"` as a colour means the item's quality colour (the name line).
"""

COLORS = ("quality", "white", "green", "yellow", "gray", "red")

# Columns read from world.item_template, in this order, after the fixed ones in
# app.fetch_character. Names verified against ObjectMgr::LoadItemTemplates and the
# live world database.
COLUMNS = (
    ["class", "subclass", "InventoryType", "Flags", "bonding", "SellPrice",
     "ItemLevel", "RequiredLevel", "AllowableClass", "AllowableRace", "maxcount",
     "ContainerSlots", "StatsCount"]
    + [f"{k}{i}" for i in range(1, 11) for k in ("stat_type", "stat_value")]
    + ["dmg_min1", "dmg_max1", "dmg_type1", "dmg_min2", "dmg_max2", "dmg_type2",
       "armor", "holy_res", "fire_res", "nature_res", "frost_res", "shadow_res",
       "arcane_res", "delay", "block", "MaxDurability", "description", "startquest"]
)

# ItemClass
CLASS_CONTAINER, CLASS_WEAPON, CLASS_ARMOR, CLASS_QUIVER = 1, 2, 4, 11

# ItemBondingType 1..4 (5 is "not used in game").
BONDING = {1: "Binds when picked up", 2: "Binds when equipped", 3: "Binds when used",
           4: "Quest Item"}

# ItemFlags / ItemFieldFlags
FLAG_CONJURED, FLAG_HEROIC, FLAG_UNIQUE_EQUIPPABLE = 0x2, 0x8, 0x80000
FIELD_FLAG_SOULBOUND = 0x1

# InventoryType -> the slot text on the tooltip's left.
INVENTORY_TYPES = {
    1: "Head", 2: "Neck", 3: "Shoulder", 4: "Shirt", 5: "Chest", 6: "Waist", 7: "Legs",
    8: "Feet", 9: "Wrist", 10: "Hands", 11: "Finger", 12: "Trinket", 13: "One-Hand",
    14: "Off Hand", 15: "Ranged", 16: "Back", 17: "Two-Hand", 19: "Tabard", 20: "Chest",
    21: "Main Hand", 22: "Off Hand", 23: "Held In Off-hand", 24: "Projectile",
    25: "Thrown", 26: "Ranged", 28: "Relic",
}
INVTYPE_BAG, INVTYPE_QUIVER = 18, 27

# ItemSubclassWeapon -> the type text on the right.
WEAPON_TYPES = {0: "Axe", 1: "Axe", 2: "Bow", 3: "Gun", 4: "Mace", 5: "Mace",
                6: "Polearm", 7: "Sword", 8: "Sword", 10: "Staff", 11: "Exotic",
                12: "Exotic", 13: "Fist Weapon", 14: "Miscellaneous", 15: "Dagger",
                16: "Thrown", 17: "Spear", 18: "Crossbow", 19: "Wand", 20: "Fishing Pole"}
# ItemSubclassArmor (0 miscellaneous shows no type).
ARMOR_TYPES = {1: "Cloth", 2: "Leather", 3: "Mail", 4: "Plate", 5: "Buckler",
               6: "Shield", 7: "Libram", 8: "Idol", 9: "Totem", 10: "Sigil"}
# ItemSubclassContainer -> "<n> Slot <kind>".
CONTAINER_KINDS = {0: "Bag", 1: "Soul Bag", 2: "Herb Bag", 3: "Enchanting Bag",
                   4: "Engineering Bag", 5: "Gem Bag", 6: "Mining Bag",
                   7: "Leatherworking Bag", 8: "Inscription Bag"}
QUIVER_KINDS = {2: "Quiver", 3: "Ammo Pouch"}

# SpellSchools 1..6 (0 physical has no word).
SCHOOLS = {1: "Holy", 2: "Fire", 3: "Nature", 4: "Frost", 5: "Shadow", 6: "Arcane"}
RESISTANCES = (("holy_res", "Holy"), ("fire_res", "Fire"), ("nature_res", "Nature"),
               ("frost_res", "Frost"), ("shadow_res", "Shadow"), ("arcane_res", "Arcane"))

# ItemModType: primary stats are white "+N Stat" lines ...
PRIMARY_STATS = {0: "Mana", 1: "Health", 3: "Agility", 4: "Strength", 5: "Intellect",
                 6: "Spirit", 7: "Stamina"}
# ... everything else is a green "Equip:" line.
EQUIP_STATS = {
    12: "Increases defense rating by {}.",
    13: "Increases your dodge rating by {}.",
    14: "Increases your parry rating by {}.",
    15: "Increases your shield block rating by {}.",
    16: "Improves melee hit rating by {}.",
    17: "Improves ranged hit rating by {}.",
    18: "Improves spell hit rating by {}.",
    19: "Improves melee critical strike rating by {}.",
    20: "Improves ranged critical strike rating by {}.",
    21: "Improves spell critical strike rating by {}.",
    22: "Improves melee hit avoidance rating by {}.",
    23: "Improves ranged hit avoidance rating by {}.",
    24: "Improves spell hit avoidance rating by {}.",
    25: "Improves melee critical avoidance rating by {}.",
    26: "Improves ranged critical avoidance rating by {}.",
    27: "Improves spell critical avoidance rating by {}.",
    28: "Improves melee haste rating by {}.",
    29: "Improves ranged haste rating by {}.",
    30: "Improves spell haste rating by {}.",
    31: "Increases your hit rating by {}.",
    32: "Increases your critical strike rating by {}.",
    33: "Improves hit avoidance rating by {}.",
    34: "Improves critical avoidance rating by {}.",
    35: "Increases your resilience rating by {}.",
    36: "Increases your haste rating by {}.",
    37: "Increases your expertise rating by {}.",
    38: "Increases attack power by {}.",
    39: "Increases ranged attack power by {}.",
    41: "Increases healing done by up to {}.",
    42: "Increases damage done by magical spells and effects by up to {}.",
    43: "Restores {} mana per 5 sec.",
    44: "Increases your armor penetration rating by {}.",
    45: "Increases spell power by {}.",
    46: "Restores {} health per 5 sec.",
    47: "Increases spell penetration by {}.",
    48: "Increases the block value of your shield by {}.",
}

# Class / race ids -> names, and the playable masks (SharedDefines.h
# CLASSMASK_ALL_PLAYABLE / RACEMASK_ALL_PLAYABLE): a mask covering all of them
# means "no restriction" and prints nothing.
CLASS_NAMES = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest",
               6: "Death Knight", 7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}
RACE_NAMES = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "Night Elf", 5: "Undead", 6: "Tauren",
              7: "Gnome", 8: "Troll", 10: "Blood Elf", 11: "Draenei"}
CLASSMASK_ALL = sum(1 << (c - 1) for c in CLASS_NAMES)
RACEMASK_ALL = sum(1 << (r - 1) for r in RACE_NAMES)


def _num(v):
    """Damage values are floats in the DB; print 3.0 as 3."""
    v = float(v)
    return str(int(v)) if v == int(v) else f"{v:g}"


def _restriction(label, mask, names, all_mask):
    if mask is None or mask == -1 or mask & all_mask == all_mask or not mask & all_mask:
        return None
    picked = [n for i, n in names.items() if mask & (1 << (i - 1))]
    return {"left": f"{label}: " + ", ".join(picked), "color": "white"}


def tooltip(name, t, count=1, instance_flags=0, durability=None):
    """Tooltip lines for one item. `t` maps COLUMNS to values; every value may be None
    (an item_instance whose entry is missing from item_template gets just its name)."""
    g = lambda k: t.get(k) or 0  # noqa: E731
    lines = [{"left": name, "color": "quality"}]
    if not t or t.get("class") is None:
        return lines

    flags, cls, sub, inv = g("Flags"), g("class"), g("subclass"), g("InventoryType")
    if flags & FLAG_HEROIC:
        lines.append({"left": "Heroic", "color": "green"})
    if flags & FLAG_CONJURED:
        lines.append({"left": "Conjured Item", "color": "white"})
    if instance_flags and instance_flags & FIELD_FLAG_SOULBOUND:
        lines.append({"left": "Soulbound", "color": "white"})
    elif g("bonding") in BONDING:
        lines.append({"left": BONDING[g("bonding")], "color": "white"})
    if g("maxcount") == 1:
        lines.append({"left": "Unique", "color": "white"})
    elif g("maxcount") > 1:
        lines.append({"left": f"Unique ({g('maxcount')})", "color": "white"})
    elif flags & FLAG_UNIQUE_EQUIPPABLE:
        lines.append({"left": "Unique-Equipped", "color": "white"})
    if g("startquest"):
        lines.append({"left": "This Item Begins a Quest", "color": "white"})

    # Slot and type ("Two-Hand    Sword"), or the bag size.
    if inv in (INVTYPE_BAG, INVTYPE_QUIVER) or cls in (CLASS_CONTAINER, CLASS_QUIVER):
        kinds = QUIVER_KINDS if cls == CLASS_QUIVER else CONTAINER_KINDS
        if g("ContainerSlots"):
            lines.append({"left": f"{g('ContainerSlots')} Slot {kinds.get(sub, 'Bag')}",
                          "color": "white"})
    elif inv in INVENTORY_TYPES:
        right = (WEAPON_TYPES.get(sub) if cls == CLASS_WEAPON
                 else ARMOR_TYPES.get(sub) if cls == CLASS_ARMOR else None)
        line = {"left": INVENTORY_TYPES[inv], "color": "white"}
        if right:
            line["right"] = right
        lines.append(line)

    # Weapon damage, speed and DPS.
    delay = g("delay")
    dmg = []
    for i in (1, 2):
        lo, hi = float(g(f"dmg_min{i}")), float(g(f"dmg_max{i}"))
        if hi > 0:
            school = SCHOOLS.get(g(f"dmg_type{i}"))
            text = _num(lo) if lo == hi else f"{_num(lo)} - {_num(hi)}"
            text += f" {school} Damage" if school else " Damage"
            dmg.append((lo, hi, text if i == 1 else "+ " + text))
    if dmg and delay:
        lines.append({"left": dmg[0][2], "right": f"Speed {delay / 1000:.2f}", "color": "white"})
        lines += [{"left": d[2], "color": "white"} for d in dmg[1:]]
        dps = sum((lo + hi) / 2 for lo, hi, _ in dmg) / (delay / 1000)
        lines.append({"left": f"({dps:.1f} damage per second)", "color": "white"})

    if g("armor"):
        lines.append({"left": f"{g('armor')} Armor", "color": "white"})
    if g("block"):
        lines.append({"left": f"{g('block')} Block", "color": "white"})

    equip = []
    for i in range(1, min(int(g("StatsCount")), 10) + 1):
        stype, value = g(f"stat_type{i}"), g(f"stat_value{i}")
        if not value:
            continue
        if stype in PRIMARY_STATS:
            lines.append({"left": f"{value:+d} {PRIMARY_STATS[stype]}", "color": "white"})
        elif stype in EQUIP_STATS:
            equip.append("Equip: " + EQUIP_STATS[stype].format(value))
    for col, school in RESISTANCES:
        if g(col):
            lines.append({"left": f"{g(col):+d} {school} Resistance", "color": "white"})

    if g("MaxDurability"):
        cur = g("MaxDurability") if durability is None else durability
        lines.append({"left": f"Durability {cur} / {g('MaxDurability')}", "color": "white"})
    for line in (_restriction("Classes", t.get("AllowableClass"), CLASS_NAMES, CLASSMASK_ALL),
                 _restriction("Races", t.get("AllowableRace"), RACE_NAMES, RACEMASK_ALL)):
        if line:
            lines.append(line)
    if g("RequiredLevel") > 0:
        lines.append({"left": f"Requires Level {g('RequiredLevel')}", "color": "white"})
    if inv and g("ItemLevel"):
        lines.append({"left": f"Item Level {g('ItemLevel')}", "color": "yellow"})
    lines += [{"left": e, "color": "green"} for e in equip]
    if t.get("description"):
        lines.append({"left": f"“{t['description']}”", "color": "yellow"})
    if g("SellPrice"):
        # The game shows the price of the whole stack.
        lines.append({"left": "Sell Price:", "money": g("SellPrice") * max(count or 1, 1),
                      "color": "white"})
    return lines
