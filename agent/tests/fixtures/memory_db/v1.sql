-- agent/memory.py, schema version 1 (the first released version), frozen as text (ADR 0018 slice 7, #739).
-- Never edit a shipped fixture: a new schema version gets a new v<N>.sql.
-- The CREATE TABLE below is a copy of MIGRATIONS[0] as it shipped, not an import of it.
CREATE TABLE memory (
        kind TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        expires_at REAL,
        PRIMARY KEY (kind, key));
-- kind, key, value (JSON text), expires_at (epoch seconds, NULL = never). Inserted in this order.
INSERT INTO memory VALUES ('known_target', 'Springpaw Cub', '{"x": 1.5, "guid": 42}', NULL);
INSERT INTO memory VALUES ('goal', 'quest', '"reach level 10"', NULL);
INSERT INTO memory VALUES ('action', 'last_cycle', '17', NULL);
INSERT INTO memory VALUES ('failed_target', 'Razormane Scout', '["move_to", "timeout"]', 4102444800.0);
INSERT INTO memory VALUES ('failed_target', 'Tauren Guard', 'null', NULL);
INSERT INTO memory VALUES ('failed_target', 'Stale Gnoll', '"old"', 1700000000.0);
PRAGMA user_version = 1;
