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
from overlays import load_overlays
from transform import DbcTables, GridAreas

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
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9400"))
# Empty means "derive from the page's own hostname at :9500" (see CHAT_JS in pages.py);
# set this only when chat-feed isn't reachable on the same host as wowmap.
CHAT_FEED_URL = os.environ.get("CHAT_FEED_URL", "")
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
    return pymysql.connect(**MYSQL)


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


def grid_areas():
    global _grid_areas
    if _grid_areas is None:
        _grid_areas = GridAreas(GRID_MAPS_DIR)
    return _grid_areas
