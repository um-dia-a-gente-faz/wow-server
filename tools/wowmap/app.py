#!/usr/bin/env python3
"""wowmap — live map of online players for a TrinityCore 3.3.5a server.

Serves:
    GET /                     the map page (single file, no build step)
    GET /api/players          online players with world + normalised coords
    GET /api/character/<name> one character's state, inventory and progression
    GET /api/areas?map=<id>   zone tiles: rect, name, whether art is available
    POST /api/calibrate       save a per-zone pixel offset
    GET /api/agents           names of agents with an observability API (UM-50)
    GET /api/agent/<name>/<view>  proxy to that agent's read-only GET /<view>
                              (healthz, state, perception, brain)
    GET /maps/<file>          extracted zone map images (static)
    GET /healthz              liveness

Coordinates come from `characters.characters`; the world->normalised transform comes
from the client DBCs (see transform.py). Map art is extracted from the client MPQs by
extract_maps.py and served from MAPS_DIR.

Env:
    MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD   as elsewhere in this repo
    DBC_DIR     default /dbc        (WorldMapArea.dbc, AreaTable.dbc, Map.dbc)
    MAPS_DIR    default /maps       (extracted PNGs)
    GRID_MAPS_DIR default /server-maps (the worldserver's maps/*.map, for subzones)
    LISTEN_PORT default 9400
    CHAT_FEED_URL default ""        (derived from the page's own hostname at :9500)
    AGENT_API_URLS default ""       "Name=http://host:9601,Name2=http://host:9602" — agent
                                    observability APIs (agent/http_api.py) to proxy
"""
import json
import logging
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
from urllib.parse import urlparse, parse_qs, unquote

import pymysql

from transform import DbcTables, GridAreas

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
GRID_MAPS_DIR = os.environ.get("GRID_MAPS_DIR", "/server-maps")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9400"))
CALIBRATION_FILE = os.environ.get(
    "CALIBRATION_FILE", os.path.join(os.path.dirname(__file__), "calibration.json")
)
MAX_CALIBRATION_PAYLOAD_BYTES = 65536
# Empty means "derive from the page's own hostname at :9500" (see CHAT_JS below);
# set this only when chat-feed isn't reachable on the same host as wowmap.
CHAT_FEED_URL = os.environ.get("CHAT_FEED_URL", "")


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

_tables = None
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


def db():
    return pymysql.connect(**MYSQL)


_grid_areas = None


def grid_areas():
    global _grid_areas
    if _grid_areas is None:
        _grid_areas = GridAreas(GRID_MAPS_DIR)
    return _grid_areas


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
                "playtime_seconds": totaltime,
                **position_fields(t, cmap, zone, float(x), float(y)),
            })
    return out


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
        if area_id in seen:
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
            "calibration": calibrations.get(str(area_id), {"dx": 0, "dy": 0}),
        })
    rows.sort(key=lambda a: a["name"])
    return rows


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
                   COALESCE(it.name, CONCAT('Item ', ii.itemEntry)), ii.count
            FROM characters.character_inventory ci
            JOIN characters.item_instance ii ON ci.item = ii.guid
            LEFT JOIN world.item_template it ON ii.itemEntry = it.entry
            WHERE ci.guid = %s
            ORDER BY ci.bag, ci.slot
        """, (guid,), "inventory")
        talents = best_effort(cur, """
            SELECT spell, talentGroup
            FROM characters.character_talent
            WHERE guid = %s
            ORDER BY talentGroup, spell
        """, (guid,), "talents")
        reputation = best_effort(cur, """
            SELECT faction, standing
            FROM characters.character_reputation
            WHERE guid = %s
            ORDER BY faction
        """, (guid,), "reputation")
        achievements = best_effort(cur, """
            SELECT achievement, date
            FROM characters.character_achievement
            WHERE guid = %s
            ORDER BY date DESC, achievement
        """, (guid,), "achievements")

    t = tables()
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
        # Current values only: max health/power are computed by the worldserver
        # at runtime and never persisted (docs/ROADMAP.md, Phase B option 1).
        "health": health,
        "power": dict(zip(POWER_NAMES, powers)),
        "inventory": [
            {"bag": bag, "slot": slot, "item_guid": item_guid, "item_entry": item_entry,
             "item_name": item_name, "count": count}
            for bag, slot, item_guid, item_entry, item_name, count in inventory
        ],
        "talents": [{"spell": spell, "spec": spec} for spell, spec in talents],
        "reputation": [
            {"faction": faction, "standing": standing}
            for faction, standing in reputation
        ],
        "achievements": [
            {"achievement": achievement, "date": date}
            for achievement, date in achievements
        ],
    }


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
                fp = os.path.join(MAPS_DIR, fn)
                if os.path.isfile(fp) and fn.endswith(".png"):
                    with open(fp, "rb") as f:
                        return self._send(200, f.read(), "image/png", cache="max-age=86400")
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
  .drawer .none { color:var(--dim); font-size:12px; padding:3px 0; }
  .drawer details { margin-top:10px; border:1px solid var(--line); border-radius:7px; padding:0 10px; }
  .drawer details[open] { padding-bottom:8px; }
  .drawer summary { cursor:pointer; padding:7px 0; color:var(--dim); font-size:12px;
                    text-transform:uppercase; letter-spacing:.6px; }
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

const Inspect = (() => {
  const EQUIP_SLOTS = ['Head', 'Neck', 'Shoulder', 'Shirt', 'Chest', 'Waist', 'Legs',
    'Feet', 'Wrist', 'Hands', 'Finger 1', 'Finger 2', 'Trinket 1', 'Trinket 2', 'Back',
    'Main Hand', 'Off Hand', 'Ranged', 'Tabard'];
  const POWER_LABELS = {mana: 'Mana', rage: 'Rage', focus: 'Focus', energy: 'Energy',
    happiness: 'Happiness', rune: 'Runes', runic_power: 'Runic Power'};
  // The server keeps rage and runic power in tenths (1000 is shown as 100 in game).
  const POWER_SCALE = {rage: 10, runic_power: 10};
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
        const c = {label: bank ? `Bank bag ${s - 66}` : `Bag ${s - 18}`, bag: it, items: []};
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

  function itemRows(parent, items, label) {
    if (!items.length) { parent.append(el('div', 'none', 'empty')); return; }
    for (const it of items) {
      const r = el('div', 'item');
      r.append(el('span', 'k', label(it)));
      const v = el('span', 'v', it.item_name);
      v.append(' ', el('span', 'n', '×' + it.count));
      r.append(v);
      parent.append(r);
    }
  }
  const slotLabel = (it) => EQUIP_SLOTS[it.slot] || `slot ${it.slot}`;
  const bagSlotLabel = (it) => `slot ${it.slot + 1}`;
  const packSlotLabel = (first) => (it) => `slot ${it.slot - first + 1}`;

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
  function containerList(parent, containers) {
    for (const c of containers) {
      parent.append(el('h4', null, `${c.label}: ${c.bag.item_name}`));
      itemRows(parent, c.items, bagSlotLabel);
    }
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

  function renderBody(c) {
    const f = document.createDocumentFragment();

    f.append(el('h3', null, 'Status'));
    f.append(kv('Health', nf.format(c.health)));
    const power = c.power || {};
    for (const key of CLASS_POWERS[c.class] || Object.keys(POWER_LABELS)) {
      if (!(key in power)) continue;
      f.append(kv(POWER_LABELS[key], nf.format(Math.floor(power[key] / (POWER_SCALE[key] || 1)))));
    }
    f.append(kv('Gold', money(c.money)));
    f.append(kv('Played time', duration(c.totaltime)));
    f.append(kv('Last logout', when(c.logout_time)));

    f.append(el('h3', null, 'Position'));
    f.append(kv('Location', placeText(c)));
    f.append(kv('Map coords', mapCoordsText(c) || '—'));
    f.append(kv('World X, Y, Z', worldText(c.position_x, c.position_y, c.position_z)));
    f.append(kv('Facing', `${c.orientation.toFixed(2)} rad`));

    const inv = groupInventory(c.inventory || []);
    f.append(el('h3', null, 'Equipped'));
    itemRows(f, inv.equipped, slotLabel);
    f.append(el('h3', null, 'Bags'));
    f.append(el('h4', null, 'Backpack'));
    itemRows(f, inv.backpack, packSlotLabel(23));
    containerList(f, inv.bags);

    if (inv.bank.length || inv.bankBags.length) {
      const d = collapsible('bank', `Bank (${inv.bank.length + inv.bankBags.reduce((n, b) => n + b.items.length, 0)})`);
      itemRows(d, inv.bank, packSlotLabel(39));
      containerList(d, inv.bankBags);
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

    // Raw ids for now; names need the DBC loader (ROADMAP Phase C).
    const talents = collapsible('talents', `Talents (${(c.talents || []).length})`);
    for (const t of c.talents || []) talents.append(kv(`spec ${t.spec + 1}`, `spell ${t.spell}`));
    const reps = collapsible('reputation', `Reputation (${(c.reputation || []).length})`);
    for (const r of c.reputation || []) reps.append(kv(`faction ${r.faction}`, nf.format(r.standing)));
    const achs = collapsible('achievements', `Achievements (${(c.achievements || []).length})`);
    for (const a of c.achievements || []) achs.append(kv(`#${a.achievement}`, when(a.date)));
    f.append(talents, reps, achs);

    const top = body.scrollTop;
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
  // (list, markers) and the map toolbar, closes it.
  document.addEventListener('click', (e) => {
    if (name && !e.target.closest('#inspect, #list, .marker, .bar')) close();
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
      r.append(el('div', 'when', `#${d.cycle} · ${d.ts ? new Date(d.ts * 1000).toLocaleTimeString() : ''}`),
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
  // Markers and the player list are rebuilt on every tick; tag the new nodes.
  for (const id of ['markers', 'list']) {
    const root = document.getElementById(id);
    if (root) new MutationObserver(() => tag(root)).observe(root, {childList: true});
  }

  loadAgents();
  setInterval(loadAgents, 60000);
  setInterval(sync, 500);
  setInterval(refresh, 3000);
  return {refresh, agents: () => [...agents.values()]};
})();
</script>
"""

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>WoW — Live map</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
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
  .stage { flex:1; position:relative; overflow:auto; }
  .stagewrap { position:relative; margin:auto; }
  #mapimg { display:block; max-width:none; pointer-events:none; }
  .marker { position:absolute; transform:translate(-50%,-50%); }
  .marker .ring { width:13px; height:13px; border-radius:50%; border:2px solid rgba(0,0,0,.65); }
  .marker .lbl { position:absolute; left:16px; top:-2px; white-space:nowrap; font-size:12px;
                 background:rgba(10,13,20,.82); padding:1px 6px; border-radius:5px;
                 border:1px solid var(--line); }
  .trail { position:absolute; inset:0; pointer-events:none; }
  .nogrid { padding:40px; color:var(--dim); text-align:center; }
  .grid-fallback { position:absolute; inset:0;
      background-image:linear-gradient(#232b3d 1px,transparent 1px),
                       linear-gradient(90deg,#232b3d 1px,transparent 1px);
      background-size:64px 64px; }
  footer { padding:8px 14px; border-top:1px solid var(--line); color:var(--dim);
           font-size:12px; background:var(--panel); display:flex; gap:14px; }
  .pill { border:1px solid var(--line); border-radius:999px; padding:2px 9px; }
  .calibration-marker { position:absolute; width:18px; height:18px; transform:translate(-50%,-50%);
                        border:2px solid #f3b84b; border-radius:50%; pointer-events:none;
                        box-shadow:0 0 0 2px rgba(0,0,0,.5); }
  .calibration-marker::before, .calibration-marker::after { content:""; position:absolute; background:#f3b84b; }
  .calibration-marker::before { width:2px; height:28px; left:6px; top:-7px; }
  .calibration-marker::after { height:2px; width:28px; left:-7px; top:6px; }
  .stagewrap.calibrating { cursor:crosshair; }
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
    <button id="fit">Fit</button>
    <button id="tglTrail" title="Show the recent trail (requires history to be enabled)">Trail: off</button>
    <button id="calibrate" title="Click a landmark and drag to line the markers up">Calibrate: off</button>
    <button id="saveCalibration" hidden>Save calibration</button>
    <div class="tabs">
      <button id="follow" title="Center on the selected character">Follow: off</button>
    </div>
  </div>
  <div class="stage" id="stage">
    <div class="stagewrap" id="wrap">
      <div class="grid-fallback" id="fallback"></div>
      <img id="mapimg" alt="">
      <svg class="trail" id="trailsvg"></svg>
      <div id="markers"></div>
    </div>
  </div>
  <footer>
    <span class="pill" id="f-refresh">–</span>
    <span class="pill" id="f-src">source: characters.position_*</span>
    <span class="pill" id="f-note"></span>
  </footer>
</main>
<!-- @inspect-html -->
<!-- @inspect-js -->
<script>window.CHAT_FEED_URL = "__CHAT_FEED_URL__";</script>
<!-- @chat-js -->
<!-- @agent-js -->
<script>
const $ = (id) => document.getElementById(id);
const CLASS_DEFAULT = "#8b93a7";
let areas = [], players = [], selected = null, follow = false, showTrail = false;
let currentArea = null, imgW = 1002, imgH = 668;
let calibrating = false, draftCalibration = null, calibrationReference = null, calibrationDrag = null;

function setImgSize(a) {
  if (!a || !a.has_image) { imgW = 1002; imgH = 668; }
}

async function loadAreas() {
  const r = await fetch('/api/areas');
  const d = await r.json();
  areas = d.areas;
  const sel = $('zone');
  const usable = areas.filter(a => a.has_image);
  const list = usable.length ? usable : areas;
  sel.innerHTML = '';
  for (const a of list) {
    const o = document.createElement('option');
    o.value = a.area_id;
    o.textContent = a.name + (a.has_image ? '' : ' (no image)');
    sel.appendChild(o);
  }
  // default to the last-viewed zone, else a zone with players, else first
  const inuse = new Set((d.in_use || []).map(z => z.zone));
  const lastZone = loadState('lastZone');
  const pick = list.find(a => String(a.area_id) === String(lastZone))
    || list.find(a => inuse.has(a.area_id)) || list[0];
  if (pick) { sel.value = pick.area_id; currentArea = pick; }
  sel.onchange = () => {
    currentArea = list.find(a => String(a.area_id) === sel.value);
    resetCalibration(); draw();
    if (currentArea) saveState('lastZone', currentArea.area_id);
  };
}

function areaFor(zone) { return areas.find(a => a.area_id === zone); }

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
  if (p && p.in_world) {
    const a = areaFor(p.zone);
    if (a && (!currentArea || currentArea.area_id !== a.area_id)) {
      currentArea = a;
      $('zone').value = a.area_id;
      saveState('lastZone', a.area_id);
      draw();
    }
  }
  markListAndMarkersSelected();
}
window.selectCharacter = selectCharacter;

function draw() {
  const a = currentArea;
  const img = $('mapimg');
  if (!a) { $('fallback').style.display = 'block'; img.style.display = 'none'; return; }
  if (a.has_image) {
    if (img.dataset.area !== String(a.area_id)) {
      img.dataset.area = a.area_id;
      img.onload = () => { imgW = img.naturalWidth; imgH = img.naturalHeight; place(); };
      img.src = '/maps/' + a.image;
    }
    img.style.display = 'block';
    $('fallback').style.display = 'none';
  } else {
    img.style.display = 'none';
    $('fallback').style.display = 'block';
    imgW = 1002; imgH = 668;
  }
  place();
}

function calibration() {
  if (calibrating && draftCalibration) return draftCalibration;
  return (currentArea && currentArea.calibration) || {dx: 0, dy: 0};
}

// Keep calibration at the final world->normalised->pixel step: offsets are image pixels.
function px(p) {
  const c = calibration();
  return [p.norm_x * imgW + c.dx, p.norm_y * imgH + c.dy];
}

function resetCalibration() {
  draftCalibration = currentArea ? {...(currentArea.calibration || {dx: 0, dy: 0})} : null;
  calibrationReference = null;
}

function place() {
  const a = currentArea;
  const wrap = $('wrap'), m = $('markers');
  wrap.style.width = imgW + 'px'; wrap.style.height = imgH + 'px';
  m.innerHTML = '';
  if (!a) return;
  const here = players.filter(p => p.in_world && p.zone === a.area_id && p.norm_x !== null);

  const fit = () => {
    const st = $('stage');
    const s = Math.min(st.clientWidth / imgW, st.clientHeight / imgH, 1);
    wrap.style.transform = `scale(${s})`;
    wrap.style.transformOrigin = 'top left';
    wrap.style.margin = '0';
    st.scrollLeft = 0; st.scrollTop = 0;
  };
  fit();

  for (const p of here) {
    const [x, y] = px(p);
    const d = document.createElement('div');
    d.className = 'marker' + (p.name === selected ? ' sel' : '');
    d.dataset.name = p.name;
    d.style.left = x + 'px'; d.style.top = y + 'px';
    const ring = document.createElement('div');
    ring.className = 'ring';
    ring.style.background = ring.style.color = p.class_color;
    const lbl = document.createElement('div');
    lbl.className = 'lbl';
    const lvl = document.createElement('span');
    lvl.style.opacity = '.6';
    lvl.textContent = p.level;
    lbl.append(p.name + ' ', lvl);
    d.append(ring, lbl);
    d.title = `${p.name} — ${p.class_name} ${p.race_name} lvl ${p.level}\n${placeText(p)}`
      + (p.map_coords ? `\n${mapCoordsText(p)}` : '');
    d.onclick = () => {
      if (calibrating) return;
      selectCharacter(p.name);
    };
    m.appendChild(d);
    if (p.name === selected && follow) $('stage').scrollTo({left: x - 300, top: y - 200, behavior:'smooth'});
  }
  if (calibrating && calibrationReference) {
    const c = calibration();
    const ref = document.createElement('div');
    ref.className = 'calibration-marker';
    ref.style.left = (calibrationReference.x + c.dx) + 'px';
    ref.style.top = (calibrationReference.y + c.dy) + 'px';
    ref.title = 'Landmark — drag to adjust';
    m.appendChild(ref);
  }
  wrap.classList.toggle('calibrating', calibrating);
  $('f-note').textContent = `${here.length} in this zone`;
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
    renderList(); place();
  } catch (e) { $('sub').textContent = 'error: ' + e; }
  Inspect.refresh();
}

$('fit').onclick = () => place();
$('tglTrail').onclick = (e) => {
  showTrail = !showTrail;
  e.target.textContent = 'Trail: ' + (showTrail ? 'on' : 'off');
  draw();
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
$('wrap').addEventListener('pointerdown', (e) => {
  if (!calibrating || !currentArea) return;
  const box = $('wrap').getBoundingClientRect();
  const point = {x: (e.clientX - box.left) * imgW / box.width,
                 y: (e.clientY - box.top) * imgH / box.height};
  const c = calibration();
  calibrationReference = {x: point.x - c.dx, y: point.y - c.dy};
  calibrationDrag = {x: point.x, y: point.y, dx: c.dx, dy: c.dy};
  $('wrap').setPointerCapture(e.pointerId);
  place();
});
$('wrap').addEventListener('pointermove', (e) => {
  if (!calibrationDrag || !draftCalibration) return;
  const box = $('wrap').getBoundingClientRect();
  const x = (e.clientX - box.left) * imgW / box.width;
  const y = (e.clientY - box.top) * imgH / box.height;
  draftCalibration.dx = Math.round((calibrationDrag.dx + x - calibrationDrag.x) * 100) / 100;
  draftCalibration.dy = Math.round((calibrationDrag.dy + y - calibrationDrag.y) * 100) / 100;
  place();
});
for (const event of ['pointerup', 'pointercancel']) {
  $('wrap').addEventListener(event, () => { calibrationDrag = null; });
}
$('saveCalibration').onclick = async () => {
  if (!currentArea || !draftCalibration) return;
  const r = await fetch('/api/calibrate', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({area_id: currentArea.area_id, ...draftCalibration})});
  const saved = await r.json();
  if (!r.ok) { $('f-note').textContent = 'save failed: ' + saved.error; return; }
  currentArea.calibration = {dx: saved.dx, dy: saved.dy};
  draftCalibration = {...currentArea.calibration};
  $('f-note').textContent = `calibration saved: ${saved.dx}px, ${saved.dy}px`;
  place();
};
addEventListener('resize', () => place());

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
  place();
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

loadAreas().then(tick);
setInterval(tick, 5000);
</script>
</body></html>
"""
PAGE = (PAGE
        .replace("/* @inspect-css */", INSPECT_CSS)
        .replace("<!-- @inspect-html -->", INSPECT_HTML)
        .replace("<!-- @inspect-js -->", INSPECT_JS)
        .replace("/* @chat-css */", CHAT_CSS)
        .replace("<!-- @chat-html -->", CHAT_HTML)
        .replace("<!-- @chat-js -->", CHAT_JS)
        .replace("/* @agent-css */", AGENT_CSS)
        .replace("<!-- @agent-js -->", AGENT_JS)
        .replace("__CHAT_FEED_URL__", CHAT_FEED_URL))


if __name__ == "__main__":
    log.info("wowmap on :%d (db=%s dbc=%s maps=%s)", LISTEN_PORT, MYSQL["host"], DBC_DIR, MAPS_DIR)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
