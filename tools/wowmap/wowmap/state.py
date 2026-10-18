"""Process-wide configuration (env) and the lazily loaded shared resources.

Other modules read these as `state.X` at call time, so tests can patch them.
"""
import logging
import os
import sys
import threading
import time

import pymysql

import item_icons
import models
from overlays import load_overlays
from transform import DbcTables, GridAreas

# The shared DBC reader (tools/dbc) and tools/common are copied to /app/dbc and /app/common in
# the image (ADR 0014); from a checkout, tools/ is two directories up.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from common.env import env_int, env_str  # noqa: E402
from dbc.names import GameNames  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wowmap")

MYSQL: dict = dict(
    host=env_str("MYSQL_HOST", "trinitycore-db"),
    port=env_int("MYSQL_PORT", 3306),
    user=env_str("MYSQL_USER", "root"),
    password=env_str("MYSQL_PASSWORD"),
    charset="utf8mb4",
    autocommit=True,
)
DBC_DIR = env_str("DBC_DIR", "/dbc")
MAPS_DIR = env_str("MAPS_DIR", "/maps")
ICONS_DIR = env_str("ICONS_DIR", "/icons")
MODELS_DIR = env_str("MODELS_DIR", "/models")
GRID_MAPS_DIR = env_str("GRID_MAPS_DIR", "/server-maps")
LISTEN_PORT = env_int("LISTEN_PORT", 9400)
# Empty means "derive from the page's own hostname at :9500" (see CHAT_JS in pages.py);
# set this only when chat-feed isn't reachable on the same host as wowmap.
CHAT_FEED_URL = env_str("CHAT_FEED_URL")
ACTIVITY_DB = os.environ.get("ACTIVITY_DB", "/data/activity.sqlite3")
ACTIVITY_CHAT_FEED_URL = os.environ.get("ACTIVITY_CHAT_FEED_URL", "http://chat-feed:9500")
AUDIT_DIR = os.environ.get("AUDIT_DIR", "/audit")
# Set in app.start_activity so importing the modules starts no threads.
activity = None

_tables = None
_overlays = None
_display_icons = None
_icons_lock = threading.Lock()
_names = None
_names_lock = threading.Lock()
_grid_areas = None


def db():
    """A read-only session with timeouts (#281); MYSQL keys override the timeouts."""
    return pymysql.connect(**{"connect_timeout": 3, "read_timeout": 5, "write_timeout": 5, **MYSQL,
                              "init_command": "SET SESSION TRANSACTION READ ONLY"})


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


def model_url(race, gender):
    """`/models/<race>_<gender>` when extract_models.py has written that model, else None."""
    key = models.model_key(race, gender)
    if key and all(os.path.isfile(os.path.join(MODELS_DIR, key + e)) for e in (".bin", ".png")):
        return "/models/" + key
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


def grid_areas():
    global _grid_areas
    if _grid_areas is None:
        _grid_areas = GridAreas(GRID_MAPS_DIR)
    return _grid_areas
