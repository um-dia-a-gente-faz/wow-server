#!/usr/bin/env python3
"""wowmap — live map of online players for a TrinityCore 3.3.5a server.

Serves:
    GET /                     the map page (single file, no build step)
    GET /api/players          online players with world + normalised coords
    GET /api/character/<name> one character's state, inventory and progression
    GET /api/areas?map=<id>   zone tiles: rect, name, whether art is available
    GET /maps/<file>          extracted zone map images (static)
    GET /healthz              liveness

Coordinates come from `characters.characters`; the world->normalised transform comes
from the client DBCs (see transform.py). Map art is extracted from the client MPQs by
extract_maps.py and served from MAPS_DIR.

Env:
    MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD   as elsewhere in this repo
    DBC_DIR     default /dbc        (WorldMapArea.dbc, AreaTable.dbc, Map.dbc)
    MAPS_DIR    default /maps       (extracted PNGs)
    LISTEN_PORT default 9400
"""
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import pymysql

from transform import DbcTables

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wowmap")

MYSQL: dict = dict(
    host=os.environ.get("MYSQL_HOST", "trinitycore-db"),
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ.get("MYSQL_USER", "root"),
    password=os.environ.get("MYSQL_PASSWORD", "trinityroot"),
    charset="utf8mb4",
    autocommit=True,
)
DBC_DIR = os.environ.get("DBC_DIR", "/dbc")
MAPS_DIR = os.environ.get("MAPS_DIR", "/maps")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9400"))

# Standard WoW class/race ids — stable for 3.3.5a.
CLASSES = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest",
           6: "Death Knight", 7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}
CLASS_COLORS = {1: "#C79C6E", 2: "#F58CBA", 3: "#ABD473", 4: "#FFF569",
                5: "#FFFFFF", 6: "#C41F3B", 7: "#0070DE", 8: "#69CCF0",
                9: "#9482C9", 11: "#FF7D0A"}
RACES = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "Night Elf", 5: "Undead", 6: "Tauren",
         7: "Gnome", 8: "Troll", 10: "Blood Elf", 11: "Draenei"}

_tables = None


def tables():
    global _tables
    if _tables is None:
        _tables = DbcTables(DBC_DIR)
    return _tables


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
               totaltime, logout_time
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
         money, totaltime, logout_time) = row

        inventory = best_effort(cur, """
            SELECT ci.slot, COALESCE(it.name, CONCAT('Item ', ii.itemEntry)), ii.count
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
        "gender": gender,
        "zone": zone,
        "zone_name": t.zone_name(zone) if zone else "Unknown",
        "map": cmap,
        "position_x": round(float(x), 2),
        "position_y": round(float(y), 2),
        "position_z": round(float(z), 2),
        "orientation": round(float(orient), 3),
        "money_gold": float(money) / 10000.0,
        "totaltime": totaltime,
        "logout_time": logout_time,
        "inventory": [
            {"slot": slot, "item_name": item_name, "count": count}
            for slot, item_name, count in inventory
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
  aside h1 { font-size:15px; margin:0; padding:14px 16px 10px; letter-spacing:.3px; }
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
</style></head>
<body>
<aside>
  <h1>Mapa ao vivo</h1>
  <div class="sub" id="sub">carregando…</div>
  <div class="stats">
    <div class="stat"><b id="s-online">–</b><span>online</span></div>
    <div class="stat"><b id="s-world">–</b><span>no mundo</span></div>
    <div class="stat"><b id="s-inst">–</b><span>em instância</span></div>
  </div>
  <div class="list" id="list"></div>
</aside>
<main>
  <div class="bar">
    <label>Zona <select id="zone"></select></label>
    <button id="fit">Ajustar</button>
    <button id="tglTrail" title="Mostra o rastro recente (requer histórico ligado)">Rastro: off</button>
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
<script>
const $ = (id) => document.getElementById(id);
const CLASS_DEFAULT = "#8b93a7";
let areas = [], players = [], selected = null, follow = false, showTrail = false;
let currentArea = null, imgW = 1002, imgH = 668;

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
  // default to a zone with players, else first
  const inuse = new Set((d.in_use || []).map(z => z.zone));
  const pick = list.find(a => inuse.has(a.area_id)) || list[0];
  if (pick) { sel.value = pick.area_id; currentArea = pick; }
  sel.onchange = () => { currentArea = list.find(a => String(a.area_id) === sel.value); draw(); };
}

function areaFor(zone) { return areas.find(a => a.area_id === zone); }

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

function px(p) { return [p.norm_x * imgW, p.norm_y * imgH]; }

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
    d.className = 'marker';
    d.style.left = x + 'px'; d.style.top = y + 'px';
    d.innerHTML = `<div class="ring" style="background:${p.class_color};color:${p.class_color}"></div>`
                + `<div class="lbl">${p.name} <span style="opacity:.6">${p.level}</span></div>`;
    d.title = `${p.name} — ${p.class_name} ${p.race_name} lvl ${p.level}\n${p.zone_name}`;
    m.appendChild(d);
    if (p.name === selected && follow) $('stage').scrollTo({left: x - 300, top: y - 200, behavior:'smooth'});
  }
  $('f-note').textContent = `${here.length} nesta zona`;
}

function renderList() {
  const l = $('list');
  if (!players.length) { l.innerHTML = '<div class="empty">Ninguém online</div>'; return; }
  l.innerHTML = '';
  for (const p of players) {
    const e = document.createElement('div');
    e.className = 'pl';
    e.innerHTML = `<span class="dot" style="background:${p.class_color};color:${p.class_color}"></span>`
      + `<span class="nm">${p.name}</span>`
      + `<span class="meta">${p.level} ${p.class_name}${p.in_world ? ' · ' + p.zone_name : ' · instância ' + p.instance}</span>`;
    e.onclick = () => {
      selected = p.name;
      if (p.in_world) {
        const a = areaFor(p.zone);
        if (a) { currentArea = a; $('zone').value = a.area_id; draw(); }
      }
    };
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
addEventListener('resize', () => place());
loadAreas().then(tick);
setInterval(tick, 5000);
</script>
</body></html>
"""


if __name__ == "__main__":
    log.info("wowmap on :%d (db=%s dbc=%s maps=%s)", LISTEN_PORT, MYSQL["host"], DBC_DIR, MAPS_DIR)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
