"""In-game style item tooltips built from `world.item_template` (+ item_instance).

Column names are the ones TrinityCore 3.3.5 reads in ObjectMgr::LoadItemTemplates
(src/server/game/Globals/ObjectMgr.cpp, `SELECT entry, class, subclass, ...`,
branch 3.3.5 @ 63d4d28, around line 2929). Enum values come from
src/server/game/Entities/Item/ItemTemplate.h of the same commit: ItemModType
(stat types), ItemBondingType, ItemFlags, ItemFieldFlags (ITEM_FIELD_FLAG_SOULBOUND
= 1, stored in item_instance.flags), InventoryType, ItemClass and the
ItemSubclass* enums. Damage schools are SpellSchools in
src/server/shared/DataStores/SharedDefines.h.

The line wording follows the 3.3.5a client's GlobalStrings.lua (ITEM_SPELL_TRIGGER_*,
ITEM_SET_NAME, ITEM_SET_BONUS, ITEM_SOCKET_BONUS, EMPTY_SOCKET_*, ITEM_MIN_SKILL,
ITEM_REQ_REPUTATION, ITEM_ENCHANT_TIME_LEFT_*). The DBC-backed lines (spell effects
with "Use:"/"Equip:"/"Chance on hit:", item sets, random "of the ..." suffixes and
their stats, enchants, gems and socket bonuses, required skill/reputation) need
`names`, a dbc.names.GameNames; without it they are left out.

Not shown yet: cooldown and charge suffixes on spell lines, spell triggers 4/5/6
(soulstone, no-delay use, learn), `$` variables the spell text resolver refuses
(dbc/spelltext.py: such a line is skipped, never guessed), set bonuses' required
skill, and "Requires ..." lines in red when the viewer doesn't meet them.

A tooltip is a list of lines, each a dict:
    {"left": str, "right": str (optional), "color": one of COLORS}
or a sell price line {"left": "Sell Price:", "money": copper, "color": "white"}.
`"quality"` as a colour means the item's quality colour (the name line).
"""

COLORS = ("quality", "white", "green", "yellow", "gray", "red")

# Columns read from world.item_template, in this order, after the fixed ones in
# app.fetch_character. Names verified against ObjectMgr::LoadItemTemplates (the
# SELECT lists spellid_1, spelltrigger_1, itemset, socketColor_1..3, socketBonus,
# RequiredSkill, RequiredSkillRank, RequiredReputationFaction/Rank and the rest).
COLUMNS = (
    ["class", "subclass", "InventoryType", "Flags", "bonding", "SellPrice",
     "ItemLevel", "RequiredLevel", "AllowableClass", "AllowableRace", "maxcount",
     "ContainerSlots", "StatsCount", "Quality", "itemset", "RequiredSkill",
     "RequiredSkillRank", "RequiredReputationFaction", "RequiredReputationRank",
     "socketColor_1", "socketColor_2", "socketColor_3", "socketBonus"]
    + [f"{k}_{i}" for i in range(1, 6) for k in ("spellid", "spelltrigger")]
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

# item_template.spelltrigger_N (ItemSpelltriggerType, ItemTemplate.h) -> GlobalStrings
# ITEM_SPELL_TRIGGER_ONUSE / ONEQUIP / ONPROC. 4 (soulstone), 5 (no-delay use) and 6
# (learn spell) are not shown: how the client words them isn't verified.
SPELL_TRIGGERS = {0: "Use:", 1: "Equip:", 2: "Chance on hit:"}

# EnchantmentSlot (src/server/game/Entities/Item/ItemDefines.h) and the layout of
# item_instance.enchantments: MAX_ENCHANTMENT_SLOT (12) groups of "id duration charges"
# (Item::LoadFromDB -> _LoadIntoDataField; ENCHANTMENT_*_OFFSET in Item.h).
PERM_SLOT, TEMP_SLOT, SOCK_SLOTS, PRISMATIC_SLOT = 0, 1, (2, 3, 4), 6
ENCHANT_SLOTS = 12
# ITEM_ENCHANTMENT_TYPE_PRISMATIC_SOCKET (DBCEnums.h): SpellItemEnchantment.Effect
# of the permanent enchant that adds a socket (Eternal Belt Buckle).
ENCHANT_PRISMATIC_SOCKET = 8
# SocketColor (ItemTemplate.h) -> GlobalStrings EMPTY_SOCKET_*
SOCKET_NAMES = {1: "Meta Socket", 2: "Red Socket", 4: "Yellow Socket", 8: "Blue Socket"}


def _get(t, key):
    return t.get(key) or 0


def _num(v):
    """Damage values are floats in the DB; print 3.0 as 3."""
    v = float(v)
    return str(int(v)) if v == int(v) else f"{v:g}"


def _restriction(label, mask, names, all_mask):
    if mask is None or mask == -1 or mask & all_mask == all_mask or not mask & all_mask:
        return None
    picked = [n for i, n in names.items() if mask & (1 << (i - 1))]
    return {"left": f"{label}: " + ", ".join(picked), "color": "white"}


def parse_enchantments(text):
    """item_instance.enchantments -> {slot: (enchant id, duration ms, charges)} for the
    slots that hold an enchantment. Malformed text gives what parsed cleanly."""
    try:
        nums = [int(x) for x in (text or "").split()]
    except ValueError:
        return {}
    out = {}
    for slot in range(min(len(nums) // 3, ENCHANT_SLOTS)):
        ench, duration, charges = nums[slot * 3:slot * 3 + 3]
        if ench:
            out[slot] = (ench, duration, charges)
    return out


def _time_left(ms):
    """GlobalStrings ITEM_ENCHANT_TIME_LEFT_*: "(59 min)", "(30 sec)", "(2 hours)"."""
    secs = ms // 1000
    for unit, size in (("day", 86400), ("hour", 3600), ("min", 60)):
        if secs >= size:
            n = secs // size
            return f"({n} {unit}{'s' if n != 1 and unit != 'min' else ''})"
    return f"({secs} sec)"


def _enchant_lines(t, names, enchants, random_property):
    """(enchant lines, socket lines, socket bonus line) for the DBC-backed part of the
    tooltip. Everything is green except empty sockets and an inactive socket bonus."""
    g = lambda k: _get(t, k)  # noqa: E731
    stats, sockets, bonus = [], [], None
    if random_property:
        stats += [{"left": text, "color": "green"} for text in names.random_property_lines(
            random_property, g("ItemLevel"), g("Quality"), g("InventoryType"))]

    perm = enchants.get(PERM_SLOT)
    extra_socket = False
    if perm:
        enchant = names.enchants.get(perm[0])
        if enchant and any(e[0] == ENCHANT_PRISMATIC_SOCKET for e in enchant[1]):
            extra_socket = True
        elif enchant and enchant[0]:
            stats.append({"left": enchant[0], "color": "green"})
    temp = enchants.get(TEMP_SLOT)
    if temp and names.enchant_name(temp[0]):
        text = names.enchant_name(temp[0]) + (f" {_time_left(temp[1])}" if temp[1] else "")
        stats.append({"left": text, "color": "green"})

    # Sockets: the template says the colours, the enchantment slots hold the gems
    # (a gem's enchantment id, GemProperties.EnchantID).
    fits = True
    colors = [g(f"socketColor_{i}") for i in (1, 2, 3)]
    for slot, color in zip(SOCK_SLOTS, colors):
        if not color:
            continue
        gem = enchants.get(slot)
        gem_name = names.enchant_name(gem[0]) if gem else None
        if gem_name:
            sockets.append({"left": gem_name, "color": "white"})
            fits = fits and bool(names.gem_colors.get(gem[0], 0) & color)
        else:
            sockets.append({"left": SOCKET_NAMES.get(color, "Prismatic Socket"), "color": "gray"})
            fits = False
    if extra_socket or PRISMATIC_SLOT in enchants:
        gem = enchants.get(PRISMATIC_SLOT)
        gem_name = names.enchant_name(gem[0]) if gem else None
        sockets.append({"left": gem_name, "color": "white"} if gem_name
                       else {"left": "Prismatic Socket", "color": "gray"})
    if g("socketBonus") and any(colors) and names.enchant_name(g("socketBonus")):
        bonus = {"left": f"Socket Bonus: {names.enchant_name(g('socketBonus'))}",
                 "color": "green" if fits else "gray"}
    return stats, sockets, bonus


def _spell_lines(t, names):
    lines = []
    for i in range(1, 6):
        label, spell = SPELL_TRIGGERS.get(_get(t, f"spelltrigger_{i}")), _get(t, f"spellid_{i}")
        text = names.spell_text(spell) if label and spell else None
        if text:
            lines.append({"left": f"{label} {text}", "color": "green"})
    return lines


def _requirement_lines(t, names):
    """"Requires Blacksmithing (300)" and "Requires Scryers - Honored" (ITEM_MIN_SKILL,
    ITEM_REQ_SKILL, ITEM_REQ_REPUTATION)."""
    lines = []
    skill = names.skill_name(_get(t, "RequiredSkill")) if _get(t, "RequiredSkill") else None
    if skill:
        rank = _get(t, "RequiredSkillRank")
        lines.append({"left": f"Requires {skill} ({rank})" if rank else f"Requires {skill}",
                      "color": "white"})
    faction = _get(t, "RequiredReputationFaction")
    if faction:
        fname, rname = names.faction_name(faction), names.reputation_rank_name(
            _get(t, "RequiredReputationRank"))
        if fname and rname:
            lines.append({"left": f"Requires {fname} - {rname}", "color": "white"})
    return lines


def _set_lines(t, names, equipped, item_names):
    """The item set block: "Name (2/5)", the pieces (equipped ones lit) and the
    "(2) Set: ..." bonuses (green once enough pieces are equipped)."""
    item_set = names.item_set(_get(t, "itemset")) if _get(t, "itemset") else None
    if not item_set:
        return []
    have = sum(1 for i in item_set["items"] if i in equipped)
    lines = [{"left": f"{item_set['name']} ({have}/{len(item_set['items'])})", "color": "yellow"}]
    for entry in item_set["items"]:
        lines.append({"left": (item_names or {}).get(entry) or f"Item {entry}",
                      "color": "yellow" if entry in equipped else "gray"})
    for threshold, spell in item_set["bonuses"]:
        text = names.spell_text(spell)
        if text:
            lines.append({"left": f"({threshold}) Set: {text}",
                          "color": "green" if have >= threshold else "gray"})
    return lines


def set_piece_entries(set_ids, names):
    """Item entries of the given item sets (ItemSet.dbc), for looking up their names."""
    return sorted({e for i in set_ids for e in (names.item_set(i) or {"items": ()})["items"]})


def tooltip(name, t, count=1, instance_flags=0, durability=None, names=None,
            random_property=0, enchantments="", equipped=(), item_names=None):
    """Tooltip lines for one item. `t` maps COLUMNS to values; every value may be None
    (an item_instance whose entry is missing from item_template gets just its name).

    `names` is a dbc.names.GameNames: with it the tooltip gets the DBC-backed lines
    (see the module docstring). `random_property` and `enchantments` are the
    item_instance columns of the same names; `equipped` is the set of item entries
    the character has equipped and `item_names` maps entries to names, both for the
    item set block."""
    g = lambda k: t.get(k) or 0  # noqa: E731
    suffix = names.random_property_name(random_property) if names and random_property else None
    lines = [{"left": f"{name} {suffix}" if suffix else name, "color": "quality"}]
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
    if names:
        enchant_lines, socket_lines, socket_bonus = _enchant_lines(
            t, names, parse_enchantments(enchantments), random_property)
        lines += enchant_lines + socket_lines + ([socket_bonus] if socket_bonus else [])

    if g("MaxDurability"):
        cur = g("MaxDurability") if durability is None else durability
        lines.append({"left": f"Durability {cur} / {g('MaxDurability')}", "color": "white"})
    for line in (_restriction("Classes", t.get("AllowableClass"), CLASS_NAMES, CLASSMASK_ALL),
                 _restriction("Races", t.get("AllowableRace"), RACE_NAMES, RACEMASK_ALL)):
        if line:
            lines.append(line)
    if g("RequiredLevel") > 0:
        lines.append({"left": f"Requires Level {g('RequiredLevel')}", "color": "white"})
    if names:
        lines += _requirement_lines(t, names)
    if inv and g("ItemLevel"):
        lines.append({"left": f"Item Level {g('ItemLevel')}", "color": "yellow"})
    lines += [{"left": e, "color": "green"} for e in equip]
    if names:
        lines += _spell_lines(t, names)
    if t.get("description"):
        lines.append({"left": f"“{t['description']}”", "color": "yellow"})
    if names:
        lines += _set_lines(t, names, set(equipped), item_names)
    if g("SellPrice"):
        # The game shows the price of the whole stack.
        lines.append({"left": "Sell Price:", "money": g("SellPrice") * max(count or 1, 1),
                      "color": "white"})
    return lines
