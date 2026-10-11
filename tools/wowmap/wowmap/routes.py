"""The route table: (method, path pattern) -> handler.

Handlers take a webio.Request and return `(status, body[, ctype, cache])`, so they run
without a socket (see tests/test_routes.py). Order matters: the first matching pattern wins.
"""
import re
import time
from urllib.parse import unquote

import agents
import areas
import assets
import calibration
import character
import fogview
import players
import state
from pages import PAGE
from webio import NO_STORE, not_found, full

_CHAR = "/api/character/"


def _char_name(m):
    return unquote(m.group("name"))


def healthz(req, m):
    return 200, {"ok": True}


def api_players(req, m):
    return 200, {"server_time": int(time.time()), "players": players.fetch_players()}


def api_summary(req, m):
    return 200, players.summary()


def char_activity(req, m):
    if state.activity is None:
        return 503, {"error": "activity feed disabled"}
    limit = req.qs1("limit", "50")
    limit = int(limit) if limit.isdigit() else 50
    return 200, state.activity.feed(_char_name(m), limit), "application/json", NO_STORE


def char_explored(req, m):
    name = _char_name(m)
    explored = fogview.fetch_explored(name) if name else None
    if explored is None:
        return 404, {"error": "character not found"}
    return 200, explored, "application/json", NO_STORE


def char_kind(req, m):
    kind = character.character_kind(_char_name(m))
    if kind is None:
        return 404, {"error": "character not found"}
    return 200, kind, "application/json", NO_STORE


def char_detail(req, m):
    name = _char_name(m)
    found = character.fetch_character(name) if name else None
    if found is None:
        return 404, {"error": "character not found"}
    return 200, found


def api_agents(req, m):
    return 200, {"agents": sorted(n for n, _ in agents.AGENT_APIS.values())}


def api_fleet(req, m):
    return 200, agents.fleet_status(), "application/json", NO_STORE


def api_agent_view(req, m):
    name, _, view = unquote(m.group("rest")).partition("/")
    n = req.qs1("n")
    status, body = agents.fetch_agent_view(name, view, int(n) if n and n.isdigit() else None)
    return status, body, "application/json", NO_STORE


def api_areas(req, m):
    mid = req.qs1("map")
    return 200, {
        "areas": areas.fetch_areas(int(mid) if mid else None),
        "continents": areas.fetch_continents(),
        "in_use": areas.zones_in_use(),
    }


def page(req, m):
    return 200, PAGE, "text/html; charset=utf-8"


def fleet_action(req, m):
    name, _, action = unquote(m.group("rest")).partition("/")
    return agents.fleet_action(req, name, action)


def calibrate(req, m):
    return calibration.calibrate(req)


ROUTES = [
    ("GET", r"/healthz", healthz),
    ("GET", r"/api/players", api_players),
    ("GET", _CHAR + r"(?P<name>.*)/activity", char_activity),
    ("GET", _CHAR + r"(?P<name>.*)/explored", char_explored),
    ("GET", _CHAR + r"(?P<name>.*)/kind", char_kind),
    ("GET", _CHAR + r"(?P<name>.*)", char_detail),
    ("GET", r"/api/agents", api_agents),
    ("GET", r"/api/fleet", api_fleet),
    ("GET", r"/api/agent/(?P<rest>.*)", api_agent_view),
    ("GET", r"/api/areas", api_areas),
    ("GET", r"/api/summary", api_summary),
    ("GET", r"/maps/(?P<rest>.*)", lambda req, m: assets.maps(req, m.group("rest"))),
    ("GET", r"/static/(?P<rest>.*)", lambda req, m: assets.static(req, m.group("rest"))),
    ("GET", r"/icons/(?P<rest>.*)", lambda req, m: assets.icons(req, m.group("rest"))),
    ("GET", r"/models/(?P<rest>.*)", lambda req, m: assets.models_file(req, m.group("rest"))),
    ("GET", r"/|/index\.html", page),
    ("POST", r"/api/fleet/agents/(?P<rest>.*)", fleet_action),
    ("POST", r"/api/calibrate", calibrate),
]
_COMPILED = [(method, re.compile(pattern, re.DOTALL), fn) for method, pattern, fn in ROUTES]


def dispatch(req):
    """Request -> (status, body, ctype, cache). A GET handler that raises is a 500."""
    for method, pattern, fn in _COMPILED:
        if method != req.method:
            continue
        m = pattern.fullmatch(req.path)
        if m:
            try:
                return full(fn(req, m))
            except Exception as e:  # noqa: BLE001
                if req.method != "GET":
                    raise
                state.log.exception("request failed")
                return full((500, {"error": str(e)}))
    return full(not_found())
