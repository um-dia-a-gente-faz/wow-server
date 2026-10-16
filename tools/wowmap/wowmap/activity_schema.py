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
# (CREATE TABLE, ADD COLUMN, CREATE INDEX) and fill what it added. Step 1 is the schema every file had
# before user_version was set, so a version-0 file with those tables migrates by doing nothing.
# Step 2 (ADR 0020 slice 3, #806): fact_key, the fact of a level or quest row as text, backfilled from
# the detail JSON the rows were written with. The paths are frozen here, not read from feed_events.
MIGRATIONS = (SCHEMA, """
ALTER TABLE events ADD COLUMN fact_key TEXT;
CREATE INDEX IF NOT EXISTS events_fact ON events (character, kind, fact_key);
UPDATE events SET fact_key = json_extract(detail_json, '$.to') WHERE kind = 'level' AND source IN ('db', 'agent');
UPDATE events SET fact_key = json_extract(detail_json, '$.quest') WHERE kind = 'quest' AND source = 'db';
UPDATE events SET fact_key = json_extract(detail_json, '$.quest_id') WHERE kind = 'quest' AND source = 'agent';
""")
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
