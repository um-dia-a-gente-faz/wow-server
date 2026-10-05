"""One character's inspect payload, and agent-or-human classification."""
import re

import agents
import state
from inventory import inventory_item, item_set_context
from players import CLASS_COLORS, CLASSES, RACES, position_fields
from repo import characters as characters_repo

# characters.power1..power7 in TrinityCore `Powers` enum order (SharedDefines.h:
# POWER_MANA=0 .. POWER_RUNIC_POWER=6; Player::SaveToDB writes GetPower(i) to power<i+1>).
POWER_NAMES = ("mana", "rage", "focus", "energy", "happiness", "rune", "runic_power")
# #174: agent characters live on accounts AGENT01..AGENT25 (agents/roster.json).
AGENT_ACCOUNT_RE = re.compile(r"AGENT(?:0[1-9]|1[0-9]|2[0-5])", re.IGNORECASE)


def is_agent_account(username):
    return bool(username) and AGENT_ACCOUNT_RE.fullmatch(username) is not None


def character_kind(name):
    """GET /api/character/<name>/kind: agent or human, from the character's account.
    None for an unknown character. A human's login name is never returned."""
    row = characters_repo.account_of(name)
    if not row:
        return None
    char_name, username = row
    if not is_agent_account(username):
        return {"name": char_name, "kind": "human"}
    return {"name": char_name, "kind": "agent", "account": username,
            "agent_api": char_name.lower() in agents.AGENT_APIS,
            "fleet_configured": bool(agents.AGENT_APIS)}


def _talent_names(n, spell):
    t = n.talent(spell) or {}
    return {"name": t.get("name"), "tree": t.get("tree"), "tree_order": t.get("tree_order"),
            "rank": t.get("rank")}


def fetch_character(name):
    """One character and its optional detail tables, or None."""
    d = characters_repo.detail(name, item_set_context)
    if d is None:
        return None
    (guid, char_name, level, race, cls, gender, zone, cmap, x, y, z, orient,
     money, totaltime, logout_time, online, health) = d["row"][:17]
    powers = d["row"][17:]
    inventory, set_context, stats = d["inventory"], d["set_context"], d["stats"]
    reputation = d["reputation"]

    t = state.tables()
    n = state.names()
    return {
        "name": char_name,
        "level": level,
        "race": race,
        "race_name": RACES.get(race, str(race)),
        "class": cls,
        "class_name": CLASSES.get(cls, str(cls)),
        "class_color": CLASS_COLORS.get(cls, "#888888"),
        "gender": gender,
        "zone": zone,
        "zone_name": t.zone_name(zone) if zone else "Unknown",
        "map": cmap,
        "map_name": t.map_name(cmap),
        "position_x": round(float(x), 2),
        "position_y": round(float(y), 2),
        "position_z": round(float(z), 2),
        "orientation": round(float(orient), 3),
        **position_fields(t, cmap, zone, float(x), float(y)),
        "money": money,
        "money_gold": float(money) / 10000.0,
        "totaltime": totaltime,
        "logout_time": logout_time,
        "online": bool(online),
        "health": health,
        "power": dict(zip(POWER_NAMES, powers)),
        # From characters.character_stats, written in the same save as health/power
        # above; None when the row doesn't exist (stat saving off, or the character
        # hasn't been saved since it was turned on).
        "max_health": stats[0][0] if stats else None,
        "max_power": dict(zip(POWER_NAMES, stats[0][1:])) if stats else None,
        "inventory": [inventory_item(row, n, *set_context) for row in inventory],
        # Names from the client DBCs (tools/dbc/names.py); null when an id is unknown.
        "talents": [
            {"spell": spell, "spec": spec, **_talent_names(n, spell)}
            for spell, spec in d["talents"]
        ],
        "reputation": [
            {"faction": faction, "standing": standing, "flags": flags,
             **n.reputation(faction, standing, race, cls)}
            for faction, standing, flags in reputation
        ],
        # The in-game reputation window: visible factions only, grouped and ordered
        # by the Faction.dbc parent tree (GameNames.reputation_panel).
        "reputation_panel": n.reputation_panel(reputation, race, cls),
        "achievements": [
            {"achievement": achievement, "date": date,
             **(n.achievement(achievement) or {"name": None, "points": None})}
            for achievement, date in d["achievements"]
        ],
    }
