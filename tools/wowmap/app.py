#!/usr/bin/env python3
"""wowmap — live map of online players for a TrinityCore 3.3.5a server.

Serves:
    GET /                     the map page (single file, no build step)
    GET /api/players          online players with world + normalised coords
    GET /api/character/<name> one character's state, inventory and progression
    GET /api/character/<name>/activity?limit=50  recent activity feed (UM-76, activity.py)
    GET /api/character/<name>/kind  agent or human, from the character's account (#174)
    GET /api/areas?map=<id>   zone tiles: rect, name, whether art is available, subzones
                              and `continents`: the continent maps and their zones' boxes
    POST /api/calibrate       save a per-zone pixel offset
    GET /api/fleet            the agent fleet panel's data, from the agent runner (#137, fleet.py)
    POST /api/fleet/agents/<name>/start|stop   forwarded to the runner (token stays server-side)
    POST /api/fleet/agents/<name>/walk[?dry_run=1]   walk an agent to a map point or next to a
                                  player, with its own movement, via the runner (#178, walk.py)
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
    AGENT_RUNNER_URL default ""     the agent runner's base URL (tools/agent-runner); empty = the
                                    fleet panel is read-only
    AGENT_RUNNER_TOKEN default ""   its bearer token. Server-side only: never sent to the browser
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import activity as activity_feed
import routes
import state
from pages import (  # noqa: F401 - the page assets are re-exported for the tests
    ACTIVITY_CSS, ACTIVITY_JS, AGENT_CSS, AGENT_JS, CHAT_CSS, CHAT_HTML, CHAT_JS,
    INSPECT_CSS, INSPECT_HTML, INSPECT_JS, PAGE)
from webio import Request

log = logging.getLogger("wowmap")


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    """Wiring only: build a webio.Request, let routes.dispatch answer, write the reply."""
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

    def _handle(self, method):
        u = urlparse(self.path)
        req = Request(method, u.path, parse_qs(u.query), self.headers, self.rfile.read,
                      self.client_address[0])
        return self._send(*routes.dispatch(req))

    def do_GET(self):  # noqa: N802
        return self._handle("GET")

    def do_POST(self):  # noqa: N802
        return self._handle("POST")


def start_activity():
    """Open the activity store and start its pollers; the feed is optional, so a
    store that can't be opened only disables it."""
    if not state.ACTIVITY_DB:
        return None
    try:
        store = activity_feed.ActivityStore(state.ACTIVITY_DB)
    except Exception as e:  # noqa: BLE001 - OSError, sqlite3.Error, ...
        log.error("activity feed disabled, cannot open %s: %s", state.ACTIVITY_DB, e)
        return None
    state.activity = activity_feed.Activity(
        store, connect=state.db, zone_name=lambda z: state.tables().zone_name(z),
        chat_url=state.ACTIVITY_CHAT_FEED_URL, audit_dir=state.AUDIT_DIR)
    state.activity.start()
    log.info("activity feed: store=%s chat=%s audit=%s", state.ACTIVITY_DB,
             state.ACTIVITY_CHAT_FEED_URL or "off", state.AUDIT_DIR or "off")
    return state.activity


if __name__ == "__main__":
    start_activity()
    log.info("wowmap on :%d (db=%s dbc=%s maps=%s)", state.LISTEN_PORT, state.MYSQL["host"],
             state.DBC_DIR, state.MAPS_DIR)
    threading.Thread(target=state.names, name="load-names", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", state.LISTEN_PORT), Handler).serve_forever()
