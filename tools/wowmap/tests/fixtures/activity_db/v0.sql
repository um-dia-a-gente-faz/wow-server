-- tools/wowmap/wowmap/activity_schema.py, the file before user_version was set (schema version 0),
-- frozen as text (ADR 0018 slice 7, #739). Never edit a shipped fixture: a new schema version gets
-- a new v<N>.sql. The tables below are a copy of SCHEMA as it shipped, not an import of it.
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
-- Rows, inserted in this order (id 1..5). 'Rubens' and 'rubens' differ only in case: the column is NOCASE.
INSERT INTO events (uid, character, t, kind, text, detail_json, source, inferred)
    VALUES ('db:1:100.000:level:', 'Rubens', 100.0, 'level', 'Reached level 11', '{"from": 10, "to": 11}', 'db', 0);
INSERT INTO events (uid, character, t, kind, text, detail_json, source, inferred)
    VALUES ('db:1:101.000:sell:-2589', 'Rubens', 101.0, 'sell', 'Sold Linen Cloth ×2',
            '{"item": 2589, "count": 2, "sell_price": 13, "money": 26}', 'db', 1);
INSERT INTO events (uid, character, t, kind, text, detail_json, source, inferred)
    VALUES ('chat:9001', 'rubens', 102.0, 'chat', 'Said "hi"', '{"kind": "say", "channel": null}', 'chat', 0);
INSERT INTO events (uid, character, t, kind, text, detail_json, source, inferred)
    VALUES ('audit:Thrall:4:103.5', 'Thrall', 103.5, 'move', 'Move to x=1.5 y=2.0 z=0.0',
            '{"action": "move_to", "args": {"x": 1.5, "y": 2.0, "z": 0.0}, "ok": true, "error": null, "cycle": 4}',
            'audit', 0);
INSERT INTO events (uid, character, t, kind, text, detail_json, source, inferred)
    VALUES ('db:1:104.000:money:', 'Rubens', 104.0, 'money', 'Money +26c', '{"delta": 26, "total": 1026}', 'db', 0);
INSERT INTO meta (key, value) VALUES ('chat_last_id', '1700000000123-0');
PRAGMA user_version = 0;
