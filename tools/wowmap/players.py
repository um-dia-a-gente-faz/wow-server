"""Online players: positions on the zone and continent maps, class/race lookups."""
import state
from repo import players as players_repo

# Standard WoW class/race ids — stable for 3.3.5a.
CLASSES = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest",
           6: "Death Knight", 7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}
CLASS_COLORS = {1: "#C79C6E", 2: "#F58CBA", 3: "#ABD473", 4: "#FFF569",
                5: "#FFFFFF", 6: "#C41F3B", 7: "#0070DE", 8: "#69CCF0",
                9: "#9482C9", 11: "#FF7D0A"}
RACES = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "Night Elf", 5: "Undead", 6: "Tauren",
         7: "Gnome", 8: "Troll", 10: "Blood Elf", 11: "Draenei"}


def continent_position(t, cmap, zone, x, y):
    """(map id, x, y) of a saved position on its continent's map frame, or None.

    None in an instance, off the frame, and in the zones the game draws on another
    map's continent (Eversong, Azuremyst, ...): their coordinates are in map 530's
    space, which that continent's rect does not cover (docs/MAP_ENGINE_SPIKE.md)."""
    if zone and t.display_map.get(zone, -1) >= 0:
        return None
    n = t.continent_normalised(cmap, x, y)
    if n is None or not (0 <= n[0] <= 1 and 0 <= n[1] <= 1):
        return None
    return cmap, n[0], n[1]


def position_fields(t, cmap, zone, x, y):
    """Continent, subzone and in-game map coordinates for one saved position."""
    area = state.grid_areas().area_id(cmap, x, y)
    # Only a subzone of the saved zone; at zone borders the grid can disagree.
    sub = area if area and area != zone and t.area_parent.get(area) == zone else None
    coords = t.game_coords(zone, x, y) if zone else None
    return {
        "continent_name": t.continent_name(cmap, zone),
        "subzone": sub,
        "subzone_name": t.zone_name(sub) if sub else None,
        "map_coords": {"x": round(coords[0], 1), "y": round(coords[1], 1)} if coords else None,
    }


def fetch_players():
    out = []
    for (name, level, cls, race, cmap, zone, x, y, z, orient,
         inst, totaltime, online) in players_repo.online():
        t = state.tables()
        n = t.to_normalised(zone, float(x), float(y)) if zone else None
        c = None if inst else continent_position(t, cmap, zone, float(x), float(y))
        out.append({
            "name": name, "level": level, "class": cls,
            "class_name": CLASSES.get(cls, str(cls)),
            "class_color": CLASS_COLORS.get(cls, "#888888"),
            "race": race, "race_name": RACES.get(race, str(race)),
            "map": cmap, "zone": zone, "zone_name": t.zone_name(zone) if zone else "Unknown",
            "x": round(float(x), 2), "y": round(float(y), 2), "z": round(float(z), 2),
            "orientation": round(float(orient), 3),
            "instance": inst or 0,
            "in_world": not inst,
            "norm_x": round(n[0], 4) if n else None,
            "norm_y": round(n[1], 4) if n else None,
            # The same position on the continent map (UM-78), as /api/areas names it.
            "continent": f"c{c[0]}" if c else None,
            "cont_x": round(c[1], 4) if c else None,
            "cont_y": round(c[2], 4) if c else None,
            "playtime_seconds": totaltime,
            **position_fields(t, cmap, zone, float(x), float(y)),
        })
    return out


def summary():
    players = fetch_players()
    idle = [p for p in players if p["in_world"]]
    return {
        "online": len(players),
        "in_world": len(idle),
        "in_instance": len(players) - len(idle),
        "zones": sorted({p["zone_name"] for p in players if p["in_world"]}),
    }
