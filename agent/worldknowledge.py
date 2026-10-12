"""Read-only reader for the world bundle (ADR 0016, slice 1): one SQLite file of static game data.

The bundle is built by hand by `tools/worldknowledge/build.py`, which imports SCHEMA_VERSION and
SCHEMA from here so there is one definition. The reader never writes, never creates the file and
never raises: a missing, corrupt or other-version file gives an unavailable reader (every lookup
returns None, meta() is {}) with one warning. A value from the bundle is a hint; the server wins.

Stdlib only; imports nothing from agent/.
"""
import logging
import pathlib
import sqlite3
import threading

log = logging.getLogger("agent.worldknowledge")

SCHEMA_VERSION = 1   # stored as PRAGMA user_version (ADR 0011)
SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tables (name TEXT PRIMARY KEY, tier TEXT NOT NULL CHECK (tier IN ('client', 'server')));
CREATE TABLE spell (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    rank TEXT NOT NULL,
    spell_level INTEGER NOT NULL,
    power_type INTEGER NOT NULL,
    mana_cost INTEGER NOT NULL,
    mana_cost_pct INTEGER NOT NULL,
    cast_time_ms INTEGER NOT NULL,
    range_min_yd REAL NOT NULL,
    range_max_yd REAL NOT NULL,
    range_max_friendly_yd REAL NOT NULL
);
"""

SPELL_COLUMNS = ("id", "name", "rank", "spell_level", "power_type", "mana_cost", "mana_cost_pct",
                 "cast_time_ms", "range_min_yd", "range_max_yd", "range_max_friendly_yd")
_SPELL_SQL = f"SELECT {', '.join(SPELL_COLUMNS)} FROM spell WHERE id = ?"


class WorldKnowledge:
    def __init__(self, path: str | None, allow_server_tier: bool = False):
        # ponytail: no server-tier table exists yet, so the flag is only recorded (ADR 0016 point 4).
        self.allow_server_tier = allow_server_tier
        self.available = False
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._meta: dict = {}
        if path is None:
            log.warning("world bundle unavailable: no path set")
            return
        try:
            # as_uri() percent-encodes the path; mode=ro never creates a missing file.
            conn = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True,
                                   check_same_thread=False)
        except (sqlite3.Error, OSError, ValueError) as e:
            log.warning("world bundle unavailable: %s: %s", path, e)
            return
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise ValueError(f"user_version {version}, expected {SCHEMA_VERSION}")
            self._meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        except (sqlite3.Error, ValueError) as e:
            conn.close()
            log.warning("world bundle unavailable: %s: %s", path, e)
            return
        self._conn = conn
        self.available = True

    def meta(self) -> dict:
        return dict(self._meta)

    def spell(self, spell_id: int) -> dict | None:
        with self._lock:
            if self._conn is None:
                return None
            try:
                row = self._conn.execute(_SPELL_SQL, (spell_id,)).fetchone()
            except (sqlite3.Error, OverflowError):
                return None
        return dict(zip(SPELL_COLUMNS, row, strict=True)) if row else None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self.available = False
