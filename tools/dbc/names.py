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
                                  18 ParentFactionID (the reputation window's
                                  headers: Classic > Horde > Orgrimmar), 23-38 Name[16]
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
