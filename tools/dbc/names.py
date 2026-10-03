"""Name lookups for spells, talents, factions, achievements and item tooltip data from
the client DBCs.

Record layouts for WoW 3.3.5a (build 12340). Field indexes are taken from
TrinityCore branch 3.3.5, src/server/shared/DataStores/DBCStructure.h (the
`// <n>` comments) and checked against the format strings in DBCfmt.h
(`SpellEntryfmt`, `TalentEntryfmt`, `TalentTabEntryfmt`, `FactionEntryfmt`,
`Achievementfmt`), whose lengths are the expected field counts below. A file
whose field count differs is from another build and is skipped, not misread.

    Spell.dbc        234 fields   0 ID, 133 SpellIconID, 136-151 Name[16],
                                  153-168 NameSubtext[16] (the rank, "Rank 2")
    Talent.dbc        23 fields   0 ID, 1 TabID, 4-8 SpellRank[5]
    TalentTab.dbc     24 fields   0 ID, 1-16 Name[16], 20 ClassMask, 22 OrderIndex
    Faction.dbc       57 fields   0 ID, 1 ReputationIndex, 2-5 ReputationRaceMask[4],
                                  6-9 ReputationClassMask[4], 10-13 ReputationBase[4],
                                  18 ParentFactionID (the reputation window's
                                  headers: Classic > Horde > Orgrimmar), 23-38 Name[16]
    Achievement.dbc   62 fields   0 ID, 1 Faction, 4-19 Title[16], 38 Category,
                                  39 Points

Item tooltip tables (UM-80 follow-up, GH-119). Same sources: DBCStructure.h comments
and DBCfmt.h (`ItemSetEntryfmt`, `ItemRandomPropertiesfmt`, `ItemRandomSuffixfmt`,
`SpellItemEnchantmentfmt`, `GemPropertiesEntryfmt`, `SkillLinefmt`, `SpellDurationfmt`,
`SpellRadiusfmt`, `RandPropPointsfmt`). The counts and the indexes below were also
checked against the real build-12340 client files (an item set, a random property,
a suffix and a gem enchantment read back with the expected names).

    Spell.dbc (more) 40 DurationIndex, 71-73 Effect[3], 74-76 EffectDieSides[3],
                                  77-79 EffectRealPointsPerLevel[3], 80-82
                                  EffectBasePoints[3], 92-94 EffectRadiusIndex[3],
                                  95-97 EffectAura[3], 98-100 EffectAuraPeriod[3],
                                  104-106 EffectChainTargets[3], 35 ProcChance,
                                  36 ProcCharges, 170-185 Description[16] (commented
                                  out in DBCStructure.h but between NameSubtext and
                                  AuraDescription, 187-202)
    SpellDuration.dbc  4 fields   0 ID, 1 Duration (ms, -1 = until cancelled)
    SpellRadius.dbc    4 fields   0 ID, 1 Radius, 3 RadiusMax
    ItemSet.dbc       53 fields   0 ID, 1-16 Name[16], 18-27 ItemID[10],
                                  35-42 SetSpellID[8], 43-50 SetThreshold[8]
    ItemRandomProperties.dbc
                      24 fields   0 ID, 2-4 Enchantment[3], 7-22 Name[16]
    ItemRandomSuffix.dbc
                      29 fields   0 ID, 1-16 Name[16], 19-21 Enchantment[3],
                                  24-26 AllocationPct[3]
    SpellItemEnchantment.dbc
                      38 fields   0 ID, 2-4 Effect[3], 5-7 EffectPointsMin[3],
                                  11-13 EffectArg[3], 14-29 Name[16]
    GemProperties.dbc  5 fields   0 ID, 1 EnchantID, 4 Type (socket colour mask)
    SkillLine.dbc     56 fields   0 ID, 3-18 DisplayName[16]
    RandPropPoints.dbc
                      16 fields   0 item level, 1-5 Epic[5], 6-10 Superior[5],
                                  11-15 Good[5]

Reputation tiers are not DBC data: `characters.character_reputation.standing`
is stored *without* the faction's starting value. TrinityCore's ReputationMgr
(src/server/game/Reputation/ReputationMgr.cpp) computes
`GetReputation = ReputationBase[i] + standing`, where `i` is the first of the
four race/class slots matching the player (GetFactionDataIndexForRaceAndClass),
and maps it to a rank with ReputationRankThresholds.
"""
import logging
import os
import sys

from .spelltext import REGEN_AURAS, Effect, SpellInfo, Unresolved, render  # noqa: F401
from .wdbc import WdbcError, WdbcFile

log = logging.getLogger("dbc")

SPELL_FIELDS, SPELL_ICON, SPELL_NAME, SPELL_RANK = 234, 133, 136, 153
TALENT_FIELDS, TALENT_TAB, TALENT_RANKS = 23, 1, range(4, 9)
TALENTTAB_FIELDS, TALENTTAB_NAME, TALENTTAB_CLASSMASK, TALENTTAB_ORDER = 24, 1, 20, 22
FACTION_FIELDS, FACTION_REP_INDEX, FACTION_RACEMASK, FACTION_CLASSMASK = 57, 1, 2, 6
FACTION_REP_BASE, FACTION_PARENT, FACTION_NAME = 10, 18, 23
ACHIEVEMENT_FIELDS, ACHIEVEMENT_FACTION, ACHIEVEMENT_TITLE = 62, 1, 4
ACHIEVEMENT_CATEGORY, ACHIEVEMENT_POINTS = 38, 39
SPELL_DURATION_INDEX, SPELL_PROC_CHANCE, SPELL_PROC_CHARGES = 40, 35, 36
SPELL_EFFECT_DIE, SPELL_EFFECT_PER_LEVEL, SPELL_EFFECT_BASE = 74, 77, 80
SPELL_EFFECT_RADIUS, SPELL_EFFECT_AURA, SPELL_EFFECT_PERIOD, SPELL_EFFECT_CHAIN = 92, 95, 98, 104
SPELL_DESCRIPTION = 170
DURATION_FIELDS, RADIUS_FIELDS = 4, 4
ITEMSET_FIELDS, ITEMSET_NAME, ITEMSET_ITEMS, ITEMSET_SPELLS, ITEMSET_THRESHOLDS = 53, 1, 18, 35, 43
RANDPROP_FIELDS, RANDPROP_ENCHANT, RANDPROP_NAME = 24, 2, 7
RANDSUFFIX_FIELDS, RANDSUFFIX_NAME, RANDSUFFIX_ENCHANT, RANDSUFFIX_PCT = 29, 1, 19, 24
ENCHANT_FIELDS, ENCHANT_EFFECT, ENCHANT_POINTS, ENCHANT_ARG, ENCHANT_NAME = 38, 2, 5, 11, 14
GEMPROP_FIELDS, GEMPROP_ENCHANT, GEMPROP_TYPE = 5, 1, 4
SKILL_FIELDS, SKILL_NAME = 56, 3
RANDPOINTS_FIELDS = 16

# InventoryType -> RandPropPoints column group, from GetRandomPropertyPoints
# (src/server/game/Entities/Item/ItemEnchantmentMgr.cpp). Other types have no points.
_PROP_INDEX = {1: 0, 4: 0, 5: 0, 7: 0, 17: 0, 20: 0,          # head body chest legs 2h robe
               3: 1, 6: 1, 8: 1, 10: 1, 12: 1,                  # shoulders waist feet hands trinket
               2: 2, 9: 2, 11: 2, 14: 2, 16: 2, 23: 2,          # neck wrists finger shield cloak holdable
               13: 3, 21: 3, 22: 3,                             # weapon main-hand off-hand
               15: 4, 25: 4, 26: 4}                             # ranged thrown ranged-right
_QUALITY_GROUP = {2: 2, 3: 1, 4: 0}  # uncommon -> Good, rare -> Superior, epic -> Epic

# ReputationMgr::ReputationRankThresholds: the lower bound of each rank.
REPUTATION_TIERS = (
    (42000, "Exalted"), (21000, "Revered"), (9000, "Honored"), (3000, "Friendly"),
    (0, "Neutral"), (-3000, "Unfriendly"), (-6000, "Hostile"), (-42000, "Hated"),
)


def reputation_tier(value):
    """Tier name for a total reputation value (base + standing)."""
    for floor, name in REPUTATION_TIERS:
        if value >= floor:
            return name
    return "Hated"  # TrinityCore clamps at Reputation_Bottom (-42000)


# ReputationMgr.h `enum class ReputationFlags` (TrinityCore 3.3.5), stored as-is in
# characters.character_reputation.flags.
REP_VISIBLE, REP_AT_WAR, REP_HIDDEN, REP_HEADER = 0x01, 0x02, 0x04, 0x08
REP_PEACEFUL, REP_INACTIVE, REP_HEADER_SHOWS_BAR = 0x10, 0x20, 0x80
# Rank bounds from ReputationMgr::ReputationRankThresholds; Exalted ends at
# Reputation_Cap + 1 (43000). `rank_id` is the client's standingID, 1 Hated .. 8
# Exalted, the key of FACTION_BAR_COLORS in FrameXML ReputationFrame.lua.
RANK_BOUNDS = (-42000, -6000, -3000, 0, 3000, 9000, 21000, 42000, 43000)
RANK_NAMES = ("Hated", "Hostile", "Unfriendly", "Neutral", "Friendly", "Honored",
              "Revered", "Exalted")


def reputation_bar(value):
    """{"rank", "rank_id", "bar_value", "bar_max"} as the in-game bar shows it:
    Orgrimmar at 562 is Neutral 562/3000, Silvermoon at 4250 Friendly 1250/6000."""
    value = max(RANK_BOUNDS[0], min(value, RANK_BOUNDS[-1] - 1))
    i = 0
    while value >= RANK_BOUNDS[i + 1]:
        i += 1
    return {"rank": RANK_NAMES[i], "rank_id": i + 1,
            "bar_value": value - RANK_BOUNDS[i], "bar_max": RANK_BOUNDS[i + 1] - RANK_BOUNDS[i]}


def _open(dbc_dir, filename, fields, use_mmap=False):
    path = os.path.join(dbc_dir, filename)
    try:
        f = WdbcFile.open(path, use_mmap=use_mmap)
    except (OSError, WdbcError) as exc:
        log.warning("names: skipping %s: %s", filename, exc)
        return None
    if f.field_count != fields:
        log.warning("names: skipping %s: %d fields, expected %d (not build 12340?)",
                    filename, f.field_count, fields)
        return None
    return f


class GameNames:
    """ID -> name tables built once from DBC_DIR. Missing files leave a table empty."""

    def __init__(self, dbc_dir):
        intern = sys.intern  # many spells share a name; keep one copy of each
        self.spells = {}         # spell id -> (name, rank, icon id)
        self.talent_tabs = {}    # tab id -> (name, class mask, order index)
        self.talent_spells = {}  # rank spell id -> (talent id, tab id, rank 1..5)
        self.factions = {}       # faction id -> (name, rep index, race masks, class masks, bases, parent)
        self.achievements = {}   # achievement id -> (title, points, category, faction)

        # Spell.dbc stays open (memory-mapped) so tooltip text can be read on demand
        # from the other 200-odd columns without keeping a row per spell in memory.
        self._spell_file = None
        self._spell_rows = {}    # spell id -> record index in _spell_file
        self._spell_slot = 0
        self._spell_cache = {}   # spell id -> SpellInfo | None
        self.durations = {}      # SpellDuration id -> ms
        self.radii = {}          # SpellRadius id -> (radius, max radius)
        self.item_sets = {}      # item set id -> (name, item ids, ((threshold, spell id), ...))
        self.random_properties = {}  # ItemRandomProperties id -> (name, enchant ids)
        self.random_suffixes = {}    # ItemRandomSuffix id -> (name, ((enchant id, allocation pct), ...))
        self.enchants = {}       # SpellItemEnchantment id -> (name, ((effect, min points, arg), ...))
        self.gem_colors = {}     # enchantment id -> GemProperties.Type (socket colour mask)
        self.skills = {}         # SkillLine id -> name
        self.rand_prop_points = {}   # item level -> (epic[5], superior[5], good[5])

        f = _open(dbc_dir, "Spell.dbc", SPELL_FIELDS, use_mmap=True)
        if f:
            slot = f.detect_locale(SPELL_NAME)
            for r in f.records():
                spell_id = f.uint(r, 0)
                self._spell_rows[spell_id] = r
                self.spells[spell_id] = (
                    intern(f.loc_string(r, SPELL_NAME, slot)),
                    intern(f.loc_string(r, SPELL_RANK, slot)),
                    f.uint(r, SPELL_ICON),
                )
            self._spell_file, self._spell_slot = f, slot

        f = _open(dbc_dir, "SpellDuration.dbc", DURATION_FIELDS)
        if f:
            self.durations = {f.uint(r, 0): f.int(r, 1) for r in f.records()}

        f = _open(dbc_dir, "SpellRadius.dbc", RADIUS_FIELDS)
        if f:
            self.radii = {f.uint(r, 0): (f.float(r, 1), f.float(r, 3)) for r in f.records()}

        f = _open(dbc_dir, "ItemSet.dbc", ITEMSET_FIELDS)
        if f:
            slot = f.detect_locale(ITEMSET_NAME)
            for r in f.records():
                bonuses = sorted(
                    (f.uint(r, ITEMSET_THRESHOLDS + i), f.uint(r, ITEMSET_SPELLS + i))
                    for i in range(8) if f.uint(r, ITEMSET_SPELLS + i))
                self.item_sets[f.uint(r, 0)] = (
                    f.loc_string(r, ITEMSET_NAME, slot),
                    tuple(i for i in (f.uint(r, ITEMSET_ITEMS + k) for k in range(10)) if i),
                    tuple(bonuses),
                )

        f = _open(dbc_dir, "ItemRandomProperties.dbc", RANDPROP_FIELDS)
        if f:
            slot = f.detect_locale(RANDPROP_NAME)
            for r in f.records():
                self.random_properties[f.uint(r, 0)] = (
                    f.loc_string(r, RANDPROP_NAME, slot),
                    tuple(e for e in (f.uint(r, RANDPROP_ENCHANT + k) for k in range(3)) if e),
                )

        f = _open(dbc_dir, "ItemRandomSuffix.dbc", RANDSUFFIX_FIELDS)
        if f:
            slot = f.detect_locale(RANDSUFFIX_NAME)
            for r in f.records():
                self.random_suffixes[f.uint(r, 0)] = (
                    f.loc_string(r, RANDSUFFIX_NAME, slot),
                    tuple((f.uint(r, RANDSUFFIX_ENCHANT + k), f.uint(r, RANDSUFFIX_PCT + k))
                          for k in range(3) if f.uint(r, RANDSUFFIX_ENCHANT + k)),
                )

        f = _open(dbc_dir, "SpellItemEnchantment.dbc", ENCHANT_FIELDS)
        if f:
            slot = f.detect_locale(ENCHANT_NAME)
            for r in f.records():
                self.enchants[f.uint(r, 0)] = (
                    f.loc_string(r, ENCHANT_NAME, slot),
                    tuple((f.uint(r, ENCHANT_EFFECT + k), f.int(r, ENCHANT_POINTS + k),
                           f.uint(r, ENCHANT_ARG + k)) for k in range(3)),
                )

        f = _open(dbc_dir, "GemProperties.dbc", GEMPROP_FIELDS)
        if f:
            for r in f.records():
                if f.uint(r, GEMPROP_ENCHANT):
                    self.gem_colors[f.uint(r, GEMPROP_ENCHANT)] = f.uint(r, GEMPROP_TYPE)

        f = _open(dbc_dir, "SkillLine.dbc", SKILL_FIELDS)
        if f:
            slot = f.detect_locale(SKILL_NAME)
            for r in f.records():
                self.skills[f.uint(r, 0)] = f.loc_string(r, SKILL_NAME, slot)

        f = _open(dbc_dir, "RandPropPoints.dbc", RANDPOINTS_FIELDS)
        if f:
            for r in f.records():
                self.rand_prop_points[f.uint(r, 0)] = tuple(
                    tuple(f.uint(r, 1 + 5 * g + k) for k in range(5)) for g in range(3))

        f = _open(dbc_dir, "TalentTab.dbc", TALENTTAB_FIELDS)
        if f:
            slot = f.detect_locale(TALENTTAB_NAME)
            for r in f.records():
                self.talent_tabs[f.uint(r, 0)] = (
                    f.loc_string(r, TALENTTAB_NAME, slot),
                    f.uint(r, TALENTTAB_CLASSMASK),
                    f.uint(r, TALENTTAB_ORDER),
                )

        f = _open(dbc_dir, "Talent.dbc", TALENT_FIELDS)
        if f:
            for r in f.records():
                talent_id, tab = f.uint(r, 0), f.uint(r, TALENT_TAB)
                for rank, field in enumerate(TALENT_RANKS, start=1):
                    spell = f.uint(r, field)
                    if spell:
                        self.talent_spells[spell] = (talent_id, tab, rank)

        f = _open(dbc_dir, "Faction.dbc", FACTION_FIELDS)
        if f:
            slot = f.detect_locale(FACTION_NAME)
            for r in f.records():
                self.factions[f.uint(r, 0)] = (
                    f.loc_string(r, FACTION_NAME, slot),
                    f.int(r, FACTION_REP_INDEX),
                    tuple(f.uint(r, FACTION_RACEMASK + i) for i in range(4)),
                    tuple(f.uint(r, FACTION_CLASSMASK + i) for i in range(4)),
                    tuple(f.int(r, FACTION_REP_BASE + i) for i in range(4)),
                    f.uint(r, FACTION_PARENT),
                )

        f = _open(dbc_dir, "Achievement.dbc", ACHIEVEMENT_FIELDS)
        if f:
            slot = f.detect_locale(ACHIEVEMENT_TITLE)
            for r in f.records():
                self.achievements[f.uint(r, 0)] = (
                    f.loc_string(r, ACHIEVEMENT_TITLE, slot),
                    f.uint(r, ACHIEVEMENT_POINTS),
                    f.uint(r, ACHIEVEMENT_CATEGORY),
                    f.int(r, ACHIEVEMENT_FACTION),
                )

    # ---- lookups (None when unknown) ----------------------------------------
    def spell(self, spell_id):
        """{"name", "rank", "icon"} for a spell id."""
        s = self.spells.get(spell_id)
        return None if s is None else {"name": s[0], "rank": s[1], "icon": s[2]}

    def talent(self, spell_id):
        """{"name", "tree", "tab", "talent", "rank"} for a talent's rank spell id
        (what `characters.character_talent.spell` stores)."""
        t = self.talent_spells.get(spell_id)
        if t is None:
            return None
        talent_id, tab, rank = t
        spell = self.spells.get(spell_id)
        tab_row = self.talent_tabs.get(tab)
        return {
            "name": spell[0] if spell else None,
            "tree": tab_row[0] if tab_row else None,
            "tree_order": tab_row[2] if tab_row else None,
            "tab": tab,
            "talent": talent_id,
            "rank": rank,
        }

    def faction_name(self, faction_id):
        f = self.factions.get(faction_id)
        return f[0] if f else None

    def reputation(self, faction_id, standing, race, cls):
        """{"faction_name", "value", "tier"}: `standing` is the stored delta, `value`
        adds the faction's race/class starting reputation as TrinityCore does."""
        f = self.factions.get(faction_id)
        if f is None:
            return {"faction_name": None, "value": standing, "tier": reputation_tier(standing)}
        name, _rep_index, race_masks, class_masks, bases, _parent = f
        race_mask = 1 << (race - 1) if race else 0
        class_mask = 1 << (cls - 1) if cls else 0
        base = 0
        for i in range(4):
            if ((race_masks[i] & race_mask or (not race_masks[i] and class_masks[i]))
                    and (class_masks[i] & class_mask or not class_masks[i])):
                base = bases[i]
                break
        value = base + standing
        return {"faction_name": name, "value": value, "tier": reputation_tier(value)}

    def reputation_panel(self, rows, race, cls):
        """The character's reputation window as a tree, from `character_reputation`
        rows of (faction, standing, flags).

        Like the client: a faction is listed when its row has the Visible flag and
        not Hidden. Headers come from Faction.dbc ParentFactionID ("Classic" >
        "Horde" > "Orgrimmar") and are listed when something under them is, or when
        they carry their own bar (HeaderShowsBar, e.g. Horde Expedition) and are
        visible themselves. Visible factions flagged Inactive move to a trailing
        "Inactive" group, as in game. Siblings are sorted by name.

        Node: {"faction", "name", "header", "children" (headers only), "rep"}; `rep`
        is None for a header without a bar, otherwise {"value", "rank", "rank_id",
        "bar_value", "bar_max", "at_war"}."""
        state = {faction: (standing, flags) for faction, standing, flags in rows}

        def rep(faction):
            standing, flags = state[faction]
            value = self.reputation(faction, standing, race, cls)["value"]
            return {"value": value, **reputation_bar(value), "at_war": bool(flags & REP_AT_WAR)}

        def shown(faction):
            flags = state.get(faction, (0, 0))[1]
            return bool(flags & REP_VISIBLE) and not flags & REP_HIDDEN

        children = {}
        for faction, f in self.factions.items():
            parent = f[5] if f[5] in self.factions else 0  # orphan -> top level
            children.setdefault(parent, []).append(faction)

        def by_name(nodes):
            return sorted(nodes, key=lambda n: (n["name"].casefold(), n["faction"] or 0))

        inactive = []

        def build(faction):
            name = self.factions[faction][0]
            kids = [n for n in map(build, children.get(faction, ())) if n]
            if faction in children:
                has_bar = shown(faction) and bool(state[faction][1] & REP_HEADER_SHOWS_BAR)
                if not kids and not has_bar:
                    return None
                return {"faction": faction, "name": name, "header": True,
                        "children": by_name(kids), "rep": rep(faction) if has_bar else None}
            if not shown(faction):
                return None
            node = {"faction": faction, "name": name, "header": False, "rep": rep(faction)}
            if state[faction][1] & REP_INACTIVE:
                inactive.append(node)
                return None
            return node

        panel = [n for n in map(build, children.get(0, ())) if n]
        # Rows the DBC doesn't know (wrong build, missing file) still show, unnamed.
        panel += [{"faction": f, "name": "Unknown faction", "header": False, "rep": rep(f)}
                  for f in state if f not in self.factions and shown(f)]
        panel = by_name(panel)
        if inactive:
            panel.append({"faction": None, "name": "Inactive", "header": True,
                          "children": by_name(inactive), "rep": None})
        return panel

    def achievement(self, achievement_id):
        """{"name", "points"} for an achievement id."""
        a = self.achievements.get(achievement_id)
        return None if a is None else {"name": a[0], "points": a[1]}

    # ---- item tooltip data -----------------------------------------------------
    def spell_info(self, spell_id):
        """SpellInfo (see spelltext.py) for a spell id, or None if Spell.dbc doesn't
        have it. Read from the open file on first use."""
        if spell_id in self._spell_cache:
            return self._spell_cache[spell_id]
        f, r = self._spell_file, self._spell_rows.get(spell_id)
        info = None
        if f is not None and r is not None:
            effects = []
            for i in range(3):
                radius = self.radii.get(f.uint(r, SPELL_EFFECT_RADIUS + i), (0.0, 0.0))
                effects.append(Effect(
                    f.int(r, SPELL_EFFECT_BASE + i), f.int(r, SPELL_EFFECT_DIE + i),
                    f.float(r, SPELL_EFFECT_PER_LEVEL + i), f.uint(r, SPELL_EFFECT_AURA + i),
                    f.uint(r, SPELL_EFFECT_PERIOD + i), radius[0], radius[1],
                    f.uint(r, SPELL_EFFECT_CHAIN + i)))
            info = SpellInfo(
                self.spells[spell_id][0],
                f.loc_string(r, SPELL_DESCRIPTION, self._spell_slot),
                self.durations.get(f.uint(r, SPELL_DURATION_INDEX), 0),
                f.uint(r, SPELL_PROC_CHANCE), f.uint(r, SPELL_PROC_CHARGES), tuple(effects))
        if len(self._spell_cache) < 5000:  # item spells are few; never grow without bound
            self._spell_cache[spell_id] = info
        return info

    def spell_text(self, spell_id):
        """The spell's description as the tooltip shows it, or None when the spell is
        unknown, has no description, or uses variables we don't resolve (see
        spelltext.py: better no line than a wrong number)."""
        info = self.spell_info(spell_id)
        if info is None or not info.description:
            return None
        try:
            return render(info.description, info, self.spell_info) or None
        except Unresolved as exc:
            log.debug("spell %d description not resolved: %s", spell_id, exc)
            return None

    def item_set(self, set_id):
        """{"name", "items": [item ids], "bonuses": [(pieces needed, spell id), ...]}
        with the bonuses in ascending order of pieces."""
        s = self.item_sets.get(set_id)
        return None if s is None else {"name": s[0], "items": list(s[1]), "bonuses": list(s[2])}

    def random_property_name(self, random_property_id):
        """The "of the Bear" suffix for item_instance.randomPropertyId (positive:
        ItemRandomProperties, negative: -ItemRandomSuffix), or None."""
        if random_property_id > 0:
            row = self.random_properties.get(random_property_id)
        else:
            row = self.random_suffixes.get(-random_property_id)
        return row[0] if row else None

    def enchant_name(self, enchant_id, amount=None):
        """SpellItemEnchantment name ("+15 Agility", "Crusader"); a random suffix's
        "+$i Stamina" gets `amount` for the $i."""
        e = self.enchants.get(enchant_id)
        if e is None or not e[0]:
            return None
        return e[0].replace("$i", str(amount)) if amount is not None else e[0]

    def random_property_points(self, item_level, quality, inventory_type):
        """The suffix factor ItemRandomSuffix percentages apply to: TrinityCore's
        GetRandomPropertyPoints(itemLevel, quality, inventoryType) (the instance's
        ITEM_FIELD_PROPERTY_SEED is recomputed from it on load, Item::LoadFromDB).
        0 for items that can't carry a suffix."""
        prop, group = _PROP_INDEX.get(inventory_type), _QUALITY_GROUP.get(quality)
        row = self.rand_prop_points.get(item_level)
        if prop is None or group is None or row is None:
            return 0
        return row[group][prop]

    def random_property_lines(self, random_property_id, item_level, quality, inventory_type):
        """Enchantment lines a random property/suffix gives, e.g. ["+8 Agility",
        "+8 Stamina"]. Suffix amounts are AllocationPct * suffix factor / 10000
        (Player::ApplyEnchantment); [] when the id or the factor is unknown."""
        if random_property_id > 0:
            row = self.random_properties.get(random_property_id)
            names = [self.enchant_name(e) for e in row[1]] if row else []
        else:
            row = self.random_suffixes.get(-random_property_id)
            factor = self.random_property_points(item_level, quality, inventory_type)
            if not row or not factor:
                return []
            names = [self.enchant_name(e, pct * factor // 10000) for e, pct in row[1]]
        return [n for n in names if n]

    def skill_name(self, skill_id):
        return self.skills.get(skill_id)

    @staticmethod
    def reputation_rank_name(rank):
        """item_template.RequiredReputationRank (ReputationRank 0 Hated .. 7 Exalted)."""
        return RANK_NAMES[rank] if 0 <= rank < len(RANK_NAMES) else None
