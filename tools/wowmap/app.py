#!/usr/bin/env python3
"""wowmap — live map of online players for a TrinityCore 3.3.5a server.

Serves:
    GET /                     the map page (single file, no build step)
    GET /api/players          online players with world + normalised coords
    GET /api/character/<name> one character's state, inventory and progression
    GET /api/areas?map=<id>   zone tiles: rect, name, whether art is available
    POST /api/calibrate       save a per-zone pixel offset
    GET /maps/<file>          extracted zone map images (static)
    GET /healthz              liveness

Coordinates come from `characters.characters`; the world->normalised transform comes
from the client DBCs (see transform.py). Map art is extracted from the client MPQs by
extract_maps.py and served from MAPS_DIR.

Env:
    MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD   as elsewhere in this repo
    DBC_DIR     default /dbc        (WorldMapArea.dbc, AreaTable.dbc, Map.dbc; names come
                                     from Spell, Talent, TalentTab, Faction, Achievement)
    MAPS_DIR    default /maps       (extracted PNGs)
    LISTEN_PORT default 9400
    CHAT_FEED_URL default ""        (derived from the page's own hostname at :9500)
"""
import json
import logging
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import pymysql

from transform import DbcTables

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
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9400"))
CALIBRATION_FILE = os.environ.get(
    "CALIBRATION_FILE", os.path.join(os.path.dirname(__file__), "calibration.json")
)
MAX_CALIBRATION_PAYLOAD_BYTES = 65536
# Empty means "derive from the page's own hostname at :9500" (see CHAT_JS below);
# set this only when chat-feed isn't reachable on the same host as wowmap.
CHAT_FEED_URL = os.environ.get("CHAT_FEED_URL", "")

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
        # Names from the client DBCs (tools/dbc/names.py); null when an id is unknown.
        "talents": [
            {"spell": spell, "spec": spec, **_talent_names(n, spell)}
            for spell, spec in talents
        ],
        "reputation": [
            {"faction": faction, "standing": standing,
             **n.reputation(faction, standing, race, cls)}
            for faction, standing in reputation
        ],
        "achievements": [
            {"achievement": achievement, "date": date,
             **(n.achievement(achievement) or {"name": None, "points": None})}
            for achievement, date in achievements
        ],
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

            if path.startswith("/api/character/"):
                name = unquote(path[len("/api/character/"):])
                if not name:
                    return self._send(404, {"error": "character not found"})
                character = fetch_character(name)
                if character is None:
                    return self._send(404, {"error": "character not found"})
                return self._send(200, character)

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
  const KIND_LABEL = {say: 'diz', yell: 'grita', channel: 'canal', guild: 'guilda',
    party: 'grupo', raid: 'raide', officer: 'oficial', battleground: 'campo de batalha',
    whisper: 'sussurro', unknown: '?'};
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
    status.textContent = 'conectando ao chat…';
    try {
      source = new EventSource(feedUrl());
    } catch (e) {
      status.textContent = 'chat indisponível: ' + e;
      return;
    }
    source.addEventListener('chat', (e) => {
      try { append(JSON.parse(e.data)); } catch (err) { /* malformed event, skip */ }
    });
    source.onopen = () => { status.hidden = true; backoffMs = 2000; };
    source.onerror = () => {
      status.hidden = false;
      status.textContent = 'chat desconectado, tentando reconectar…';
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
<section class="drawer" id="inspect" aria-hidden="true" aria-label="Inspecionar personagem">
  <div class="drawer-head">
    <div class="dh-main" id="inspect-head"></div>
    <button id="inspect-close" title="Fechar (Esc)" aria-label="Fechar">✕</button>
  </div>
  <div class="drawer-body" id="inspect-body"></div>
  <div class="drawer-foot" id="inspect-status"></div>
</section>
"""

# Everything from the API is rendered with textContent (never innerHTML): character
# and item names come straight from the database.
INSPECT_JS = r"""
<script>
const Inspect = (() => {
  const EQUIP_SLOTS = ['Cabeça', 'Pescoço', 'Ombros', 'Camisa', 'Peito', 'Cintura', 'Pernas',
    'Pés', 'Pulsos', 'Mãos', 'Dedo 1', 'Dedo 2', 'Berloque 1', 'Berloque 2', 'Costas',
    'Mão principal', 'Mão secundária', 'À distância', 'Tabardo'];
  const POWER_LABELS = {mana: 'Mana', rage: 'Raiva', focus: 'Foco', energy: 'Energia',
    happiness: 'Felicidade', rune: 'Runas', runic_power: 'Poder rúnico'};
  // The server keeps rage and runic power in tenths (1000 is shown as 100 in game).
  const POWER_SCALE = {rage: 10, runic_power: 10};
  const CLASS_POWERS = {1: ['rage'], 2: ['mana'], 3: ['mana'], 4: ['energy'], 5: ['mana'],
    6: ['runic_power'], 7: ['mana'], 8: ['mana'], 9: ['mana'], 11: ['mana', 'rage', 'energy']};
  const nf = new Intl.NumberFormat('pt-BR');
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
  function when(ts) { return ts ? new Date(ts * 1000).toLocaleString('pt-BR') : 'nunca'; }

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
        const c = {label: bank ? `Bolsa do banco ${s - 66}` : `Bolsa ${s - 18}`, bag: it, items: []};
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
    if (!items.length) { parent.append(el('div', 'none', 'vazio')); return; }
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
      el('div', 'line', `Nível ${c.level} · ${c.race_name} · ${c.class_name}`),
      el('div', 'line', c.zone_name));
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
  function reputationSection(list) {
    const d = collapsible('reputation', `Reputation (${list.length})`);
    const value = (r) => r.value ?? r.standing;
    for (const r of [...list].sort((a, b) => value(b) - value(a))) {
      d.append(kv(r.faction_name || `faction ${r.faction}`, `${r.tier || ''} · ${nf.format(value(r))}`));
    }
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
    f.append(kv('Vida', nf.format(c.health)));
    const power = c.power || {};
    for (const key of CLASS_POWERS[c.class] || Object.keys(POWER_LABELS)) {
      if (!(key in power)) continue;
      f.append(kv(POWER_LABELS[key], nf.format(Math.floor(power[key] / (POWER_SCALE[key] || 1)))));
    }
    f.append(kv('Ouro', money(c.money)));
    f.append(kv('Tempo de jogo', duration(c.totaltime)));
    f.append(kv('Último logout', when(c.logout_time)));
    f.append(kv('Posição', `${c.map_name} (${c.map}) · ${c.position_x}, ${c.position_y}, ${c.position_z}`));

    const inv = groupInventory(c.inventory || []);
    f.append(el('h3', null, 'Equipado'));
    itemRows(f, inv.equipped, slotLabel);
    f.append(el('h3', null, 'Bolsas'));
    f.append(el('h4', null, 'Mochila'));
    itemRows(f, inv.backpack, packSlotLabel(23));
    containerList(f, inv.bags);

    if (inv.bank.length || inv.bankBags.length) {
      const d = collapsible('bank', `Banco (${inv.bank.length + inv.bankBags.reduce((n, b) => n + b.items.length, 0)})`);
      itemRows(d, inv.bank, packSlotLabel(39));
      containerList(d, inv.bankBags);
      f.append(d);
    }
    for (const [key, title, items] of [['keyring', 'Chaveiro', inv.keyring],
                                       ['currency', 'Moedas', inv.currency],
                                       ['other', 'Outros itens', inv.other]]) {
      if (!items.length) continue;
      const d = collapsible(key, `${title} (${items.length})`);
      itemRows(d, items, (it) => `bag ${it.bag} / ${it.slot}`);
      f.append(d);
    }

    f.append(talentSection(c.talents || []), reputationSection(c.reputation || []),
             achievementSection(c.achievements || []));

    const top = body.scrollTop;
    body.replaceChildren(f);
    body.scrollTop = top;
  }

  function stamp() { status.textContent = 'atualizado ' + new Date().toLocaleTimeString(); }

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
        body.replaceChildren(el('div', 'empty', r.status === 404 ? 'Personagem não encontrado' : 'erro ' + r.status));
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
      if (mine === seq) status.textContent = 'erro: ' + e;
    }
  }

  function open(who) {
    if (who !== name) {
      name = who;
      lastJson = null;
      head.replaceChildren(el('h2', null, who));
      body.replaceChildren(el('div', 'empty', 'carregando…'));
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
    console.warn('hash de inspeção inválido:', e);
  }

  return {open, close, refresh, current: () => name};
})();
</script>
"""

PAGE = r"""<!doctype html>
<html lang="pt-BR"><head>
<meta charset="utf-8"><title>WoW — Mapa ao vivo</title>
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
</style></head>
<body>
<aside id="aside">
  <h1>Mapa ao vivo <button id="collapse" title="Recolher barra lateral" aria-label="Recolher barra lateral">«</button></h1>
  <div class="sub" id="sub">carregando…</div>
  <div class="stats">
    <div class="stat"><b id="s-online">–</b><span>online</span></div>
    <div class="stat"><b id="s-world">–</b><span>no mundo</span></div>
    <div class="stat"><b id="s-inst">–</b><span>em instância</span></div>
  </div>
  <div class="side-tabs">
    <button id="tab-players" class="on">Jogadores</button>
    <button id="tab-chat">Chat</button>
  </div>
  <div class="search-wrap" id="search-wrap">
    <input id="search" type="text" placeholder="Buscar personagem (/)" autocomplete="off">
  </div>
  <div class="list" id="list"></div>
  <!-- @chat-html -->
  <div id="resize-handle" title="Arraste para redimensionar"></div>
</aside>
<button id="expand" hidden title="Mostrar barra lateral" aria-label="Mostrar barra lateral">»</button>
<main>
  <div class="bar">
    <label>Zona <select id="zone"></select></label>
    <button id="fit">Ajustar</button>
    <button id="tglTrail" title="Mostra o rastro recente (requer histórico ligado)">Rastro: off</button>
    <button id="calibrate" title="Clique num ponto de referência e arraste para ajustar os marcadores">Calibrar: off</button>
    <button id="saveCalibration" hidden>Salvar calibração</button>
    <div class="tabs">
      <button id="follow" title="Centraliza no personagem selecionado">Seguir: off</button>
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
    <span class="pill" id="f-src">fonte: characters.position_*</span>
    <span class="pill" id="f-note"></span>
  </footer>
</main>
<!-- @inspect-html -->
<!-- @inspect-js -->
<script>window.CHAT_FEED_URL = "__CHAT_FEED_URL__";</script>
<!-- @chat-js -->
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
    o.textContent = a.name + (a.has_image ? '' : ' (sem imagem)');
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
    d.title = `${p.name} — ${p.class_name} ${p.race_name} lvl ${p.level}\n${p.zone_name}`;
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
    ref.title = 'Ponto de referência — arraste para ajustar';
    m.appendChild(ref);
  }
  wrap.classList.toggle('calibrating', calibrating);
  $('f-note').textContent = `${here.length} nesta zona`;
}

function renderList() {
  const l = $('list');
  const query = ($('search').value || '').trim().toLowerCase();
  const filtered = query ? players.filter(p => p.name.toLowerCase().includes(query)) : players;
  if (!players.length) { l.innerHTML = '<div class="empty">Ninguém online</div>'; return; }
  if (!filtered.length) { l.innerHTML = '<div class="empty">Nenhum personagem corresponde à busca</div>'; return; }
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
    meta.textContent = `${p.level} ${p.class_name}${p.in_world ? ' · ' + p.zone_name : ' · instância ' + p.instance}`;
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
    $('sub').textContent = s.zones.length ? s.zones.join(' · ') : 'sem ninguém no mundo';
    $('f-refresh').textContent = 'atualizado ' + new Date().toLocaleTimeString();
    renderList(); place();
  } catch (e) { $('sub').textContent = 'erro: ' + e; }
  Inspect.refresh();
}

$('fit').onclick = () => place();
$('tglTrail').onclick = (e) => {
  showTrail = !showTrail;
  e.target.textContent = 'Rastro: ' + (showTrail ? 'on' : 'off');
  draw();
};
$('follow').onclick = (e) => {
  follow = !follow;
  e.target.textContent = 'Seguir: ' + (follow ? 'on' : 'off');
  e.target.classList.toggle('on', follow);
  place();
};
$('calibrate').onclick = (e) => {
  calibrating = !calibrating;
  if (calibrating) resetCalibration();
  e.target.textContent = 'Calibrar: ' + (calibrating ? 'on' : 'off');
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
  if (!r.ok) { $('f-note').textContent = 'erro ao salvar: ' + saved.error; return; }
  currentArea.calibration = {dx: saved.dx, dy: saved.dy};
  draftCalibration = {...currentArea.calibration};
  $('f-note').textContent = `calibração salva: ${saved.dx}px, ${saved.dy}px`;
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
        .replace("__CHAT_FEED_URL__", CHAT_FEED_URL))


if __name__ == "__main__":
    log.info("wowmap on :%d (db=%s dbc=%s maps=%s)", LISTEN_PORT, MYSQL["host"], DBC_DIR, MAPS_DIR)
    threading.Thread(target=names, name="load-names", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
