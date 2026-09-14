# wowmap

Small stdlib HTTP service that serves the live player map and JSON data from a
TrinityCore 3.3.5a database. Configure MySQL with `MYSQL_HOST`, `MYSQL_PORT`,
`MYSQL_USER`, and `MYSQL_PASSWORD` (plus the optional paths and port described
in `app.py`).

## Character inspect endpoint

`GET /api/character/<name>` returns a character's saved state. The name is URL
decoded; a missing character returns `404` with `{"error":"character not found"}`.
Inventory, talents, reputation, and achievements are optional best-effort
lookups, so an unavailable detail table produces an empty list without hiding
the character's base state.

```json
{
  "name": "Thrall",
  "level": 80,
  "race": 2,
  "race_name": "Orc",
  "class": 7,
  "class_name": "Shaman",
  "gender": 0,
  "zone": 1637,
  "zone_name": "Orgrimmar",
  "map": 1,
  "position_x": 1502.3,
  "position_y": -4415.2,
  "position_z": 22.1,
  "orientation": 1.2,
  "money_gold": 123.45,
  "totaltime": 86400,
  "logout_time": 1710000000,
  "inventory": [{"slot": 0, "item_name": "Example Item", "count": 1}],
  "talents": [{"spell": 12345, "spec": 0}],
  "reputation": [{"faction": 72, "standing": 42000}],
  "achievements": [{"achievement": 6, "date": 1710000000}]
}
```
