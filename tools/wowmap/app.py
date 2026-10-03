#!/usr/bin/env python3
"""wowmap — live map of online players for a TrinityCore 3.3.5a server.

Serves:
    GET /                     the map page (single file, no build step)
    GET /api/players          online players with world + normalised coords
    GET /api/character/<name> one character's state, inventory and progression
    GET /api/character/<name>/activity?limit=50  recent activity feed (UM-76, activity.py)
    GET /api/areas?map=<id>   zone tiles: rect, name, whether art is available, subzones
                              and `continents`: the continent maps and their zones' boxes
    POST /api/calibrate       save a per-zone pixel offset
    GET /api/agents           names of agents with an observability API (UM-50)
    GET /api/agent/<name>/<view>  proxy to that agent's read-only GET /<view>
                              (healthz, state, perception, brain)
    GET /maps/<file>          extracted zone map images (static)
    GET /icons/<file>         extracted item icons (static, see item_icons.py)
    GET /healthz              liveness

Coordinates come from `characters.characters`; the world->normalised transform comes
from the client DBCs (see transform.py). Map art is extracted from the client MPQs by
extract_maps.py and served from MAPS_DIR.

Env:
    MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD   as elsewhere in this repo
    DBC_DIR     default /dbc        (WorldMapArea.dbc, AreaTable.dbc, Map.dbc,
                                     optional WorldMapOverlay.dbc for subzones; names come
                                     from Spell, Talent, TalentTab, Faction, Achievement)
    MAPS_DIR    default /maps       (extracted PNGs)
    ICONS_DIR   default /icons      (item icon PNGs from extract_icons.py)
    GRID_MAPS_DIR default /server-maps (the worldserver's maps/*.map, for subzones)
    LISTEN_PORT default 9400
    CHAT_FEED_URL default ""        (derived from the page's own hostname at :9500)
    ACTIVITY_DB   default /data/activity.sqlite3   activity feed store ("" disables the feed)
    ACTIVITY_CHAT_FEED_URL default http://chat-feed:9500  chat-feed as seen from this
                                    process ("" = no chat in the activity feed)
    AUDIT_DIR     default /audit    agents' UM-51 decision logs ("" = no agent events)
    AGENT_API_URLS default ""       "Name=http://host:9601,Name2=http://host:9602" — agent
                                    observability APIs (agent/http_api.py) to proxy
"""
import json
import logging
import math
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
from urllib.parse import urlparse, parse_qs, unquote

import pymysql

import activity as activity_feed
import item_icons
import fog
import item_tooltip
from overlays import load_overlays, subzones
from transform import MAP_FRAME_H, MAP_FRAME_W, DbcTables, GridAreas

# The shared DBC reader lives in tools/dbc (copied to /app/dbc in the image).
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dbc.names import GameNames  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wowmap")

MYSQL: dict = dict(
    host=os.environ.get("MYSQL_HOST", "trinitycore-db"),
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ.get("MYSQL_USER", "root"),
    password=os.environ.get("MYSQL_PASSWORD", ""),
    charset="utf8mb4",
    autocommit=True,
)
DBC_DIR = os.environ.get("DBC_DIR", "/dbc")
MAPS_DIR = os.environ.get("MAPS_DIR", "/maps")
ICONS_DIR = os.environ.get("ICONS_DIR", "/icons")
GRID_MAPS_DIR = os.environ.get("GRID_MAPS_DIR", "/server-maps")
# Vendored front-end files (Leaflet), served under /static/. An explicit list, so
# nothing else in the directory is ever reachable.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {"leaflet.js": "text/javascript; charset=utf-8",
                "leaflet.css": "text/css; charset=utf-8"}
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9400"))
CALIBRATION_FILE = os.environ.get(
    "CALIBRATION_FILE", os.path.join(os.path.dirname(__file__), "calibration.json")
)
MAX_CALIBRATION_PAYLOAD_BYTES = 65536
MAX_FOG_OVERLAYS = 128   # ids accepted in /maps/<area>.png?explored=; no zone has that many
# Empty means "derive from the page's own hostname at :9500" (see CHAT_JS below);
# set this only when chat-feed isn't reachable on the same host as wowmap.
CHAT_FEED_URL = os.environ.get("CHAT_FEED_URL", "")
ACTIVITY_DB = os.environ.get("ACTIVITY_DB", "/data/activity.sqlite3")
ACTIVITY_CHAT_FEED_URL = os.environ.get("ACTIVITY_CHAT_FEED_URL", "http://chat-feed:9500")
AUDIT_DIR = os.environ.get("AUDIT_DIR", "/audit")
# Set in __main__ (see start_activity) so importing this module starts no threads.
activity = None


def parse_agent_urls(spec):
    """AGENT_API_URLS -> {lowercased name: (name, base url)}. Malformed
    entries are skipped; only http(s) URLs are accepted."""
    out = {}
    for part in (spec or "").split(","):
        name, sep, url = part.strip().partition("=")
        name, url = name.strip(), url.strip().rstrip("/")
        if sep and name and url.startswith(("http://", "https://")):
            out[name.lower()] = (name, url)
    return out


# UM-50: agents' read-only observability APIs. wowmap proxies them so the page
# needs no extra ports or CORS; only these GET views are ever forwarded.
AGENT_APIS = parse_agent_urls(os.environ.get("AGENT_API_URLS", ""))
AGENT_VIEWS = ("healthz", "state", "perception", "brain")
AGENT_PROXY_TIMEOUT_S = 3
MAX_AGENT_RESPONSE_BYTES = 4 * 1024 * 1024


def fetch_agent_view(name, view, n=None):
    """GET <agent base>/<view> and return (status, body bytes). Unknown agent
    or view -> 404; an unreachable agent -> 502. The agent's URL is never
    echoed back to the browser."""
    entry = AGENT_APIS.get((name or "").lower())
    if entry is None or view not in AGENT_VIEWS:
        return 404, json.dumps({"error": "unknown agent or view"}).encode()
    url = f"{entry[1]}/{view}"
    if view == "brain" and n is not None:
        url += f"?n={int(n)}"
    try:
        with urllib.request.urlopen(url, timeout=AGENT_PROXY_TIMEOUT_S) as r:
            return r.status, r.read(MAX_AGENT_RESPONSE_BYTES)
    except urllib.error.HTTPError as e:
        e.close()
        return 502, json.dumps({"error": f"agent API returned {e.code}"}).encode()
    except (OSError, ValueError) as e:
        log.info("agent API %s unreachable: %s", entry[0], e)
        return 502, json.dumps({"error": "agent API unreachable"}).encode()

# Standard WoW class/race ids — stable for 3.3.5a.
CLASSES = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest",
           6: "Death Knight", 7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}
CLASS_COLORS = {1: "#C79C6E", 2: "#F58CBA", 3: "#ABD473", 4: "#FFF569",
                5: "#FFFFFF", 6: "#C41F3B", 7: "#0070DE", 8: "#69CCF0",
                9: "#9482C9", 11: "#FF7D0A"}
RACES = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "Night Elf", 5: "Undead", 6: "Tauren",
         7: "Gnome", 8: "Troll", 10: "Blood Elf", 11: "Draenei"}
# characters.power1..power7 in TrinityCore `Powers` enum order (SharedDefines.h:
# POWER_MANA=0 .. POWER_RUNIC_POWER=6; Player::SaveToDB writes GetPower(i) to power<i+1>).
POWER_NAMES = ("mana", "rage", "focus", "energy", "happiness", "rune", "runic_power")
# What item_icons.icon_file() produces; anything else under /icons/ is a 404.
ICON_FILE_RE = re.compile(r"[a-z0-9_\-]+\.png")

_tables = None
_overlays = None
_display_icons = None
_icons_lock = threading.Lock()
_names = None
_names_lock = threading.Lock()
_calibration_lock = threading.Lock()


def load_calibrations():
    """Load the small operator-maintained per-zone pixel-offset store."""
    try:
        with open(CALIBRATION_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("top-level value must be an object")
        return {
            str(area_id): {"dx": float(value.get("dx", 0)), "dy": float(value.get("dy", 0))}
            for area_id, value in data.items()
            if isinstance(value, dict)
        }
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        log.warning("could not load calibration store %s: %s", CALIBRATION_FILE, exc)
        return {}


calibrations = load_calibrations()


def save_calibration(area_id, dx, dy):
    """Read-modify-write one zone's offset; callers receive the saved value."""
    value = {"dx": round(dx, 2), "dy": round(dy, 2)}
    with _calibration_lock:
        calibrations[str(area_id)] = value
        directory = os.path.dirname(CALIBRATION_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = f"{CALIBRATION_FILE}.tmp"
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(calibrations, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(temporary, CALIBRATION_FILE)
    return value


def tables():
    global _tables
    if _tables is None:
        _tables = DbcTables(DBC_DIR)
    return _tables


def overlays():
    """WorldMapOverlay rows by WorldMapArea ID ({} without the DBC); see overlays.py."""
    global _overlays
    if _overlays is None:
        _overlays = load_overlays(DBC_DIR)
    return _overlays


def display_icons():
    """{ItemDisplayInfo id: icon name}, loaded once; empty if the DBC is unavailable."""
    global _display_icons
    with _icons_lock:
        if _display_icons is None:
            path = os.path.join(DBC_DIR, "ItemDisplayInfo.dbc")
            try:
                _display_icons = item_icons.load_display_icons(path)
            except (OSError, ValueError) as e:
                log.warning("item icons disabled: %s", e)
                _display_icons = {}
        return _display_icons


def icon_url(display_id):
    """`/icons/<file>.png` for an item_template.displayid, or None when there is no
    icon (unknown display id, or the PNG wasn't extracted). The UI shows a placeholder."""
    name = display_icons().get(display_id) if display_id else None
    fn = item_icons.icon_file(name) if name else ""
    if fn and os.path.isfile(os.path.join(ICONS_DIR, fn)):
        return "/icons/" + fn
    return None


def names():
    """Spell/talent/faction/achievement names, built once (~1 s, ~15 MB)."""
    global _names
    with _names_lock:
        if _names is None:
            t0 = time.monotonic()
            _names = GameNames(DBC_DIR)
            log.info("names: %d spells, %d talents, %d factions, %d achievements in %.1fs",
                     len(_names.spells), len(_names.talent_spells), len(_names.factions),
                     len(_names.achievements), time.monotonic() - t0)
    return _names


def db():
    return pymysql.connect(**MYSQL)


_grid_areas = None


def grid_areas():
    global _grid_areas
    if _grid_areas is None:
        _grid_areas = GridAreas(GRID_MAPS_DIR)
    return _grid_areas


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
    area = grid_areas().area_id(cmap, x, y)
    # Only a subzone of the saved zone; at zone borders the grid can disagree.
    sub = area if area and area != zone and t.area_parent.get(area) == zone else None
    coords = t.game_coords(zone, x, y) if zone else None
    return {
        "continent_name": t.continent_name(cmap, zone),
        "subzone": sub,
        "subzone_name": t.zone_name(sub) if sub else None,
        "map_coords": {"x": round(coords[0], 1), "y": round(coords[1], 1)} if coords else None,
    }


# ---------------------------------------------------------------- queries
def fetch_players():
    sql = """
        SELECT c.name, c.level, c.class, c.race, c.map, c.zone,
               c.position_x, c.position_y, c.position_z, c.orientation,
               c.instance_id, c.totaltime, c.online
        FROM characters.characters c
        WHERE c.online = 1
        ORDER BY c.name
    """
    out = []
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql)
        for (name, level, cls, race, cmap, zone, x, y, z, orient,
             inst, totaltime, online) in cur.fetchall():
            t = tables()
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


def zone_overlays(area_id):
    """A zone's WorldMapOverlay rows by its AreaTable ID. As in fetch_areas(), the
    first WorldMapArea row of an area is the one the page shows."""
    for r in tables()._wm:  # noqa: SLF001 - internal read, kept local to this module
        if r[3] and r[2] == area_id:
            return overlays().get(r[0], [])
    return []


def fetch_explored(name):
    """Which overlays a character has revealed, per zone (see fog.py), or None for
    an unknown character. Zones are keyed by area ID, as in /api/areas."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, exploredZones FROM characters.characters WHERE name = %s LIMIT 1",
                    (name,))
        row = cur.fetchone()
    if not row:
        return None
    t = tables()
    bits = fog.explored_bits(row[1])
    by_wma = overlays()
    zones, seen = {}, set()
    for r in t._wm:  # noqa: SLF001
        if not r[3] or r[2] in seen:
            continue
        seen.add(r[2])
        ids = fog.revealed(by_wma.get(r[0], []), t.area_bits, bits)
        if ids:
            zones[str(r[2])] = ids
    return {"name": row[0], "explored_bits": len(bits), "zones": zones}


def fog_image(area_id, explored):
    """PNG bytes for /maps/<area_id>.png?explored=<overlay ids>, or None.

    Only this zone's own overlays count, so the query can't name arbitrary files.
    """
    try:
        ids = {int(v) for v in explored.split(",") if v}
    except ValueError:
        return None
    if len(ids) > MAX_FOG_OVERLAYS:
        return None
    return fog.compose(MAPS_DIR, area_id, zone_overlays(area_id), ids)


def fetch_areas(map_id=None):
    t = tables()
    rows = []
    seen = set()
    for r in t._wm:  # noqa: SLF001 - internal read, kept local to this module
        if r[3] == 0:
            continue
        if map_id is not None and r[1] != map_id:
            continue
        area_id = r[2]
        if not area_id or area_id in seen:      # area 0 rows are the continents
            continue
        seen.add(area_id)
        rect = t.rects.get(area_id)
        if not rect:
            continue
        xmin, xmax, ymin, ymax = rect
        img = f"{area_id}.png"
        rows.append({
            "area_id": area_id, "name": t.zone_name(area_id), "map": r[1],
            "map_name": t.map_name(r[1]),
            "xmin": round(xmin, 1), "xmax": round(xmax, 1),
            "ymin": round(ymin, 1), "ymax": round(ymax, 1),
            "image": img, "has_image": os.path.exists(os.path.join(MAPS_DIR, img)),
            # Per-overlay art is extracted, so the page can draw this zone with a
            # character's fog of war (/maps/<area_id>.png?explored=...).
            "fog": fog.has_art(MAPS_DIR, area_id, overlays().get(r[0], [])),
            "calibration": calibrations.get(str(area_id), {"dx": 0, "dy": 0}),
            # Explored-area rects in image pixels (the 1024x768 canvas), not world
            # coordinates, so they don't go through the marker transform.
            "subzones": subzones(overlays().get(r[0], []), t.area_names),
        })
    rows.sort(key=lambda a: a["name"])
    return rows


def fetch_continents():
    """The four continent maps as areas the page can show like a zone (UM-78).

    `zones` are the zones drawn on the continent, each with `box`: its WorldMapArea rect
    in the continent's map-frame pixels [x, y, w, h], or None when the rect does not
    fit the continent (see transform.zone_box). Clicking a box opens the zone.
    """
    t = tables()
    out = []
    for map_id in t.continent_rects:
        zones = []
        for area_id in t.rects:
            if not area_id or t.zone_continent(area_id) != map_id:
                continue
            box = t.zone_box(area_id)
            zones.append({
                "area_id": area_id, "name": t.zone_name(area_id),
                "box": box and [round(box[0] * MAP_FRAME_W, 1), round(box[1] * MAP_FRAME_H, 1),
                                round((box[2] - box[0]) * MAP_FRAME_W, 1),
                                round((box[3] - box[1]) * MAP_FRAME_H, 1)],
            })
        zones.sort(key=lambda z: z["name"])
        img = f"continent_{map_id}.png"
        out.append({
            "area_id": f"c{map_id}", "continent_view": True, "map": map_id,
            "name": t.map_display_names.get(map_id) or t.map_name(map_id),
            "map_name": t.map_name(map_id),
            "image": img, "has_image": os.path.exists(os.path.join(MAPS_DIR, img)),
            "fog": False, "calibration": {"dx": 0, "dy": 0}, "subzones": [],
            "zones": zones,
        })
    out.sort(key=lambda c: c["name"])
    return out


def zones_in_use():
    """Zones that currently have online players — the useful default filter."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT zone, map FROM characters.characters WHERE online = 1")
        return [{"zone": z, "map": m} for z, m in cur.fetchall()]


def best_effort(cur, sql, args, label):
    """Run an optional character-detail query without failing the whole response."""
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("character %s query failed: %s", label, e)
        return []


def fetch_character(name):
    """Fetch one character and optional detail tables using one DB connection."""
    character_sql = """
        SELECT guid, name, level, race, class, gender, zone, map,
               position_x, position_y, position_z, orientation, money,
               totaltime, logout_time, online,
               health, power1, power2, power3, power4, power5, power6, power7
        FROM characters.characters
        WHERE name = %s
        LIMIT 1
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(character_sql, (name,))
        row = cur.fetchone()
        if not row:
            return None

        (guid, char_name, level, race, cls, gender, zone, cmap, x, y, z, orient,
         money, totaltime, logout_time, online, health) = row[:17]
        powers = row[17:]

        # `bag` is 0 for the character's own slots, otherwise the item_instance guid
        # of the container holding the item — `item_guid` lets callers resolve it.
        inventory = best_effort(cur, """
            SELECT ci.bag, ci.slot, ci.item, ii.itemEntry,
                   COALESCE(it.name, CONCAT('Item ', ii.itemEntry)), ii.count,
                   it.displayid, it.Quality, ii.flags, ii.durability, """
            + ", ".join(f"it.`{c}`" for c in item_tooltip.COLUMNS) + """,
                   ii.randomPropertyId, ii.enchantments
            FROM characters.character_inventory ci
            JOIN characters.item_instance ii ON ci.item = ii.guid
            LEFT JOIN world.item_template it ON ii.itemEntry = it.entry
            WHERE ci.guid = %s
            ORDER BY ci.bag, ci.slot
        """, (guid,), "inventory")
        set_context = item_set_context(cur, inventory)
        talents = best_effort(cur, """
            SELECT spell, talentGroup
            FROM characters.character_talent
            WHERE guid = %s
            ORDER BY talentGroup, spell
        """, (guid,), "talents")
        reputation = best_effort(cur, """
            SELECT faction, standing, flags
            FROM characters.character_reputation
            WHERE guid = %s
            ORDER BY faction
        """, (guid,), "reputation")
        # Max health/power only exist when the worldserver persists them
        # (PlayerSave.Stats.MinLevel > 0; Player::_SaveStats). No row -> None.
        stats = best_effort(cur, """
            SELECT maxhealth, maxpower1, maxpower2, maxpower3, maxpower4,
                   maxpower5, maxpower6, maxpower7
            FROM characters.character_stats
            WHERE guid = %s
        """, (guid,), "stats")
        achievements = best_effort(cur, """
            SELECT achievement, date
            FROM characters.character_achievement
            WHERE guid = %s
            ORDER BY date DESC, achievement
        """, (guid,), "achievements")

    t = tables()
    n = names()
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
            for spell, spec in talents
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
            for achievement, date in achievements
        ],
    }


def item_set_context(cur, rows):
    """(entries the character has equipped, {item set piece entry: name}) for the item
    set block of the tooltips. Equipped = bag 0, slots 0..18 (Player.h EQUIPMENT_SLOT_*);
    piece names come from world.item_template (ItemSet.dbc only has the entries)."""
    equipped = {r[3] for r in rows if r[0] == 0 and 0 <= r[1] < 19}
    col = 10 + item_tooltip.COLUMNS.index("itemset")
    entries = item_tooltip.set_piece_entries({r[col] for r in rows if r[col]}, names())
    if not entries:
        return equipped, {}
    found = best_effort(
        cur, "SELECT entry, name FROM world.item_template WHERE entry IN (%s)"
        % ", ".join(["%s"] * len(entries)), entries, "item set pieces")
    return equipped, dict(found)


def inventory_item(row, n=None, equipped=(), set_names=None):
    """One inventory row: (bag, slot, item guid, entry, name, count, displayid, Quality,
    item_instance.flags, item_instance.durability, *item_tooltip.COLUMNS,
    item_instance.randomPropertyId, item_instance.enchantments). `n` (GameNames) adds
    the DBC-backed tooltip lines; `equipped`/`set_names` feed the item set block."""
    (bag, slot, item_guid, item_entry, item_name, count, display_id, quality,
     inst_flags, durability) = row[:10]
    n_cols = len(item_tooltip.COLUMNS)
    template = dict(zip(item_tooltip.COLUMNS, row[10:10 + n_cols]))
    random_property, enchantments = (tuple(row[10 + n_cols:]) + (0, ""))[:2]
    return {
        "bag": bag, "slot": slot, "item_guid": item_guid, "item_entry": item_entry,
        "item_name": item_name, "count": count,
        # Quality is item_template.Quality (0 poor .. 7 heirloom), None if unknown.
        "quality": quality, "icon": icon_url(display_id),
        # A bag's size (item_template.ContainerSlots), for drawing its grid; 0 otherwise.
        "container_slots": template.get("ContainerSlots") or 0,
        "tooltip": item_tooltip.tooltip(
            item_name, template, count, inst_flags, durability, names=n,
            random_property=random_property or 0, enchantments=enchantments or "",
            equipped=equipped, item_names=set_names),
    }


def _talent_names(n, spell):
    t = n.talent(spell) or {}
    return {"name": t.get("name"), "tree": t.get("tree"), "tree_order": t.get("tree_order"),
            "rank": t.get("rank")}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "wowmap/1.0"

    def log_message(self, fmt, *args):  # quieter access log
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code, body, ctype="application/json", cache=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path == "/healthz":
                return self._send(200, {"ok": True})

            if path == "/api/players":
                return self._send(200, {
                    "server_time": int(time.time()),
                    "players": fetch_players(),
                })

            if path.startswith("/api/character/") and path.endswith("/activity"):
                name = unquote(path[len("/api/character/"):-len("/activity")])
                if activity is None:
                    return self._send(503, {"error": "activity feed disabled"})
                limit = qs.get("limit", ["50"])[0]
                limit = int(limit) if limit.isdigit() else 50
                return self._send(200, activity.feed(name, limit), cache="no-store")

            if path.startswith("/api/character/") and path.endswith("/explored"):
                name = unquote(path[len("/api/character/"):-len("/explored")])
                explored = fetch_explored(name) if name else None
                if explored is None:
                    return self._send(404, {"error": "character not found"})
                return self._send(200, explored, cache="no-store")

            if path.startswith("/api/character/"):
                name = unquote(path[len("/api/character/"):])
                if not name:
                    return self._send(404, {"error": "character not found"})
                character = fetch_character(name)
                if character is None:
                    return self._send(404, {"error": "character not found"})
                return self._send(200, character)

            if path == "/api/agents":
                return self._send(200, {"agents": sorted(n for n, _ in AGENT_APIS.values())})

            if path.startswith("/api/agent/"):
                name, _, view = unquote(path[len("/api/agent/"):]).partition("/")
                n = qs.get("n", [None])[0]
                status, body = fetch_agent_view(name, view, int(n) if n and n.isdigit() else None)
                return self._send(status, body, cache="no-store")

            if path == "/api/areas":
                mid = qs.get("map", [None])[0]
                return self._send(200, {
                    "areas": fetch_areas(int(mid) if mid else None),
                    "continents": fetch_continents(),
                    "in_use": zones_in_use(),
                })

            if path == "/api/summary":
                players = fetch_players()
                idle = [p for p in players if p["in_world"]]
                return self._send(200, {
                    "online": len(players),
                    "in_world": len(idle),
                    "in_instance": len(players) - len(idle),
                    "zones": sorted({p["zone_name"] for p in players if p["in_world"]}),
                })

            if path.startswith("/maps/"):
                fn = os.path.basename(path[len("/maps/"):])
                if "explored" in qs and fn.endswith(".png") and fn[:-4].isdigit():
                    # The same zone art, with only the listed overlays revealed.
                    body = fog_image(int(fn[:-4]), qs["explored"][0])
                    if body is None:
                        return self._send(404, {"error": "not found"})
                    return self._send(200, body, "image/png", cache="max-age=86400")
                fp = os.path.join(MAPS_DIR, fn)
                if os.path.isfile(fp) and fn.endswith(".png"):
                    with open(fp, "rb") as f:
                        return self._send(200, f.read(), "image/png", cache="max-age=86400")
                return self._send(404, {"error": "not found"})

            if path.startswith("/static/"):
                fn = path[len("/static/"):]
                if fn in STATIC_FILES:
                    with open(os.path.join(STATIC_DIR, fn), "rb") as f:
                        return self._send(200, f.read(), STATIC_FILES[fn],
                                          cache="public, max-age=86400")
                return self._send(404, {"error": "not found"})

            if path.startswith("/icons/"):
                fn = path[len("/icons/"):]
                fp = os.path.join(ICONS_DIR, fn)
                if ICON_FILE_RE.fullmatch(fn) and os.path.isfile(fp):
                    with open(fp, "rb") as f:
                        # Icons never change for a given client build.
                        return self._send(200, f.read(), "image/png",
                                          cache="public, max-age=2592000, immutable")
                return self._send(404, {"error": "not found"})

            if path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")

            return self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            log.exception("request failed")
            return self._send(500, {"error": str(e)})

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path != "/api/calibrate":
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_CALIBRATION_PAYLOAD_BYTES:
                return self._send(413, {"error": "payload too large"})
            payload = json.loads(self.rfile.read(length))
            area_id = int(payload["area_id"])
            dx, dy = float(payload["dx"]), float(payload["dy"])
            if area_id <= 0 or not math.isfinite(dx) or not math.isfinite(dy):
                raise ValueError("area_id must be positive and offsets must be finite")
            value = save_calibration(area_id, dx, dy)
            log.info("saved calibration for area %d: dx=%s dy=%s", area_id, value["dx"], value["dy"])
            return self._send(200, {"area_id": area_id, **value})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._send(400, {"error": str(exc)})
        except OSError as exc:
            log.exception("could not persist calibration")
            return self._send(500, {"error": str(exc)})


# ---------------------------------------------------------------- page: chat panel
# Chat comes from tools/chat-feed's SSE stream on a different port; wowmap only
# renders it (see CHAT_FEED_URL / CHAT_JS below). Sender names and message text are
# untrusted and always rendered with textContent, never innerHTML.
CHAT_CSS = r"""
  .side-tabs { display:flex; gap:6px; padding:0 16px 10px; }
  .side-tabs button { flex:1; background:#141824; color:var(--dim); border:1px solid var(--line);
                      border-radius:7px; padding:6px 4px; font:inherit; cursor:pointer; }
  .side-tabs button.on { background:#2b3550; border-color:#46557a; color:var(--fg); }
  .search-wrap { padding:0 16px 10px; }
  .search-wrap input { width:100%; background:#141824; color:var(--fg); border:1px solid var(--line);
                       border-radius:7px; padding:6px 9px; font:inherit; }
  .chat { flex:1; overflow:auto; padding:0 12px 12px; display:flex; flex-direction:column; gap:4px; }
  .chat-msg { font-size:12.5px; line-height:1.35; overflow-wrap:anywhere; }
  .chat-msg .sender { cursor:pointer; font-weight:600; }
  .chat-msg .sender:hover { text-decoration:underline; }
  .chat-msg .kind-say .sender { color:#e6e9f0; }
  .chat-msg .kind-yell .sender { color:#ff6b6b; }
  .chat-msg .kind-channel .sender { color:#5fd0d8; }
  .chat-msg .kind-guild .sender, .chat-msg .kind-party .sender,
  .chat-msg .kind-raid .sender, .chat-msg .kind-officer .sender,
  .chat-msg .kind-battleground .sender { color:#7ddf8a; }
  .chat-msg .kind-whisper .sender { color:#d68cf5; }
  .chat-msg .chan { color:var(--dim); font-size:11px; }
  .chat-status { padding:6px 16px; color:var(--dim); font-size:11px; }
  #resize-handle { position:absolute; top:0; right:-3px; width:6px; height:100%; cursor:col-resize; z-index:5; }
  aside { position:relative; }
  body.aside-collapsed aside { display:none; }
"""

CHAT_HTML = r"""
<div class="chat" id="chat" hidden></div>
<div class="chat-status" id="chat-status" hidden></div>
"""

CHAT_JS = r"""
<script>
const Chat = (() => {
  const KIND_LABEL = {say: 'says', yell: 'yells', channel: 'channel', guild: 'guild',
    party: 'party', raid: 'raid', officer: 'officer', battleground: 'battleground',
    whisper: 'whisper', unknown: '?'};
  const list = document.getElementById('chat');
  const status = document.getElementById('chat-status');
  let source = null, autoscroll = true, backoffMs = 2000;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function append(ev) {
    const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 24;
    const row = el('div', 'chat-msg kind-' + (ev.kind || 'unknown'));
    if (ev.sender) {
      const sender = el('span', 'sender', ev.sender);
      sender.onclick = () => window.selectCharacter && window.selectCharacter(ev.sender);
      row.append(sender, ' ');
    }
    if (ev.channel) row.append(el('span', 'chan', `[${ev.channel}] `));
    row.append(document.createTextNode(ev.text || ''));
    list.appendChild(row);
    while (list.children.length > 300) list.removeChild(list.firstChild);
    if (autoscroll || atBottom) list.scrollTop = list.scrollHeight;
  }

  function feedUrl() {
    if (window.CHAT_FEED_URL) return window.CHAT_FEED_URL.replace(/\/$/, '') + '/api/chat/stream';
    return `${location.protocol}//${location.hostname}:9500/api/chat/stream`;
  }

  function connect() {
    if (source) return;
    status.hidden = false;
    status.textContent = 'connecting to chat…';
    try {
      source = new EventSource(feedUrl());
    } catch (e) {
      status.textContent = 'chat unavailable: ' + e;
      return;
    }
    source.addEventListener('chat', (e) => {
      try { append(JSON.parse(e.data)); } catch (err) { /* malformed event, skip */ }
    });
    source.onopen = () => { status.hidden = true; backoffMs = 2000; };
    source.onerror = () => {
      status.hidden = false;
      status.textContent = 'chat disconnected, reconnecting…';
    };
  }

  function disconnect() {
    if (source) { source.close(); source = null; }
  }

  list.addEventListener('scroll', () => {
    autoscroll = list.scrollTop + list.clientHeight >= list.scrollHeight - 24;
  });

  return {connect, disconnect, shown: () => !list.hidden};
})();
</script>
"""

# ---------------------------------------------------------------- page: inspect drawer
# The page is one embedded document; each panel keeps its CSS/HTML/JS in its own
# constants and is spliced into PAGE at a named marker, so panels stay independent.
INSPECT_CSS = r"""
  .drawer { position:fixed; top:0; right:0; z-index:20; width:380px; max-width:100%; height:100vh;
            background:var(--panel); border-left:1px solid var(--line); display:flex;
            flex-direction:column; box-shadow:-8px 0 24px rgba(0,0,0,.35);
            transform:translateX(100%); visibility:hidden;
            transition:transform .18s ease, visibility 0s linear .18s; }
  .drawer.open { transform:none; visibility:visible; transition:transform .18s ease; }
  /* Make room for the drawer instead of painting over the toolbar and the map. */
  body.inspect-open main { margin-right:380px; }
  @media (max-width: 600px) {
    .drawer { width:100%; border-left:0; }
    body.inspect-open main { margin-right:0; }
  }
  .drawer-head { display:flex; gap:10px; align-items:flex-start; padding:14px 14px 12px 16px;
                 border-bottom:1px solid var(--line); }
  .drawer-head .dh-main { flex:1; min-width:0; }
  .drawer-head h2 { margin:0; font-size:17px; display:flex; align-items:center; gap:8px; }
  .drawer-head h2 .nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .drawer-head .line { color:var(--dim); font-size:12px; margin-top:2px; }
  .drawer-head .badge { font-size:11px; font-weight:normal; border:1px solid var(--line);
                        border-radius:999px; padding:0 7px; color:var(--dim); }
  .drawer-head .badge.on { color:#7ddf8a; border-color:#2f6b3a; }
  .drawer-body { flex:1; overflow:auto; padding:6px 16px 16px; }
  .drawer-foot { padding:6px 16px; border-top:1px solid var(--line); color:var(--dim); font-size:11px; }
  .drawer h3 { font-size:12px; text-transform:uppercase; letter-spacing:.6px; color:var(--dim);
               margin:14px 0 6px; }
  .drawer h4 { font-size:12px; margin:10px 0 4px; color:var(--fg); font-weight:600; }
  .drawer .kv, .drawer .item { display:flex; gap:10px; padding:3px 0; border-bottom:1px solid #20263a; }
  .drawer .kv .k, .drawer .item .k { color:var(--dim); flex:0 0 118px; }
  .drawer .kv .v, .drawer .item .v { flex:1; min-width:0; overflow-wrap:anywhere; }
  .drawer .item .n { color:var(--dim); }
  .drawer .item { align-items:center; }
  /* Item icon: 36 px, border in the item's quality colour (set inline from Q_COLORS). */
  .drawer .ico { position:relative; flex:0 0 36px; width:36px; height:36px; box-sizing:border-box;
                 border:1px solid #555; border-radius:4px; overflow:hidden; background:#0b0e16; }
  .drawer .ico img { display:block; width:100%; height:100%; }
  .drawer .ico.ph::before { content:'?'; display:grid; place-items:center; height:100%;
                            color:var(--dim); font-weight:600; }
  .drawer .ico .cnt { position:absolute; right:2px; bottom:0; font-size:11px; font-weight:600;
                      color:#fff; text-shadow:0 0 2px #000, 0 0 2px #000; }
  .drawer .none { color:var(--dim); font-size:12px; padding:3px 0; }
  .drawer .meter-bar { position:relative; height:16px; background:#20263a; border-radius:3px; overflow:hidden; }
  .drawer .meter-bar .fill { position:absolute; inset:0 auto 0 0; }
  .drawer .meter-bar .txt { position:relative; display:block; text-align:center; font-size:11px;
                      line-height:16px; text-shadow:0 0 2px #000, 0 0 2px #000; }
  .drawer details { margin-top:10px; border:1px solid var(--line); border-radius:7px; padding:0 10px; }
  .drawer details[open] { padding-bottom:8px; }
  /* UM-80: paper doll, bag bar and bag grids (38 px squares, game layout). */
  .drawer .ico.empty { display:flex; align-items:center; justify-content:center;
                       border-color:#2a3047; background:#0d111b; }
  .drawer .ico .lbl { font-size:7.5px; line-height:1.1; text-align:center; color:#5b647e; }
  .drawer .ico[tabindex] { cursor:default; }
  .drawer .ico[tabindex]:focus-visible { outline:2px solid #ffd100; outline-offset:1px; }
  .drawer .doll { display:grid; grid-template-columns:36px 1fr 36px; gap:4px 8px; }
  .drawer .doll .col { display:flex; flex-direction:column; gap:4px; }
  .drawer .doll .mid { display:flex; flex-direction:column; justify-content:center; align-items:center;
                       gap:2px; border:1px solid #20263a; border-radius:6px; font-size:12px;
                       color:var(--dim); text-align:center; min-width:0;
                       background:radial-gradient(ellipse at center, #1a2033 0%, #0d111b 75%); }
  .drawer .doll .mid .nm { font-size:14px; font-weight:600; overflow-wrap:anywhere; }
  .drawer .doll .bottom { grid-column:1 / -1; display:flex; justify-content:center; gap:4px; }
  .drawer .bag-bar { display:flex; flex-wrap:wrap; gap:4px; margin-bottom:8px; }
  .drawer .bags { display:flex; flex-wrap:wrap; gap:8px; align-items:flex-start; }
  .drawer .bag { border:1px solid #2a3047; border-radius:6px; padding:5px 6px 6px; background:#10141f;
                 max-width:100%; }
  .drawer .bag-title { font-size:11px; margin-bottom:4px; overflow:hidden; text-overflow:ellipsis;
                       white-space:nowrap; max-width:166px; }
  .drawer .bag-grid { display:grid; gap:2px; max-width:100%; }
  .drawer .bag-grid .ico { width:36px; height:36px; }
  .item-tip { position:fixed; z-index:50; max-width:min(320px, calc(100vw - 12px)); pointer-events:none;
              background:rgba(9,12,30,.95); border:1px solid #8a8fa8; border-radius:5px;
              padding:6px 9px; font-size:12.5px; line-height:1.35;
              box-shadow:0 4px 14px rgba(0,0,0,.6); }
  .item-tip .tl { display:flex; gap:18px; justify-content:space-between; }
  .item-tip .tl:first-child { font-size:14px; }
  .item-tip .r { white-space:nowrap; }
  .item-tip .coin { display:inline-block; width:9px; height:9px; border-radius:50%; margin-left:2px;
                    vertical-align:-1px; }
  .item-tip .coin.g { background:#e8c447; }
  .item-tip .coin.s { background:#c7c7cf; }
  .item-tip .coin.c { background:#c06a35; }
  .drawer summary { cursor:pointer; padding:7px 0; color:var(--dim); font-size:12px;
                    text-transform:uppercase; letter-spacing:.6px; }
  /* Reputation window: nested headers, one bar per faction (name | rank bar | numbers). */
  .drawer details.rep-group { margin:0; border:0; border-radius:0; padding:0 0 0 12px; }
  .drawer details.rep-group[open] { padding-bottom:0; }
  .drawer details.rep-group > summary { display:flex; align-items:center; gap:4px; margin-left:-12px;
                                        padding:3px 0; list-style:none; text-transform:none;
                                        letter-spacing:0; font-size:13px; color:var(--fg); }
  .drawer details.rep-group > summary::-webkit-details-marker { display:none; }
  .drawer details.rep-group > summary::before { content:'\25B8'; flex:0 0 8px; color:var(--dim); }
  .drawer details.rep-group[open] > summary::before { content:'\25BE'; }
  .drawer .rep-head { font-weight:600; }
  .drawer .rep { display:flex; gap:6px; align-items:center; padding:2px 0; font-size:13px; }
  .drawer summary > .rep { flex:1; min-width:0; padding:0; }
  .drawer .rep-name { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .drawer .rep-bar { position:relative; flex:0 0 72px; height:14px; background:#20263a;
                     border-radius:3px; overflow:hidden; }
  .drawer .rep-bar.war { outline:1px solid #e0402f; }
  .drawer .rep-bar .fill { position:absolute; inset:0 auto 0 0; }
  .drawer .rep-bar .txt { position:relative; display:block; text-align:center; font-size:11px;
                          line-height:14px; color:#fff; text-shadow:0 0 2px #000, 0 0 2px #000; }
  .drawer .rep-num { flex:0 0 72px; text-align:right; color:var(--dim); font-size:12px;
                     font-variant-numeric:tabular-nums; }
"""

INSPECT_HTML = r"""
<section class="drawer" id="inspect" aria-hidden="true" aria-label="Inspect character">
  <div class="drawer-head">
    <div class="dh-main" id="inspect-head"></div>
    <button id="inspect-close" title="Close (Esc)" aria-label="Close">✕</button>
  </div>
  <div class="drawer-body" id="inspect-body"></div>
  <div class="drawer-foot" id="inspect-status"></div>
</section>
"""

# Everything from the API is rendered with textContent (never innerHTML): character
# and item names come straight from the database.
INSPECT_JS = r"""
<script>
// Position text shared by the inspect drawer, the player list and marker tooltips.
function placeText(p) {
  return [p.continent_name, p.zone_name, p.subzone_name].filter(Boolean).join(' › ');
}
function mapCoordsText(p) {
  return p.map_coords ? `${p.map_coords.x.toFixed(1)}, ${p.map_coords.y.toFixed(1)}` : '';
}
function worldText(x, y, z) {
  return `${x.toFixed(1)}, ${y.toFixed(1)}, ${z.toFixed(1)}`;
}

// In-game style item tooltip (UM-80). The API sends ready-made lines
// (item_tooltip.py); this only draws them, with textContent, and keeps the box on screen.
const ItemTip = (() => {
  const TIP_COLORS = {white: '#ffffff', green: '#1eff00', yellow: '#ffd100', gray: '#9d9d9d',
    red: '#ff2020'};
  const QUALITY = ['#9d9d9d', '#ffffff', '#1eff00', '#0070dd', '#a335ee', '#ff8000', '#e6cc80', '#e6cc80'];
  const tip = document.createElement('div');
  tip.className = 'item-tip';
  tip.hidden = true;
  tip.setAttribute('role', 'tooltip');
  document.body.append(tip);
  let anchor = null;

  function span(cls, text) {
    const e = document.createElement('span');
    if (cls) e.className = cls;
    e.textContent = String(text);
    return e;
  }
  // Copper as the game's coins: only the non-zero denominations.
  function coins(copper) {
    const out = document.createElement('span');
    const parts = [[Math.floor(copper / 10000), 'g'], [Math.floor(copper / 100) % 100, 's'], [copper % 100, 'c']];
    for (const [n, c] of parts) if (n) out.append(span(null, n), span('coin ' + c, ''), ' ');
    return out;
  }
  function render(it) {
    const lines = it.tooltip && it.tooltip.length ? it.tooltip : [{left: it.item_name, color: 'quality'}];
    tip.replaceChildren();
    for (const l of lines) {
      const row = document.createElement('div');
      row.className = 'tl';
      row.style.color = l.color === 'quality' ? (QUALITY[it.quality] || '#fff') : (TIP_COLORS[l.color] || '#fff');
      const left = span('l', l.left);
      if (l.money !== undefined) left.append(' ', coins(Number(l.money) || 0));
      row.append(left);
      if (l.right) row.append(span('r', l.right));
      tip.append(row);
    }
  }
  // Beside the square (left first: the drawer sits on the right), else below or
  // above it; always clamped to the viewport so narrow screens keep it visible.
  function place() {
    const r = anchor.getBoundingClientRect();
    const w = tip.offsetWidth, h = tip.offsetHeight, vw = innerWidth, vh = innerHeight, m = 6;
    let x = r.left - w - m, y = r.top;
    if (x < m) x = r.right + m;
    if (x + w > vw - m) {
      x = Math.max(m, Math.min(r.left, vw - w - m));
      y = r.bottom + m;
      if (y + h > vh - m) y = r.top - h - m;
    }
    y = Math.max(m, Math.min(y, vh - h - m));
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }
  function show(box, it) {
    anchor = box;
    render(it);
    tip.hidden = false;
    place();
  }
  function hide() { tip.hidden = true; anchor = null; }
  function attach(box, it) {
    box.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse') show(box, it); });
    box.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse' && anchor === box) hide(); });
    box.addEventListener('focus', () => show(box, it));
    box.addEventListener('blur', () => { if (anchor === box) hide(); });
    // Touch: a tap shows it; tapping anywhere else (or scrolling) hides it.
    box.addEventListener('click', () => show(box, it));
  }
  document.addEventListener('pointerdown', (e) => {
    if (anchor && !anchor.contains(e.target)) hide();
  });
  addEventListener('scroll', () => { if (anchor) hide(); }, true);
  addEventListener('resize', () => { if (anchor) hide(); });
  return {attach, hide};
})();

const Inspect = (() => {
  const EQUIP_SLOTS = ['Head', 'Neck', 'Shoulder', 'Shirt', 'Chest', 'Waist', 'Legs',
    'Feet', 'Wrist', 'Hands', 'Finger 1', 'Finger 2', 'Trinket 1', 'Trinket 2', 'Back',
    'Main Hand', 'Off Hand', 'Ranged', 'Tabard'];
  const POWER_LABELS = {mana: 'Mana', rage: 'Rage', focus: 'Focus', energy: 'Energy',
    happiness: 'Happiness', rune: 'Runes', runic_power: 'Runic Power'};
  // The server keeps rage and runic power in tenths (1000 is shown as 100 in game).
  const POWER_SCALE = {rage: 10, runic_power: 10};
  // Bar colours roughly follow the default unit frames.
  const BAR_COLORS = {health: '#1f9e3a', mana: '#2f5fd8', rage: '#c42f2f', focus: '#d98a3a',
    energy: '#d6c22e', happiness: '#2fa88a', rune: '#7f7f7f', runic_power: '#1fa6c4'};
  const CLASS_POWERS = {1: ['rage'], 2: ['mana'], 3: ['mana'], 4: ['energy'], 5: ['mana'],
    6: ['runic_power'], 7: ['mana'], 8: ['mana'], 9: ['mana'], 11: ['mana', 'rage', 'energy']};
  const nf = new Intl.NumberFormat('en-US');
  const drawer = document.getElementById('inspect');
  const head = document.getElementById('inspect-head');
  const body = document.getElementById('inspect-body');
  const status = document.getElementById('inspect-status');
  const openSections = new Set();
  let name = null, lastJson = null, seq = 0;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function kv(label, value, cls = 'kv') {
    const r = el('div', cls);
    r.append(el('span', 'k', label), el('span', 'v', value));
    return r;
  }
  // A bar when the max is known (character_stats row), otherwise the plain number.
  function meter(label, cur, max, color) {
    const value = nf.format(cur);
    if (!(max > 0)) return kv(label, value);
    const b = el('div', 'meter-bar');
    const fill = el('span', 'fill');
    fill.style.width = `${Math.max(0, Math.min(100, cur / max * 100))}%`;
    fill.style.background = color;
    b.append(fill, el('span', 'txt', `${value} / ${nf.format(max)}`));
    const r = el('div', 'kv');
    r.append(el('span', 'k', label));
    const v = el('span', 'v');
    v.append(b);
    r.append(v);
    return r;
  }
  function money(copper) {
    const c = Number(copper) || 0;
    return `${nf.format(Math.floor(c / 10000))}g ${Math.floor(c / 100) % 100}s ${c % 100}c`;
  }
  function duration(sec) {
    const s = Number(sec) || 0;
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  }
  function when(ts) { return ts ? new Date(ts * 1000).toLocaleString('en-US') : 'never'; }

  // bag 0 = the character's own slots (TrinityCore Player.h EquipmentSlots, InventorySlots, …);
  // any other bag value is the item_instance guid of the container holding the item.
  function groupInventory(items) {
    const g = {equipped: [], backpack: [], bags: [], bank: [], bankBags: [], keyring: [],
               currency: [], other: []};
    const containers = new Map();
    for (const it of items) {
      if (it.bag !== 0) continue;
      const s = it.slot;
      if (s < 19) g.equipped.push(it);
      else if (s < 23 || (s >= 67 && s < 74)) {
        const bank = s >= 67;
        const c = {slot: s, bag: it, items: []};
        (bank ? g.bankBags : g.bags).push(c);
        containers.set(it.item_guid, c);
      }
      else if (s < 39) g.backpack.push(it);
      else if (s < 67) g.bank.push(it);
      else if (s >= 86 && s < 118) g.keyring.push(it);
      else if (s >= 118 && s < 150) g.currency.push(it);
      else g.other.push(it);
    }
    for (const it of items) {
      if (it.bag === 0) continue;
      const c = containers.get(it.bag);
      (c ? c.items : g.other).push(it);
    }
    return g;
  }

  // item_template.Quality 0..7: poor, common, uncommon, rare, epic, legendary, artifact, heirloom.
  const Q_COLORS = ['#9d9d9d', '#ffffff', '#1eff00', '#0070dd', '#a335ee', '#ff8000', '#e6cc80', '#e6cc80'];
  // `it.icon` is a same-origin /icons/<file>.png URL from the API, or null (no icon
  // extracted): then a '?' placeholder is drawn, as it is if the image fails to load.
  function itemIcon(it) {
    const q = Q_COLORS[it.quality];
    const box = el('span', 'ico');
    box.setAttribute('aria-label', it.count > 1 ? `${it.item_name} ×${it.count}` : it.item_name);
    box.tabIndex = 0;
    ItemTip.attach(box, it);
    if (q) box.style.borderColor = q;
    if (it.icon) {
      const img = el('img');
      img.alt = '';
      img.loading = 'lazy';
      img.addEventListener('error', () => { img.remove(); box.classList.add('ph'); });
      img.src = it.icon;
      box.append(img);
    } else box.classList.add('ph');
    if (it.count > 1) box.append(el('span', 'cnt', it.count));
    return box;
  }

  function itemRows(parent, items, label) {
    if (!items.length) { parent.append(el('div', 'none', 'empty')); return; }
    for (const it of items) {
      const r = el('div', 'item');
      r.append(el('span', 'k', label(it)), itemIcon(it));
      const v = el('span', 'v', it.item_name);
      if (Q_COLORS[it.quality]) v.style.color = Q_COLORS[it.quality];
      v.append(' ', el('span', 'n', '×' + it.count));
      r.append(v);
      parent.append(r);
    }
  }

  function collapsible(key, title) {
    const d = el('details');
    d.dataset.key = key;
    d.open = openSections.has(key);
    d.append(el('summary', null, title));
    d.addEventListener('toggle', () => {
      d.open ? openSections.add(key) : openSections.delete(key);
    });
    return d;
  }

  // ---- In-game style equipment and bags (UM-80) ----------------------------------
  // Paper doll: the character window's slot columns (EquipmentSlots ids, Player.h).
  const DOLL_LEFT = [0, 1, 2, 14, 4, 3, 18, 8];      // head neck shoulder back chest shirt tabard wrist
  const DOLL_RIGHT = [9, 5, 6, 7, 10, 11, 12, 13];   // hands waist legs feet finger×2 trinket×2
  const DOLL_BOTTOM = [15, 16, 17];                  // main hand, off hand, ranged
  // An empty slot: a dimmed square named after the slot, like the game's slot art.
  function emptySlot(label) {
    const e = el('span', 'ico empty');
    if (label) { e.title = label; e.append(el('span', 'lbl', label)); }
    return e;
  }
  function paperDoll(equipped, c) {
    const by = new Map(equipped.map((it) => [it.slot, it]));
    const sq = (s) => by.has(s) ? itemIcon(by.get(s)) : emptySlot(EQUIP_SLOTS[s]);
    const doll = el('div', 'doll');
    const left = el('div', 'col'), right = el('div', 'col'), bottom = el('div', 'bottom');
    left.append(...DOLL_LEFT.map(sq));
    right.append(...DOLL_RIGHT.map(sq));
    bottom.append(...DOLL_BOTTOM.map(sq));
    const mid = el('div', 'mid');
    mid.append(el('div', 'nm', c.name), el('div', null, `Level ${c.level} ${c.race_name}`),
               el('div', null, c.class_name));
    mid.firstChild.style.color = c.class_color;
    doll.append(left, mid, right, bottom);
    return doll;
  }
  // One bag window: `size` squares, `cols` wide. Like the game's container frames
  // the slots fill from the bottom right, so a partial row sits top right.
  function bagGrid(title, quality, size, items, first, cols) {
    const box = el('div', 'bag');
    const h = el('div', 'bag-title', title);
    if (Q_COLORS[quality]) h.style.color = Q_COLORS[quality];
    const grid = el('div', 'bag-grid');
    grid.style.gridTemplateColumns = `repeat(${cols}, 36px)`;
    const by = new Map(items.map((it) => [it.slot - first, it]));
    const n = Math.max(size, ...[...by.keys()].map((k) => k + 1), 0);
    for (let i = 0; i < (cols - n % cols) % cols; i++) grid.append(el('span', 'gap'));
    for (let i = 0; i < n; i++) grid.append(by.has(i) ? itemIcon(by.get(i)) : emptySlot());
    box.append(h, grid);
    return box;
  }
  // The bag bar (the four bag slots, InventorySlots 19-22, or bank bags 67-73)
  // followed by a grid per equipped bag.
  function bagWindows(parent, packTitle, pack, packSize, packFirst, packCols, bags, firstBagSlot, nBags) {
    const bySlot = new Map(bags.map((b) => [b.slot, b]));
    const bar = el('div', 'bag-bar');
    for (let s = firstBagSlot; s < firstBagSlot + nBags; s++) {
      bar.append(bySlot.has(s) ? itemIcon(bySlot.get(s).bag) : emptySlot('Bag'));
    }
    const wrap = el('div', 'bags');
    wrap.append(bagGrid(packTitle, 1, packSize, pack, packFirst, packCols));
    for (const b of [...bags].sort((x, y) => x.slot - y.slot)) {
      wrap.append(bagGrid(b.bag.item_name, b.bag.quality, b.bag.container_slots, b.items, 0, 4));
    }
    parent.append(bar, wrap);
  }

  function renderHead(c) {
    const h = el('h2');
    const dot = el('span', 'dot');
    dot.style.background = dot.style.color = c.class_color;
    h.append(dot, el('span', 'nm', c.name),
             el('span', 'badge' + (c.online ? ' on' : ''), c.online ? 'online' : 'offline'));
    head.replaceChildren(h,
      el('div', 'line', `Level ${c.level} · ${c.race_name} · ${c.class_name}`),
      el('div', 'line', placeText(c)));
  }

  // Names come from the client DBCs (tools/dbc/names.py); unknown ids fall back to the raw id.
  function talentSection(list) {
    const d = collapsible('talents', `Talents (${list.length})`);
    const groups = new Map();
    for (const t of list) {
      const key = `${t.spec}|${t.tree_order ?? 99}|${t.tree ?? ''}`;
      if (!groups.has(key)) groups.set(key, {spec: t.spec, order: t.tree_order ?? 99, tree: t.tree, items: []});
      groups.get(key).items.push(t);
    }
    const specs = new Set(list.map((t) => t.spec));
    const sorted = [...groups.values()].sort((a, b) => a.spec - b.spec || a.order - b.order);
    for (const g of sorted) {
      const tree = g.tree || 'Unknown tree';
      d.append(el('h4', null, specs.size > 1 ? `Spec ${g.spec + 1} · ${tree} (${g.items.length})` : `${tree} (${g.items.length})`));
      for (const t of g.items) d.append(kv(t.name || `spell ${t.spell}`, t.rank ? `rank ${t.rank}` : ''));
    }
    return d;
  }
  // The in-game reputation window (GameNames.reputation_panel): headers from the
  // Faction.dbc tree, only factions the character has discovered. Bar colours are
  // FACTION_BAR_COLORS from the 3.3.5 FrameXML ReputationFrame.lua, keyed by rank_id.
  const REP_COLORS = {1: '#cc4d38', 2: '#cc4d38', 3: '#bf4500', 4: '#e6b300',
    5: '#00991a', 6: '#00991a', 7: '#00991a', 8: '#00991a'};
  const closedReps = new Set();
  function repRow(name, r) {
    const row = el('div', 'rep');
    row.append(el('span', 'rep-name', name));
    const b = el('span', 'rep-bar' + (r.at_war ? ' war' : ''));
    const fill = el('span', 'fill');
    fill.style.width = `${Math.max(0, Math.min(100, r.bar_value / r.bar_max * 100))}%`;
    fill.style.background = REP_COLORS[r.rank_id] || '#888';
    b.append(fill, el('span', 'txt', r.rank));
    if (r.at_war) b.title = 'At war';
    row.append(b, el('span', 'rep-num', `${r.bar_value}/${r.bar_max}`));  // as in game: 562/3000
    return row;
  }
  function repNodes(parent, nodes) {
    for (const n of nodes) {
      if (!n.header) { parent.append(repRow(n.name, n.rep)); continue; }
      const key = String(n.faction ?? n.name);
      const d = el('details', 'rep-group');
      d.open = !closedReps.has(key);
      const s = el('summary');
      s.append(n.rep ? repRow(n.name, n.rep) : el('span', 'rep-head', n.name));
      d.append(s);
      d.addEventListener('toggle', () => { d.open ? closedReps.delete(key) : closedReps.add(key); });
      repNodes(d, n.children || []);
      parent.append(d);
    }
  }
  function countReps(nodes) {
    return nodes.reduce((k, n) => k + (n.rep ? 1 : 0) + countReps(n.children || []), 0);
  }
  function reputationSection(panel) {
    const d = collapsible('reputation', `Reputation (${countReps(panel)})`);
    if (!panel.length) d.append(el('div', 'none', 'No factions discovered'));
    repNodes(d, panel);
    return d;
  }
  function achievementSection(list) {
    const points = list.reduce((n, a) => n + (a.points || 0), 0);
    const d = collapsible('achievements', `Achievements (${list.length} · ${nf.format(points)} pts)`);
    for (const a of list) {
      const pts = a.points != null ? `${a.points} pts · ` : '';
      d.append(kv(a.name || `#${a.achievement}`, pts + when(a.date)));
    }
    return d;
  }

  function renderBody(c) {
    const f = document.createDocumentFragment();

    f.append(el('h3', null, 'Status'));
    f.append(meter('Health', c.health, c.max_health, BAR_COLORS.health));
    const power = c.power || {}, maxPower = c.max_power || {};
    for (const key of CLASS_POWERS[c.class] || Object.keys(POWER_LABELS)) {
      if (!(key in power)) continue;
      const scale = POWER_SCALE[key] || 1;
      f.append(meter(POWER_LABELS[key], Math.floor(power[key] / scale),
                     Math.floor((maxPower[key] || 0) / scale), BAR_COLORS[key]));
    }
    f.append(kv('Gold', money(c.money)));
    f.append(kv('Played time', duration(c.totaltime)));
    f.append(kv('Last logout', when(c.logout_time)));

    f.append(el('h3', null, 'Position'));
    f.append(kv('Location', placeText(c)));
    f.append(kv('Map coords', mapCoordsText(c) || '—'));
    f.append(kv('World X, Y, Z', worldText(c.position_x, c.position_y, c.position_z)));
    f.append(kv('Facing', `${c.orientation.toFixed(2)} rad`));
    if (window.ActivityFeed) f.append(window.ActivityFeed.section(c.name));

    const inv = groupInventory(c.inventory || []);
    f.append(el('h3', null, 'Equipped'));
    f.append(paperDoll(inv.equipped, c));
    f.append(el('h3', null, 'Bags'));
    // Backpack: InventoryPackSlots 23-38; bags in InventorySlots 19-22 (Player.h).
    bagWindows(f, 'Backpack', inv.backpack, 16, 23, 4, inv.bags, 19, 4);

    if (inv.bank.length || inv.bankBags.length) {
      const d = collapsible('bank', `Bank (${inv.bank.length + inv.bankBags.reduce((n, b) => n + b.items.length, 0)})`);
      // Bank: BankItemSlots 39-66 (28 squares, 7 wide in game); bank bags in 67-73.
      bagWindows(d, 'Bank', inv.bank, 28, 39, 7, inv.bankBags, 67, 7);
      f.append(d);
    }
    for (const [key, title, items] of [['keyring', 'Keyring', inv.keyring],
                                       ['currency', 'Currency', inv.currency],
                                       ['other', 'Other items', inv.other]]) {
      if (!items.length) continue;
      const d = collapsible(key, `${title} (${items.length})`);
      itemRows(d, items, (it) => `bag ${it.bag} / ${it.slot}`);
      f.append(d);
    }

    f.append(talentSection(c.talents || []), reputationSection(c.reputation_panel || []),
             achievementSection(c.achievements || []));

    const top = body.scrollTop;
    ItemTip.hide();
    body.replaceChildren(f);
    body.scrollTop = top;
  }

  function stamp() { status.textContent = 'updated ' + new Date().toLocaleTimeString(); }

  function markSelected() {
    for (const e of document.querySelectorAll('.pl')) e.classList.toggle('sel', e.dataset.name === name);
  }

  async function refresh() {
    if (!name) return;
    const mine = ++seq, who = name;
    try {
      const r = await fetch('/api/character/' + encodeURIComponent(who));
      const text = await r.text();
      if (mine !== seq || who !== name) return;
      if (!r.ok) {
        lastJson = null;
        head.replaceChildren(el('h2', null, who));
        body.replaceChildren(el('div', 'empty', r.status === 404 ? 'Character not found' : 'error ' + r.status));
        stamp();
        return;
      }
      if (text !== lastJson) {
        const c = JSON.parse(text);
        renderHead(c);
        renderBody(c);
        lastJson = text;
      }
      stamp();
    } catch (e) {
      if (mine === seq) status.textContent = 'error: ' + e;
    }
  }

  function open(who) {
    if (who !== name) {
      name = who;
      lastJson = null;
      head.replaceChildren(el('h2', null, who));
      body.replaceChildren(el('div', 'empty', 'loading…'));
      body.scrollTop = 0;
      status.textContent = '';
    }
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    // The page re-fits the map on resize; reuse that after the layout shifts.
    document.body.classList.add('inspect-open');
    dispatchEvent(new Event('resize'));
    markSelected();
    return refresh();
  }

  function close() {
    if (!name) return;
    name = null;
    lastJson = null;
    seq++;
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('inspect-open');
    dispatchEvent(new Event('resize'));
    markSelected();
  }

  document.getElementById('inspect-close').onclick = close;
  addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  // Click-away: anything outside the drawer, except the controls that open it
  // (list, markers) and the map toolbar, closes it. The map reports its own
  // clicks (the page calls Inspect.close()), because the DOM click that ends a
  // pan or a zoom-button press must not close the drawer.
  document.addEventListener('click', (e) => {
    if (name && !e.target.closest('#inspect, #list, #map, .marker, .bar')) close();
  });
  // Deep link: /#inspect=<name> opens the drawer, handy for offline characters.
  // A malformed hash must not take the rest of the page down with it.
  try {
    const m = location.hash.match(/^#inspect=(.+)$/);
    if (m) open(decodeURIComponent(m[1]));
  } catch (e) {
    console.warn('invalid inspect hash:', e);
  }

  return {open, close, refresh, current: () => name};
})();
</script>
"""

# ---------------------------------------------------------------- page: agent mind
# UM-50: agents that run an observability API (AGENT_API_URLS) get a distinct
# ring on the map and an "Agent mind" tab in the inspect drawer, polled through
# /api/agent/<name>/{brain,perception}. Self-contained: it only reads
# Inspect.current() and adds its own elements, so the inspect panel is untouched.
# Everything from the agent is rendered with textContent (chat and names are untrusted).
AGENT_CSS = r"""
  .marker.agent .ring { outline:2px dashed #5fd0d8; outline-offset:2px; }
  .pl.agent .nm::after { content:" · agent"; color:#5fd0d8; font-size:11px; }
  .mind-tabs { display:flex; gap:6px; padding:8px 16px 0; }
  .mind-tabs button { flex:1; background:#141824; color:var(--dim); border:1px solid var(--line);
                      border-radius:7px; padding:5px 4px; font:inherit; cursor:pointer; }
  .mind-tabs button.on { background:#2b3550; border-color:#46557a; color:var(--fg); }
  .mind .dec { padding:4px 0; border-bottom:1px solid #20263a; font-size:12.5px; overflow-wrap:anywhere; }
  .mind .dec .when { color:var(--dim); font-size:11px; }
  .mind .dec.fail .act { color:#ff8b8b; }
  .mind .dec .err { color:var(--dim); }
"""

AGENT_JS = r"""
<script>
const AgentMind = (() => {
  const drawer = document.getElementById('inspect');
  const charBody = document.getElementById('inspect-body');
  const status = document.getElementById('inspect-status');
  const agents = new Map();  // lowercased name -> name
  let tab = 'character', shownFor = null, seq = 0;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function kv(label, value) {
    const r = el('div', 'kv');
    r.append(el('span', 'k', label), el('span', 'v', value));
    return r;
  }
  const isAgent = (n) => !!n && agents.has(n.toLowerCase());

  const tabs = el('div', 'mind-tabs');
  const bChar = el('button', 'on', 'Character'), bMind = el('button', null, 'Agent mind');
  tabs.append(bChar, bMind);
  tabs.hidden = true;
  const pane = el('div', 'drawer-body mind');
  pane.hidden = true;
  drawer.insertBefore(tabs, charBody);
  drawer.insertBefore(pane, status);

  function setTab(t) {
    tab = t;
    bChar.classList.toggle('on', t === 'character');
    bMind.classList.toggle('on', t === 'mind');
    charBody.hidden = t === 'mind';
    pane.hidden = t !== 'mind';
    if (t === 'mind') refresh();
  }
  bChar.onclick = () => setTab('character');
  bMind.onclick = () => setTab('mind');

  function sync() {
    const n = Inspect.current();
    tabs.hidden = !isAgent(n);
    if (!isAgent(n) && tab === 'mind') setTab('character');
    if (n !== shownFor) {
      shownFor = n;
      seq++;
      pane.replaceChildren(el('div', 'none', 'loading…'));
      if (tab === 'mind' && isAgent(n)) refresh();
    }
  }

  function args(a) {
    const s = JSON.stringify(a || {});
    return s === '{}' ? '' : s.length > 120 ? s.slice(0, 117) + '...' : s;
  }
  function unitLine(u) {
    const bits = [u.name || `${u.type || 'object'} ${u.entry ?? ''}`];
    if (u.level != null) bits.push(`L${u.level}`);
    if (u.health_pct != null) bits.push(`${Math.round(u.health_pct * 100)}% hp`);
    if (u.in_combat) bits.push('in combat');
    return bits.join(' · ');
  }

  function render(brain, perc) {
    const f = document.createDocumentFragment();
    f.append(el('h3', null, 'Brain'));
    f.append(kv('Status', brain.connected ? 'in game' : 'not connected'));
    f.append(kv('Goal', brain.goal || '(none)'));
    if (brain.brain) f.append(kv('Brain', brain.brain));
    f.append(kv('Model', brain.model || '(none)'));
    f.append(kv('Cycle', brain.cycle ?? '-'));
    const t = brain.tokens || {};
    f.append(kv('Tokens', `${t.prompt_total || 0} in · ${t.completion_total || 0} out · ${t.cycles || 0} cycles`));
    for (const [k, v] of Object.entries(brain.reflexes || {})) f.append(kv(`Reflex: ${k}`, JSON.stringify(v)));

    f.append(el('h3', null, 'Last decisions'));
    const decs = (brain.decisions || []).slice().reverse();
    if (!decs.length) f.append(el('div', 'none', 'no decisions yet'));
    for (const d of decs) {
      const ok = d.result && d.result.ok;
      const r = el('div', 'dec' + (ok ? '' : ' fail'));
      const tc = d.tool_call || {};
      // UM-101: which brain picked it, Jev's confidence, and a fallback marker.
      const who = d.brain ? ` · ${d.brain}${d.confidence != null ? ' ' + Math.round(d.confidence * 100) + '%' : ''}${d.fallback && d.brain === 'llm' ? ' (jev fallback)' : ''}` : '';
      r.append(el('div', 'when', `#${d.cycle} · ${d.ts ? new Date(d.ts * 1000).toLocaleTimeString() : ''}${who}`),
               el('div', 'act', `${tc.name || '(no action)'} ${args(tc.args)}`));
      if (!ok && d.result && d.result.error) r.append(el('div', 'err', d.result.error));
      f.append(r);
    }

    f.append(el('h3', null, 'Nearby'));
    if (perc && perc.connected !== false) {
      const p = perc.position;
      if (p) f.append(kv('Position', `map ${p.map} · ${p.x.toFixed(1)}, ${p.y.toFixed(1)}, ${p.z.toFixed(1)}`));
      for (const [label, key] of [['Units', 'nearby_units'], ['Players', 'nearby_players'], ['Objects', 'nearby_objects']]) {
        const list = (perc[key] || []).slice(0, 8);
        f.append(el('h4', null, `${label} (${(perc[key] || []).length})`));
        if (!list.length) f.append(el('div', 'none', 'none'));
        for (const u of list) f.append(kv(`${u.distance} yd`, unitLine(u)));
      }
      for (const key of ['window', 'trade', 'pending_invite']) {
        if (perc[key]) f.append(kv(key.replace('_', ' '), JSON.stringify(perc[key]).slice(0, 200)));
      }
    } else {
      f.append(el('div', 'none', 'no perception'));
    }
    const top = pane.scrollTop;
    pane.replaceChildren(f);
    pane.scrollTop = top;
  }

  async function getJson(who, view) {
    const r = await fetch(`/api/agent/${encodeURIComponent(who)}/${view}` + (view === 'brain' ? '?n=5' : ''));
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || 'error ' + r.status);
    return j;
  }

  async function refresh() {
    const who = Inspect.current();
    if (!isAgent(who) || tab !== 'mind') return;
    const mine = ++seq;
    try {
      const [brain, perc] = await Promise.all([getJson(who, 'brain'), getJson(who, 'perception')]);
      if (mine !== seq) return;
      render(brain, perc);
      status.textContent = 'agent updated ' + new Date().toLocaleTimeString();
    } catch (e) {
      if (mine === seq) pane.replaceChildren(el('div', 'none', 'agent API: ' + e.message));
    }
  }

  function tag(root) {
    for (const e of root.querySelectorAll('.marker, .pl')) e.classList.toggle('agent', isAgent(e.dataset.name));
  }
  async function loadAgents() {
    try {
      const j = await (await fetch('/api/agents')).json();
      agents.clear();
      for (const n of j.agents || []) agents.set(n.toLowerCase(), n);
      tag(document);
      sync();
    } catch (e) { /* no agents configured or wowmap restarting */ }
  }
  // The player list is rebuilt on every tick; tag the new nodes. Map markers live in
  // Leaflet's marker pane, so the page calls AgentMind.tag() when it places them.
  const list = document.getElementById('list');
  if (list) new MutationObserver(() => tag(list)).observe(list, {childList: true});

  loadAgents();
  setInterval(loadAgents, 60000);
  setInterval(sync, 500);
  setInterval(refresh, 3000);
  return {refresh, tag, agents: () => [...agents.values()]};
})();
</script>
"""

# ---------------------------------------------------------------- page: activity feed
# UM-76: "Recent activity" section of the inspect drawer, fed by
# GET /api/character/<name>/activity (see activity.py). Inspect.renderBody appends
# this module's persistent node; the module polls on its own every 5 s. Event text
# comes from chat and the database, so it is rendered with textContent only.
ACTIVITY_CSS = r"""
  .activity .act { display:flex; gap:8px; padding:4px 0; border-bottom:1px solid #20263a;
                   font-size:12.5px; line-height:1.35; }
  .activity .act .ic { flex:0 0 18px; text-align:center; color:var(--dim); }
  .activity .act .tx { flex:1; min-width:0; overflow-wrap:anywhere; }
  .activity .act .meta { color:var(--dim); font-size:11px; }
  .activity .act.failed .tx { color:#ff8b8b; }
  .activity .inferred { font-size:10px; border:1px solid #6b5a2f; color:#f3b84b; border-radius:999px;
                        padding:0 6px; margin-left:6px; white-space:nowrap; }
  .activity .note { color:var(--dim); font-size:11px; padding:3px 0; }
  .activity .ic.k-combat { color:#ff6b6b; } .activity .ic.k-loot, .activity .ic.k-item { color:#7ddf8a; }
  .activity .ic.k-sell, .activity .ic.k-buy, .activity .ic.k-money { color:#f3b84b; }
  .activity .ic.k-chat, .activity .ic.k-whisper { color:#5fd0d8; }
  .activity .ic.k-level, .activity .ic.k-quest { color:#d68cf5; }
"""

ACTIVITY_JS = r"""
<script>
window.ActivityFeed = (() => {
  const ICONS = {combat: '⚔', loot: '✚', item: '✚', item_lost: '✖', sell: '$', buy: '$',
    money: '¤', trade: '⇄', chat: '“', whisper: '“', level: '▲', zone: '➜', quest: '!',
    mail: '✉', move: '→', group: '◆', session: '●', action: '•'};
  const SOURCES = {db: 'database', chat: 'public chat', audit: 'agent log'};
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const box = el('div', 'activity');
  const list = el('div');
  box.append(el('h3', null, 'Recent activity'), list);
  let shownFor = null, seq = 0, data = null;

  function ago(t) {
    const s = Math.max(0, Math.round(Date.now() / 1000 - t));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  function render() {
    const f = document.createDocumentFragment();
    const src = (data && data.sources) || {};
    if (!data) f.append(el('div', 'none', 'loading…'));
    else if (data.error) f.append(el('div', 'none', data.error));
    else {
      for (const [key, label] of [['db', 'database'], ['chat', 'chat feed']]) {
        if (src[key] && !src[key].ok) f.append(el('div', 'note', `${label} unreachable, events may be missing`));
      }
      if (!data.events.length) f.append(el('div', 'none', 'no activity recorded yet'));
      for (const e of data.events) {
        const failed = e.detail && e.detail.ok === false;
        const r = el('div', 'act' + (failed ? ' failed' : ''));
        r.append(el('span', 'ic k-' + e.kind, ICONS[e.kind] || '•'));
        const tx = el('div', 'tx', e.text);
        if (e.inferred) {
          const b = el('span', 'inferred', 'inferred');
          b.title = 'Not recorded by the server: deduced from database changes';
          tx.append(b);
        }
        const meta = el('div', 'meta', `${ago(e.t)} · ${SOURCES[e.source] || e.source}`);
        meta.title = new Date(e.t * 1000).toLocaleString();
        tx.append(meta);
        r.append(tx);
        f.append(r);
      }
    }
    list.replaceChildren(f);
  }

  async function refresh() {
    const who = Inspect.current();
    if (!who || who !== shownFor) return;
    const mine = ++seq;
    try {
      const r = await fetch(`/api/character/${encodeURIComponent(who)}/activity?limit=50`);
      const j = await r.json().catch(() => ({}));
      if (mine !== seq || who !== shownFor) return;
      data = r.ok ? j : {error: j.error || 'error ' + r.status};
    } catch (e) {
      if (mine !== seq) return;
      data = {error: 'activity unavailable: ' + e};
    }
    render();
  }

  // Called by Inspect.renderBody on every re-render; the node is reused.
  function section(name) {
    if (name !== shownFor) {
      shownFor = name;
      data = null;
      seq++;
      render();
      refresh();
    }
    return box;
  }

  setInterval(refresh, 5000);
  return {section, refresh};
})();
</script>
"""

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>WoW — Live map</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/leaflet.css">
<script src="/static/leaflet.js"></script>
<style>
  :root { --bg:#10131a; --panel:#1a1f2b; --line:#2a3244; --fg:#e6e9f0; --dim:#8b93a7; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.4 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         display:flex; height:100vh; overflow:hidden; }
  aside { width:290px; flex:0 0 290px; background:var(--panel);
          border-right:1px solid var(--line); display:flex; flex-direction:column; }
  aside h1 { font-size:15px; margin:0; padding:14px 16px 10px; letter-spacing:.3px;
             display:flex; align-items:center; justify-content:space-between; }
  aside h1 button, #expand { background:#141824; color:var(--dim); border:1px solid var(--line);
                             border-radius:6px; cursor:pointer; font:inherit; padding:2px 7px; }
  #expand { position:fixed; top:12px; left:12px; z-index:10; }
  aside .sub { padding:0 16px 12px; color:var(--dim); font-size:12px; }
  .stats { display:flex; gap:8px; padding:0 16px 12px; }
  .stat { flex:1; background:#141824; border:1px solid var(--line); border-radius:8px;
          padding:8px; text-align:center; }
  .stat b { display:block; font-size:20px; line-height:1.1; }
  .stat span { color:var(--dim); font-size:11px; }
  .list { flex:1; overflow:auto; padding:0 8px 12px; }
  .pl { display:flex; align-items:center; gap:8px; padding:7px 8px; border-radius:7px;
        cursor:pointer; }
  .pl:hover { background:#212836; }
  .dot { width:9px; height:9px; border-radius:50%; flex:0 0 9px; box-shadow:0 0 6px currentColor; }
  .pl .nm { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .pl .meta { color:var(--dim); font-size:12px; }
  .empty { color:var(--dim); padding:16px; text-align:center; font-size:13px; }
  main { flex:1; position:relative; display:flex; flex-direction:column; min-width:0; }
  .bar { display:flex; gap:10px; align-items:center; padding:10px 14px;
         border-bottom:1px solid var(--line); background:var(--panel); flex-wrap:wrap; }
  select, button { background:#141824; color:var(--fg); border:1px solid var(--line);
                   border-radius:7px; padding:6px 9px; font:inherit; }
  button { cursor:pointer; }
  button:hover { border-color:#3d4a63; }
  .tabs { display:flex; gap:6px; margin-left:auto; }
  .tabs button.on { background:#2b3550; border-color:#46557a; }
  button.on { background:#2b3550; border-color:#46557a; }
  /* isolate: Leaflet's panes and controls use z-index up to 1000, which must stay
     under the inspect drawer. */
  .stage { flex:1; position:relative; overflow:hidden; isolation:isolate; }
  /* The Leaflet map (L.CRS.Simple, map units = zone art pixels). */
  #map { position:absolute; inset:0; background:var(--bg); font:inherit; }
  #map.grid-fallback {
      background-image:linear-gradient(#232b3d 1px,transparent 1px),
                       linear-gradient(90deg,#232b3d 1px,transparent 1px);
      background-size:64px 64px; }
  #map.calibrating { cursor:crosshair; }
  .leaflet-bar a, .leaflet-bar a:hover { background:#141824; color:var(--fg); border-color:var(--line); }
  .leaflet-bar a.leaflet-disabled { background:#141824; color:var(--dim); }
  /* Leaflet positions a marker's top-left corner on its point (iconSize: null), so
     the icon is a zero-size box and its children are centred on it. */
  .marker, .subzone-label { width:0; height:0; }
  .marker .ring { position:absolute; left:-6.5px; top:-6.5px; width:13px; height:13px;
                  border-radius:50%; border:2px solid rgba(0,0,0,.65); }
  .marker .lbl { position:absolute; left:9.5px; top:-8.5px; white-space:nowrap; font-size:12px;
                 background:rgba(10,13,20,.82); padding:1px 6px; border-radius:5px;
                 border:1px solid var(--line); color:var(--fg); }
  .subzone-label span, #subzone-hover { position:absolute; white-space:nowrap; font-size:12px;
      color:#ffd25e; text-shadow:0 0 3px #000,0 0 3px #000,0 0 2px #000; pointer-events:none; }
  .subzone-label span { transform:translate(-50%,-50%); }
  .zone-glow { stroke:#ffd166; stroke-width:1.5; stroke-opacity:.9; fill:#ffd166; fill-opacity:.12; }
  #map.continent { cursor:pointer; }
  #subzone-hover { z-index:1000; transform:translate(12px,-130%); background:rgba(10,13,20,.82);
      color:var(--fg); text-shadow:none; padding:1px 7px; border-radius:5px; border:1px solid var(--line); }
  footer { padding:8px 14px; border-top:1px solid var(--line); color:var(--dim);
           font-size:12px; background:var(--panel); display:flex; gap:14px; }
  .pill { border:1px solid var(--line); border-radius:999px; padding:2px 9px; }
  .calibration-marker { border:2px solid #f3b84b; border-radius:50%; pointer-events:none;
                        box-shadow:0 0 0 2px rgba(0,0,0,.5); }
  .calibration-marker::before, .calibration-marker::after { content:""; position:absolute; background:#f3b84b; }
  .calibration-marker::before { width:2px; height:28px; left:6px; top:-7px; }
  .calibration-marker::after { height:2px; width:28px; left:-7px; top:6px; }
  .pl.sel { background:#2b3550; }
  .marker { cursor:pointer; }
  .marker.sel .ring { box-shadow:0 0 0 3px #f3b84b; }
  aside { width:var(--sidebar-w, 290px); flex:0 0 var(--sidebar-w, 290px); }
  @media (max-width: 480px) {
    aside { width:180px; flex:0 0 180px; }
    .stats, #resize-handle { display:none; }
  }
/* @inspect-css */
/* @chat-css */
/* @activity-css */
/* @agent-css */
</style></head>
<body>
<aside id="aside">
  <h1>Live map <button id="collapse" title="Collapse sidebar" aria-label="Collapse sidebar">«</button></h1>
  <div class="sub" id="sub">loading…</div>
  <div class="stats">
    <div class="stat"><b id="s-online">–</b><span>online</span></div>
    <div class="stat"><b id="s-world">–</b><span>in world</span></div>
    <div class="stat"><b id="s-inst">–</b><span>in instance</span></div>
  </div>
  <div class="side-tabs">
    <button id="tab-players" class="on">Players</button>
    <button id="tab-chat">Chat</button>
  </div>
  <div class="search-wrap" id="search-wrap">
    <input id="search" type="text" placeholder="Search characters (/)" autocomplete="off">
  </div>
  <div class="list" id="list"></div>
  <!-- @chat-html -->
  <div id="resize-handle" title="Drag to resize"></div>
</aside>
<button id="expand" hidden title="Show sidebar" aria-label="Show sidebar">»</button>
<main>
  <div class="bar">
    <label>Zone <select id="zone"></select></label>
    <button id="toContinent" title="Zoom out to the continent map (or right-click the map)">Continent</button>
    <button id="fit" title="Fit the whole zone in view">Fit zone</button>
    <button id="tglLabels" title="Show every subzone name (from WorldMapOverlay.dbc)">Labels: off</button>
    <button id="tglFog" class="on" title="With a character open, colour only the areas that character has explored, as its in-game map does">Fog of war: on</button>
    <button id="tglTrail" title="Show the path each character took since this page was opened">Trail: off</button>
    <button id="calibrate" title="Click a landmark and drag to line the markers up">Calibrate: off</button>
    <button id="saveCalibration" hidden>Save calibration</button>
    <div class="tabs">
      <button id="follow" title="Center on the selected character">Follow: off</button>
    </div>
  </div>
  <div class="stage" id="stage">
    <div id="map"></div>
    <div id="subzone-hover" hidden></div>
  </div>
  <footer>
    <span class="pill" id="f-refresh">–</span>
    <span class="pill" id="f-src">source: characters.position_*</span>
    <span class="pill" id="f-note"></span>
  </footer>
</main>
<!-- @inspect-html -->
<!-- @inspect-js -->
<!-- @activity-js -->
<script>window.CHAT_FEED_URL = "__CHAT_FEED_URL__";</script>
<!-- @chat-js -->
<!-- @agent-js -->
<script>
const $ = (id) => document.getElementById(id);
const CLASS_DEFAULT = "#8b93a7";
let areas = [], players = [], selected = null, follow = false, showTrail = false;
let showLabels = false;
let currentArea = null;
let calibrating = false, draftCalibration = null, calibrationReference = null, calibrationDrag = null;
// Fog of war: with a character open in the inspect drawer the zone art shows only
// what that character has explored. `fog` is its /api/character/<name>/explored
// answer, null with nobody open (then the art is the fully explored zone).
let fogOn = true, fogFor = null, fog = null;

// ---- the map: Leaflet with L.CRS.Simple. Map units are zone-art pixels with y
// pointing down, so image point (x, y) is LatLng(-y, x) and nothing else on the page
// has to know about Leaflet's axes. Normalised coords are fractions of the game's
// 1002x668 map frame, which the 1024x768 tile sheet overflows (transform.MAP_FRAME_W/H,
// #109), so positions scale by the frame and the art is laid over the whole sheet.
const MAP_FRAME_W = 1002, MAP_FRAME_H = 668, SHEET_W = 1024, SHEET_H = 768;
const ll = (x, y) => L.latLng(-y, x);
const FRAME = L.latLngBounds(ll(0, MAP_FRAME_H), ll(MAP_FRAME_W, 0));
const ZOOM_OUT = 1, ZOOM_IN = 3;          // zoom levels around "fit zone": half size to 8x
const map = L.map('map', {
  crs: L.CRS.Simple, attributionControl: false, zoomSnap: 0, zoomDelta: 1,
  maxBounds: FRAME.pad(0.5), maxBoundsViscosity: 0.8,
});
map.setView(FRAME.getCenter(), 0);
map.createPane('art').style.zIndex = 300;        // under trails (overlayPane, 400)
map.createPane('labels').style.zIndex = 550;     // under player markers (markerPane, 600)
const trailLayer = L.layerGroup().addTo(map);
const labelLayer = L.layerGroup().addTo(map);
const playerLayer = L.layerGroup().addTo(map);
const markers = new Map();                       // character name -> L.marker
let art = null, artKey = '', shownZone = null, lastFit = null, calibrationPin = null;
let continents = [], zoneGlow = null;

// The zoom at which the zone's map frame fills the stage. getBoundsZoom clamps to the
// current zoom range, which belongs to the previous stage size, so lift it first.
function fitZoom() {
  map.setMinZoom(-Infinity); map.setMaxZoom(Infinity);
  return map.getBoundsZoom(FRAME);
}
// Fit the zone's map frame in the stage and allow zooming around that level.
function fitZone() {
  // A zoom animation in flight sets its own view when it ends, undoing the fit, and
  // Leaflet has no public way to cancel one (vendored 1.9.4), so fit after it.
  if (map._animatingZoom) { map.once('zoomend', fitZone); return; }
  map.invalidateSize({pan: false});
  const z = fitZoom();
  map.fitBounds(FRAME, {animate: false});
  map.setMinZoom(z - ZOOM_OUT); map.setMaxZoom(z + ZOOM_IN);
  lastFit = {zoom: map.getZoom(), center: map.getCenter()};
}
// True until the user zooms or pans away from the fitted view.
function atFit() {
  if (!lastFit) return true;
  const c = map.getCenter();
  return Math.abs(map.getZoom() - lastFit.zoom) < 1e-6
    && Math.abs(c.lat - lastFit.center.lat) < 0.5 && Math.abs(c.lng - lastFit.center.lng) < 0.5;
}
// The stage changed size (window, sidebar, inspect drawer). A fitted view stays
// fitted; a view the user chose is kept, with the zoom range moved to the new fit.
function onResize() {
  if (atFit()) return fitZone();
  map.invalidateSize({pan: false});
  const z = fitZoom();
  map.setMinZoom(z - ZOOM_OUT); map.setMaxZoom(z + ZOOM_IN);
}
// A DOM pointer event -> zone-art pixels.
function eventPoint(e) {
  const p = map.mouseEventToLatLng(e);
  return {x: p.lng, y: -p.lat};
}

async function loadAreas() {
  const r = await fetch('/api/areas');
  const d = await r.json();
  // Continents are areas too (area_id "c<map>", continent_view): the same stage shows
  // their art, with their zones' boxes as the hover/label/click targets.
  continents = (d.continents || []).filter(c => c.has_image);
  for (const c of continents) {
    c.subzones = c.zones.filter(z => z.box).map(z => ({
      name: z.name, area_id: z.area_id, hit: z.box,
      label: [z.box[0] + z.box[2] / 2, z.box[1] + z.box[3] / 2]}));
  }
  areas = continents.concat(d.areas);
  const sel = $('zone');
  const usable = areas.filter(a => a.has_image);
  const list = usable.length ? usable : areas;
  sel.innerHTML = '';
  const groups = [['Continents', list.filter(a => a.continent_view)],
                  ['Zones', list.filter(a => !a.continent_view)]];
  for (const [label, members] of groups) {
    if (!members.length) continue;
    const g = document.createElement('optgroup');
    g.label = label;
    for (const a of members) {
      const o = document.createElement('option');
      o.value = a.area_id;
      o.textContent = a.name + (a.has_image ? '' : ' (no image)');
      g.appendChild(o);
    }
    sel.appendChild(g);
  }
  // default to the last-viewed zone, else a zone with players, else first
  const inuse = new Set((d.in_use || []).map(z => z.zone));
  const lastZone = loadState('lastZone');
  const pick = list.find(a => String(a.area_id) === String(lastZone))
    || list.find(a => inuse.has(a.area_id)) || list[0];
  if (pick) { sel.value = pick.area_id; currentArea = pick; }
  sel.onchange = () => showArea(list.find(a => String(a.area_id) === sel.value));
}

function areaFor(zone) { return areas.find(a => a.area_id === zone); }
// The continent map a zone is drawn on, if its art is there.
function continentOf(a) {
  if (!a || a.continent_view) return null;
  return continents.find(c => c.zones.some(z => z.area_id === a.area_id)) || null;
}
// Switch the stage to a zone or a continent: like the game's world map, left-click a
// zone on the continent to open it and right-click (or "Continent") to go back out.
function showArea(a) {
  if (!a) return;
  // Calibration is per zone: leave it before a continent disables its button.
  if (a.continent_view && calibrating) $('calibrate').click();
  currentArea = a;
  $('zone').value = a.area_id;
  saveState('lastZone', a.area_id);
  $('subzone-hover').hidden = true;
  resetCalibration();
  draw();
}
// With Follow on, the stage goes where the selected character goes: stay on a
// continent that still holds it, else open its zone.
function followAcrossZones() {
  if (!follow || !selected || calibrating || !currentArea) return;
  const p = players.find(pl => pl.name === selected);
  if (!p || !p.in_world) return;
  if (currentArea.continent_view && p.continent === currentArea.area_id) return;
  const a = areaFor(p.zone);
  if (a && a !== currentArea) showArea(a);
}
function zoomOut() {
  const c = continentOf(currentArea);
  if (c && !calibrating) showArea(c);
}

// Toggle-only highlighting, safe to call from inside a click handler: rebuilding
// #list or #markers here (as renderList()/place() do) would detach the very
// element the click originated on before the event finishes bubbling, which
// makes Inspect's click-away handler misfire and close the drawer it just opened.
function markListAndMarkersSelected() {
  for (const e of document.querySelectorAll('.pl')) e.classList.toggle('sel', e.dataset.name === selected);
  for (const e of document.querySelectorAll('.marker')) e.classList.toggle('sel', e.dataset.name === selected);
}

// Unified selection: called from the player list, a map marker, or a chat sender —
// highlights the character everywhere and pans the map to their zone.
function selectCharacter(name) {
  selected = name;
  if (!calibrating) Inspect.open(name);
  const p = players.find(pl => pl.name === name);
  const onContinent = currentArea && currentArea.continent_view && p
    && p.continent === currentArea.area_id;
  if (p && p.in_world && !onContinent) {
    const a = areaFor(p.zone);
    if (a && (!currentArea || currentArea.area_id !== a.area_id)) showArea(a);
  }
  markListAndMarkersSelected();
}
window.selectCharacter = selectCharacter;

// The zone art to show: fully explored, or as the open character has explored it
// (the base parchment plus that character's revealed overlays, composed by the server).
function artUrl(a) {
  if (!fog || !a.fog) return '/maps/' + a.image;
  const ids = fog.zones[a.area_id] || [];
  return ids.length ? `/maps/${a.area_id}.png?explored=${ids.join(',')}` : `/maps/${a.area_id}_base.png`;
}

async function loadFog() {
  const name = fogFor;
  let next = null;
  if (name) {
    try {
      const r = await fetch(`/api/character/${encodeURIComponent(name)}/explored`);
      if (r.ok) next = await r.json();
    } catch (e) { /* keep the fully explored art */ }
    if (name !== fogFor) return;               // the selection changed while loading
  }
  const changed = JSON.stringify(next && next.zones) !== JSON.stringify(fog && fog.zones);
  fog = next;
  if (changed) draw();
}
// The drawer opens and closes from several places (list, marker, chat, Esc, click
// away), so follow Inspect.current() instead of hooking each of them.
function syncFog() {
  const name = fogOn ? Inspect.current() : null;
  if (name === fogFor) return;
  fogFor = name;
  loadFog();
}

// Zone art and view. The view is only re-fitted when the zone changes, so the 5 s
// refresh (tick -> place) never resets the user's zoom or pan.
function draw() {
  const a = currentArea;
  const key = a && a.has_image ? artUrl(a) : '';
  if (key !== artKey) {
    if (art) art.remove();
    art = null;
    artKey = key;
    if (key) {
      art = L.imageOverlay(key, [ll(0, SHEET_H), ll(SHEET_W, 0)], {pane: 'art'}).addTo(map);
      const layer = art;
      // The extractor writes the whole 1024x768 sheet; follow the file if that changes.
      layer.on('load', () => {
        const im = layer.getElement();
        if (im.naturalWidth) layer.setBounds([ll(0, im.naturalHeight), ll(im.naturalWidth, 0)]);
      });
    }
  }
  $('map').classList.toggle('grid-fallback', !key);
  $('map').classList.toggle('continent', !!(a && a.continent_view));
  $('toContinent').disabled = !continentOf(a);
  $('calibrate').disabled = !!(a && a.continent_view);   // offsets are per zone
  if (zoneGlow) { zoneGlow.remove(); zoneGlow = null; }
  const zone = a ? a.area_id : null;
  if (zone !== shownZone) { shownZone = zone; fitZone(); }
  place();
}

function calibration() {
  if (calibrating && draftCalibration) return draftCalibration;
  return (currentArea && currentArea.calibration) || {dx: 0, dy: 0};
}

// Keep calibration at the final world->normalised->pixel step: offsets are zone-art
// pixels, which are also the map's units, so they hold at every zoom level.
function pxOf(nx, ny) {
  const c = calibration();
  return [nx * MAP_FRAME_W + c.dx, ny * MAP_FRAME_H + c.dy];
}
function px(p) { return pxOf(p.norm_x, p.norm_y); }

function resetCalibration() {
  draftCalibration = currentArea ? {...(currentArea.calibration || {dx: 0, dy: 0})} : null;
  calibrationReference = null;
}

// Fill a marker's icon. The children are only rebuilt when what they show changes:
// replacing them during a click would detach the click target (see
// markListAndMarkersSelected).
function fillMarker(e, p) {
  const shown = `${p.name}|${p.level}|${p.class_color}`;
  if (e.dataset.shown !== shown) {
    e.dataset.shown = shown;
    e.dataset.name = p.name;
    const ring = document.createElement('div');
    ring.className = 'ring';
    ring.style.background = ring.style.color = p.class_color;
    const lbl = document.createElement('div');
    lbl.className = 'lbl';
    const lvl = document.createElement('span');
    lvl.style.opacity = '.6';
    lvl.textContent = p.level;
    lbl.append(p.name + ' ', lvl);
    e.replaceChildren(ring, lbl);
  }
  e.classList.toggle('sel', p.name === selected);
  e.title = `${p.name} — ${p.class_name} ${p.race_name} lvl ${p.level}\n${placeText(p)}`
    + (p.map_coords ? `\n${mapCoordsText(p)}` : '');
}

function place() {
  const a = currentArea;
  const onContinent = !!(a && a.continent_view);
  const here = !a ? [] : onContinent
    ? players.filter(p => p.in_world && p.continent === a.area_id)
    : players.filter(p => p.in_world && p.zone === a.area_id && p.norm_x !== null);
  const seen = new Set();
  for (const p of here) {
    // A continent has no calibration: it is one transform for the whole map.
    const [x, y] = onContinent ? [p.cont_x * MAP_FRAME_W, p.cont_y * MAP_FRAME_H] : px(p);
    seen.add(p.name);
    let m = markers.get(p.name);
    if (!m) {
      m = L.marker(ll(x, y), {icon: L.divIcon({className: 'marker', iconSize: null, html: ''}),
                              keyboard: false, riseOnHover: true}).addTo(playerLayer);
      m.on('click', () => { if (!calibrating) selectCharacter(p.name); });
      markers.set(p.name, m);
    } else {
      m.setLatLng(ll(x, y));
    }
    fillMarker(m.getElement(), p);
    if (p.name === selected && follow && !calibrating) map.panTo(ll(x, y));
  }
  for (const [name, m] of markers) {
    if (!seen.has(name)) { m.remove(); markers.delete(name); }
  }
  if (calibrationPin) { calibrationPin.remove(); calibrationPin = null; }
  if (calibrating && calibrationReference) {
    const c = calibration();
    calibrationPin = L.marker(ll(calibrationReference.x + c.dx, calibrationReference.y + c.dy), {
      icon: L.divIcon({className: 'calibration-marker', iconSize: [18, 18], html: ''}),
      interactive: false, keyboard: false, title: 'Landmark — drag to adjust'}).addTo(map);
  }
  $('map').classList.toggle('calibrating', calibrating);
  if (calibrating) map.dragging.disable(); else map.dragging.enable();
  AgentMind.tag($('map'));
  drawLabels();
  drawTrails();
  $('f-note').textContent = `${here.length} ${onContinent ? 'on this continent' : 'in this zone'}`;
}

// ---- trails: where each character has been since this page was opened. The server
// keeps no position history, so points are collected from the 5 s refresh, as
// normalised coords (calibration is applied when drawing).
const TRAIL_MAX_POINTS = 720;                    // an hour at one point per refresh
const trails = new Map();                        // name -> {zone, color, pts: [[nx, ny], ...]}
function recordTrails() {
  const online = new Set();
  for (const p of players) {
    online.add(p.name);
    if (!p.in_world || p.norm_x === null) continue;
    let t = trails.get(p.name);
    if (!t || t.zone !== p.zone) { t = {zone: p.zone, pts: []}; trails.set(p.name, t); }
    t.color = p.class_color;
    const last = t.pts[t.pts.length - 1];
    if (last && last[0] === p.norm_x && last[1] === p.norm_y) continue;
    t.pts.push([p.norm_x, p.norm_y]);
    if (t.pts.length > TRAIL_MAX_POINTS) t.pts.shift();
  }
  for (const name of trails.keys()) if (!online.has(name)) trails.delete(name);
}
function drawTrails() {
  trailLayer.clearLayers();
  if (!showTrail || !currentArea) return;
  for (const t of trails.values()) {
    if (t.zone !== currentArea.area_id || t.pts.length < 2) continue;
    L.polyline(t.pts.map(([nx, ny]) => ll(...pxOf(nx, ny))),
               {color: t.color || CLASS_DEFAULT, weight: 2, opacity: 0.85, interactive: false})
      .addTo(trailLayer);
  }
}

// ---- subzones: WorldMapOverlay hit rects, already in image pixels (see /api/areas)
function drawLabels() {
  labelLayer.clearLayers();
  const a = currentArea;
  if (!showLabels || !a || !a.has_image) return;
  for (const sz of a.subzones || []) {
    const text = document.createElement('span');
    text.textContent = sz.name;
    L.marker(ll(sz.label[0], sz.label[1]), {
      icon: L.divIcon({className: 'subzone-label', iconSize: null, html: text}),
      pane: 'labels', interactive: false, keyboard: false}).addTo(labelLayer);
  }
}

// The smallest rect under the point wins: Ruins of Silvermoon sits inside Silvermoon City.
function subzoneAt(x, y) {
  if (currentArea && currentArea.continent_view) return zoneAt(x, y);
  let best = null, bestArea = Infinity;
  for (const sz of (currentArea && currentArea.subzones) || []) {
    const r = sz.hit || sz.art;
    if (!r || x < r[0] || y < r[1] || x > r[0] + r[2] || y > r[1] + r[3]) continue;
    if (r[2] * r[3] < bestArea) { best = sz; bestArea = r[2] * r[3]; }
  }
  return best;
}

// On a continent the zone rects are bounding boxes that overlap a lot (half of
// Kalimdor is under two or more), so "the box under the pointer" is often the wrong
// zone. A zone's explored-area rects (its subzones) cover its land much more tightly:
// prefer the zones whose land is under the pointer, then the nearest box centre.
// Cities have no subzones and count as land all over their small box.
function onZoneLand(z, x, y) {
  const a = areaFor(z.area_id);
  if (!a || !a.subzones || !a.subzones.length) return true;
  const [bx, by, bw, bh] = z.hit;
  const zx = (x - bx) / bw * MAP_FRAME_W, zy = (y - by) / bh * MAP_FRAME_H;
  return a.subzones.some(sz => {
    const r = sz.hit || sz.art;
    return r && zx >= r[0] && zy >= r[1] && zx <= r[0] + r[2] && zy <= r[1] + r[3];
  });
}
function zoneAt(x, y) {
  const under = currentArea.subzones.filter(z => {
    const [bx, by, bw, bh] = z.hit;
    return x >= bx && y >= by && x <= bx + bw && y <= by + bh;
  });
  const land = under.filter(z => onZoneLand(z, x, y));
  let best = null, bestD = Infinity;
  for (const z of (land.length ? land : under)) {
    const [bx, by, bw, bh] = z.hit;
    const d = Math.hypot(x - bx - bw / 2, y - by - bh / 2);
    if (d < bestD) { best = z; bestD = d; }
  }
  return best;
}

function renderList() {
  const l = $('list');
  const query = ($('search').value || '').trim().toLowerCase();
  const filtered = query ? players.filter(p => p.name.toLowerCase().includes(query)) : players;
  if (!players.length) { l.innerHTML = '<div class="empty">Nobody online</div>'; return; }
  if (!filtered.length) { l.innerHTML = '<div class="empty">No character matches the search</div>'; return; }
  l.innerHTML = '';
  for (const p of filtered) {
    const e = document.createElement('div');
    e.className = 'pl' + (p.name === Inspect.current() ? ' sel' : '');
    e.dataset.name = p.name;
    const dot = document.createElement('span');
    dot.className = 'dot';
    dot.style.background = dot.style.color = p.class_color;
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = p.name;
    const meta = document.createElement('span');
    meta.className = 'meta';
    const where = p.map_coords ? `${p.zone_name} ${mapCoordsText(p)}` : p.zone_name;
    meta.textContent = `${p.level} ${p.class_name} · ${p.in_world ? where : p.continent_name + ' (instance)'}`;
    e.append(dot, nm, meta);
    e.onclick = () => selectCharacter(p.name);
    l.appendChild(e);
  }
}

async function tick() {
  try {
    const [pr, sr] = await Promise.all([fetch('/api/players'), fetch('/api/summary')]);
    players = (await pr.json()).players;
    const s = await sr.json();
    $('s-online').textContent = s.online;
    $('s-world').textContent = s.in_world;
    $('s-inst').textContent = s.in_instance;
    $('sub').textContent = s.zones.length ? s.zones.join(' · ') : 'nobody in the world';
    $('f-refresh').textContent = 'updated ' + new Date().toLocaleTimeString();
    recordTrails();
    followAcrossZones();
    renderList(); place();
  } catch (e) { $('sub').textContent = 'error: ' + e; }
  Inspect.refresh();
  if (fogFor) loadFog();                       // the character may have explored more
}

$('fit').onclick = () => fitZone();
$('tglTrail').onclick = (e) => {
  showTrail = !showTrail;
  e.target.textContent = 'Trail: ' + (showTrail ? 'on' : 'off');
  e.target.classList.toggle('on', showTrail);
  drawTrails();
};
$('follow').onclick = (e) => {
  follow = !follow;
  e.target.textContent = 'Follow: ' + (follow ? 'on' : 'off');
  e.target.classList.toggle('on', follow);
  place();
};
$('calibrate').onclick = (e) => {
  calibrating = !calibrating;
  if (calibrating) resetCalibration();
  e.target.textContent = 'Calibrate: ' + (calibrating ? 'on' : 'off');
  e.target.classList.toggle('on', calibrating);
  $('saveCalibration').hidden = !calibrating;
  place();
};
// Calibration: press on a landmark and drag it to where it should be. Dragging the
// map is off meanwhile (see place()); wheel and pinch zoom still work, so the
// offset can be set zoomed in.
$('map').addEventListener('pointerdown', (e) => {
  if (!calibrating || !currentArea || e.target.closest('.leaflet-control')) return;
  const point = eventPoint(e);
  const c = calibration();
  calibrationReference = {x: point.x - c.dx, y: point.y - c.dy};
  calibrationDrag = {x: point.x, y: point.y, dx: c.dx, dy: c.dy};
  $('map').setPointerCapture(e.pointerId);
  place();
});
$('map').addEventListener('pointermove', (e) => {
  if (!calibrationDrag || !draftCalibration) return;
  const {x, y} = eventPoint(e);
  draftCalibration.dx = Math.round((calibrationDrag.dx + x - calibrationDrag.x) * 100) / 100;
  draftCalibration.dy = Math.round((calibrationDrag.dy + y - calibrationDrag.y) * 100) / 100;
  place();
});
$('map').addEventListener('pointermove', (e) => {
  const hover = $('subzone-hover');
  if (calibrating || !currentArea || !currentArea.has_image) { hover.hidden = true; return; }
  const {x, y} = eventPoint(e);
  const sz = subzoneAt(x, y);
  hover.hidden = !sz;
  if (currentArea.continent_view) {
    if (zoneGlow) { zoneGlow.remove(); zoneGlow = null; }
    if (sz) {
      const [bx, by, bw, bh] = sz.hit;
      zoneGlow = L.rectangle([ll(bx, by + bh), ll(bx + bw, by)],
                             {className: 'zone-glow', interactive: false}).addTo(map);
    }
  }
  if (!sz) return;
  hover.textContent = sz.name;
  const at = map.mouseEventToContainerPoint(e);
  hover.style.left = at.x + 'px'; hover.style.top = at.y + 'px';
});
$('map').addEventListener('pointerleave', () => {
  $('subzone-hover').hidden = true;
  if (zoneGlow) { zoneGlow.remove(); zoneGlow = null; }
});
$('tglLabels').onclick = (e) => {
  showLabels = !showLabels;
  e.target.textContent = 'Labels: ' + (showLabels ? 'on' : 'off');
  e.target.classList.toggle('on', showLabels);
  drawLabels();
};
$('tglFog').onclick = (e) => {
  fogOn = !fogOn;
  e.target.textContent = 'Fog of war: ' + (fogOn ? 'on' : 'off');
  e.target.classList.toggle('on', fogOn);
  syncFog();
};
setInterval(syncFog, 300);
for (const event of ['pointerup', 'pointercancel']) {
  $('map').addEventListener(event, () => { calibrationDrag = null; });
}
$('saveCalibration').onclick = async () => {
  if (!currentArea || !draftCalibration) return;
  const r = await fetch('/api/calibrate', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({area_id: currentArea.area_id, ...draftCalibration})});
  const saved = await r.json();
  if (!r.ok) { $('f-note').textContent = 'save failed: ' + saved.error; return; }
  currentArea.calibration = {dx: saved.dx, dy: saved.dy};
  draftCalibration = {...currentArea.calibration};
  place();
  $('f-note').textContent = `calibration saved: ${saved.dx}px, ${saved.dy}px`;
};
addEventListener('resize', onResize);
// A plain click on the map closes the inspect drawer; Leaflet fires no click after a drag.
// On a continent it opens the zone under the pointer instead.
map.on('click', (e) => {
  if (calibrating) return;
  if (currentArea && currentArea.continent_view) {
    const z = zoneAt(e.latlng.lng, -e.latlng.lat);
    const a = z && areaFor(z.area_id);
    if (a && a.has_image) return showArea(a);     // only zones the selector offers
  }
  Inspect.close();
});
map.on('contextmenu', zoomOut);                  // also keeps the browser menu away
$('toContinent').onclick = zoomOut;

// ---- UI state persisted in localStorage: sidebar width/collapse, active tab,
// last zone (used above in loadAreas) and pinned character. Every access is
// wrapped in try/catch: private-browsing / disabled storage must not break the page.
const STATE_PREFIX = 'wowmap.';
function loadState(key) {
  try { return JSON.parse(localStorage.getItem(STATE_PREFIX + key)); }
  catch (e) { return null; }
}
function saveState(key, value) {
  try { localStorage.setItem(STATE_PREFIX + key, JSON.stringify(value)); }
  catch (e) { /* storage unavailable, state just won't persist */ }
}

const MIN_ASIDE_W = 220, MAX_ASIDE_W = 560;
function setAsideWidth(w) {
  const clamped = Math.max(MIN_ASIDE_W, Math.min(MAX_ASIDE_W, w));
  document.documentElement.style.setProperty('--sidebar-w', clamped + 'px');
  return clamped;
}
(() => {
  const saved = loadState('asideWidth');
  if (saved) setAsideWidth(saved);
})();
let resizeDrag = null;
$('resize-handle').addEventListener('pointerdown', (e) => {
  resizeDrag = {startX: e.clientX, startW: $('aside').getBoundingClientRect().width};
  $('resize-handle').setPointerCapture(e.pointerId);
});
$('resize-handle').addEventListener('pointermove', (e) => {
  if (!resizeDrag) return;
  setAsideWidth(resizeDrag.startW + (e.clientX - resizeDrag.startX));
  onResize();
});
for (const event of ['pointerup', 'pointercancel']) {
  $('resize-handle').addEventListener(event, () => {
    if (resizeDrag) saveState('asideWidth', $('aside').getBoundingClientRect().width);
    resizeDrag = null;
  });
}

function setCollapsed(collapsed) {
  document.body.classList.toggle('aside-collapsed', collapsed);
  $('expand').hidden = !collapsed;
  saveState('collapsed', collapsed);
  dispatchEvent(new Event('resize'));
}
$('collapse').onclick = () => setCollapsed(true);
$('expand').onclick = () => setCollapsed(false);
if (loadState('collapsed')) setCollapsed(true);

// Tabs: players list vs. chat panel share the sidebar; chat only connects (SSE)
// while its tab is visible, so a viewer who never opens it costs nothing extra.
function setTab(tab) {
  const isChat = tab === 'chat';
  $('tab-players').classList.toggle('on', !isChat);
  $('tab-chat').classList.toggle('on', isChat);
  $('list').hidden = isChat;
  $('search-wrap').hidden = isChat;
  $('chat').hidden = !isChat;
  $('chat-status').hidden = !isChat || Chat.shown();
  saveState('tab', tab);
  if (isChat) Chat.connect(); else Chat.disconnect();
}
$('tab-players').onclick = () => setTab('players');
$('tab-chat').onclick = () => setTab('chat');
setTab(loadState('tab') === 'chat' ? 'chat' : 'players');

// Keyboard: `/` focuses the character search, `c` toggles the chat tab, Esc for
// the drawer is handled by Inspect itself. Ignore these while typing elsewhere.
addEventListener('keydown', (e) => {
  const typing = /^(input|textarea|select)$/i.test(e.target.tagName);
  if (e.key === '/' && !typing) {
    e.preventDefault();
    setTab('players');
    $('search').focus();
  } else if (e.key === 'c' && !typing) {
    setTab($('tab-chat').classList.contains('on') ? 'players' : 'chat');
  }
});
$('search').addEventListener('input', renderList);

// Remember the pinned (open) character across reloads.
document.getElementById('inspect-close').addEventListener('click', () => saveState('pinned', null));
addEventListener('keydown', (e) => { if (e.key === 'Escape') saveState('pinned', null); });
const originalSelectCharacter = selectCharacter;
selectCharacter = (name) => { originalSelectCharacter(name); saveState('pinned', name); };
window.selectCharacter = selectCharacter;
if (!location.hash) {
  const pinned = loadState('pinned');
  if (pinned) Inspect.open(pinned);
}

// draw() loads the zone art; tick() alone only places markers, which left the stage
// an empty grid on first load until the zone was changed.
loadAreas().then(() => { draw(); return tick(); });
setInterval(tick, 5000);
</script>
</body></html>
"""
PAGE = (PAGE
        .replace("/* @inspect-css */", INSPECT_CSS)
        .replace("<!-- @inspect-html -->", INSPECT_HTML)
        .replace("<!-- @inspect-js -->", INSPECT_JS)
        .replace("/* @activity-css */", ACTIVITY_CSS)
        .replace("<!-- @activity-js -->", ACTIVITY_JS)
        .replace("/* @chat-css */", CHAT_CSS)
        .replace("<!-- @chat-html -->", CHAT_HTML)
        .replace("<!-- @chat-js -->", CHAT_JS)
        .replace("/* @agent-css */", AGENT_CSS)
        .replace("<!-- @agent-js -->", AGENT_JS)
        .replace("__CHAT_FEED_URL__", CHAT_FEED_URL))


def start_activity():
    """Open the activity store and start its pollers; the feed is optional, so a
    store that can't be opened only disables it."""
    global activity
    if not ACTIVITY_DB:
        return None
    try:
        store = activity_feed.ActivityStore(ACTIVITY_DB)
    except Exception as e:  # noqa: BLE001 - OSError, sqlite3.Error, ...
        log.error("activity feed disabled, cannot open %s: %s", ACTIVITY_DB, e)
        return None
    activity = activity_feed.Activity(
        store, connect=db, zone_name=lambda z: tables().zone_name(z),
        chat_url=ACTIVITY_CHAT_FEED_URL, audit_dir=AUDIT_DIR)
    activity.start()
    log.info("activity feed: store=%s chat=%s audit=%s", ACTIVITY_DB,
             ACTIVITY_CHAT_FEED_URL or "off", AUDIT_DIR or "off")
    return activity


if __name__ == "__main__":
    start_activity()
    log.info("wowmap on :%d (db=%s dbc=%s maps=%s)", LISTEN_PORT, MYSQL["host"], DBC_DIR, MAPS_DIR)
    threading.Thread(target=names, name="load-names", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
