# exporters/

Custom Prometheus exporters for the WoW server.

## wow-exporter

Reads game state from the TrinityCore databases and exposes it as Prometheus metrics
on **:9300**. Refreshes on every scrape (custom collector), so empty label series —
a level bucket that emptied out, a guild that disbanded — disappear on their own
instead of lingering as stale data.

| Env | Default | Meaning |
|---|---|---|
| `MYSQL_HOST` | `trinitycore-db` | game DB host |
| `MYSQL_PORT` | `3306` | |
| `MYSQL_USER` | `root` | |
| `MYSQL_PASSWORD` | — | required; compose passes `MYSQL_ROOT_PASSWORD` from the root `.env` |
| `LISTEN_PORT` | `9300` | |
| `TOP_PLAYED` | `20` | how many characters to expose per-character playtime for |

### Metric groups

- **Population** — `wow_players_online`, `wow_accounts_online`, `wow_characters_total`,
  `wow_accounts_total`, `wow_guilds_total`
- **Distributions** — `wow_characters_by_level`, `wow_characters_by_class`,
  `wow_characters_by_race`, `wow_players_by_zone`, `wow_players_by_map`
- **Movement** — `wow_player_position_x/y/z{character,map,zone}` and
  `wow_player_orientation{character,map,zone}` for online characters;
  `wow_player_position_bucket{zone,cell_x,cell_y}` is their current coarse
  per-zone occupancy (20 × 20 cells across the `[-20000, 20000]` world-coordinate
  span).
- **Activity** — `wow_accounts_created{window}`, `wow_accounts_active{window}`,
  `wow_accounts_ever_logged_in`, `wow_playtime_seconds_total`
- **Per character** — `wow_character_playtime_seconds{character}`,
  `wow_character_level{character}`, `wow_character_quests_completed_total{character}`
  (top N by playtime; the last is lifetime completed quests)
- **Guilds** — `wow_guild_members{guild}`
- **Economy** — `wow_money_gold_total`, `wow_money_copper_total`
- **Realm** — `wow_realm_uptime_seconds`, `wow_realm_start_time_seconds`,
  `wow_realm_peak_players`, `wow_realm_peak_players_alltime`
- **Database** — `wow_db_size_bytes{database}`, `wow_mysql_connections`
- **Security** — `wow_accounts_locked`, `wow_accounts_with_failed_logins`,
  `wow_failed_logins_total`, `wow_accounts_muted`
- **Self** — `wow_exporter_up`, `wow_exporter_scrape_duration_seconds`

### Build & run

Built and started by `monitoring/docker-compose.yml` (`build: ./wow-exporter`):

```bash
cd /opt/monitoring && docker compose up -d --build wow-exporter
```

### Gotchas discovered the hard way

1. **`cryptography` is required.** MySQL 8.4 defaults to `caching_sha2_password`.
   PyMySQL only handles it without `cryptography` on the *fast path* (server-side auth
   cache). Once that cache expires, the scrape starts failing with
   `'cryptography' package is required for ... caching_sha2_password`. It looks
   intermittent — it works for a while, then every scrape fails. Keep it in
   `requirements.txt`.
2. **MySQL `SUM()` returns `Decimal`.** `SUM(money) / 10000.0` raises
   `unsupported operand type(s) for /: 'decimal.Decimal' and 'float'`, which aborts the
   collector part-way: you get some metrics and a silent `wow_exporter_up 0`. Cast with
   `float(...)` before arithmetic or before handing the value to a gauge.
3. **`auth.uptime.starttime` is an `int` epoch, not a `TIMESTAMP`.**
   `UNIX_TIMESTAMP(starttime)` returns `0` (MySQL parses the int as `YYYYMMDDHHMMSS`),
   which yields a bogus uptime of ~1.79e9. Use the raw value.
4. **`auth.uptime` accumulates rows.** Only the newest (`ORDER BY starttime DESC LIMIT 1`)
   is the live run — it is the one that gets updated with `uptime`/`maxplayers`. Older
   rows are leftovers from previous `worldserver` invocations and read 0.
5. **A single failing query kills the rest of the scrape.** `q()` catches per-query
   exceptions and returns `[]`, but anything raised in `_collect()` after the first
   `yield` aborts the remaining metric families. Always sanity-check `wow_exporter_up`
   after changing a query.
6. **Movement trails reflect saved positions.** The position metrics work without
   configuration changes, but finer trails need `TC_WORLD__PlayerSaveInterval=5000`
   (the roadmap's save-interval tuning step) instead of the 90-second default.

### Why not read the worldserver console?

For player counts and character data the databases are the right source — the
worldserver console is for actions, not metrics. If you later need live in-memory
state (e.g. exact per-map player distribution the DB does not persist), the console is
reachable via the web UI's socket.io API — see `scripts/wow_console.py`.
