"""SQLite schema of the activity store and how it is migrated (ADR 0018, slice 6).

The store (`activity.ActivityStore`) calls `migrate` at every open.
"""
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL UNIQUE,
    character   TEXT NOT NULL COLLATE NOCASE,
    t           REAL NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    source      TEXT NOT NULL,
    inferred    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_character_t ON events (character, t);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
# Forward-only: append a step, never edit one that has shipped. A step may only add
# (CREATE TABLE, ADD COLUMN, CREATE INDEX). Step 1 is the schema every file had before
# user_version was set, so a version-0 file with those tables migrates by doing nothing.
MIGRATIONS = (SCHEMA,)
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(f"schema version {version} is newer than {SCHEMA_VERSION}")
    for n in range(version, SCHEMA_VERSION):
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{MIGRATIONS[n]}\nPRAGMA user_version = {n + 1};\nCOMMIT;")
        except BaseException:
            # executescript leaves the transaction open when a statement fails.
            conn.rollback()
            raise
