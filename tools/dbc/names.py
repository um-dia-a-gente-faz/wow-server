"""Name lookups for spells, talents, factions and achievements from the client DBCs.

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
                                  18 ParentFactionID, 23-38 Name[16]
    Achievement.dbc   62 fields   0 ID, 1 Faction, 4-19 Title[16], 38 Category,
                                  39 Points

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

from .wdbc import WdbcError, WdbcFile

log = logging.getLogger("dbc")

SPELL_FIELDS, SPELL_ICON, SPELL_NAME, SPELL_RANK = 234, 133, 136, 153
TALENT_FIELDS, TALENT_TAB, TALENT_RANKS = 23, 1, range(4, 9)
TALENTTAB_FIELDS, TALENTTAB_NAME, TALENTTAB_CLASSMASK, TALENTTAB_ORDER = 24, 1, 20, 22
FACTION_FIELDS, FACTION_REP_INDEX, FACTION_RACEMASK, FACTION_CLASSMASK = 57, 1, 2, 6
FACTION_REP_BASE, FACTION_PARENT, FACTION_NAME = 10, 18, 23
ACHIEVEMENT_FIELDS, ACHIEVEMENT_FACTION, ACHIEVEMENT_TITLE = 62, 1, 4
ACHIEVEMENT_CATEGORY, ACHIEVEMENT_POINTS = 38, 39

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


def _open(dbc_dir, filename, fields):
    path = os.path.join(dbc_dir, filename)
    try:
        f = WdbcFile.open(path)
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

        f = _open(dbc_dir, "Spell.dbc", SPELL_FIELDS)
        if f:
            slot = f.detect_locale(SPELL_NAME)
            for r in f.records():
                self.spells[f.uint(r, 0)] = (
                    intern(f.loc_string(r, SPELL_NAME, slot)),
                    intern(f.loc_string(r, SPELL_RANK, slot)),
                    f.uint(r, SPELL_ICON),
                )

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

    def achievement(self, achievement_id):
        """{"name", "points"} for an achievement id."""
        a = self.achievements.get(achievement_id)
        return None if a is None else {"name": a[0], "points": a[1]}
