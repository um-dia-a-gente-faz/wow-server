#!/usr/bin/env python3
"""Prometheus exporter for a TrinityCore 3.3.5a server.

Reads game state from the `auth`, `characters` and `world` databases and exposes it
on :9300/metrics. Refreshes on every scrape (custom collector), so there are no
stale label series when e.g. a level bucket empties out.

Env:
    MYSQL_HOST      default: trinitycore-db
    MYSQL_PORT      default: 3306
    MYSQL_USER      default: root
    MYSQL_PASSWORD  default: trinityroot
    LISTEN_PORT     default: 9300
    TOP_PLAYED      default: 20   (how many characters to expose playtime for)
"""
import logging
import os
import time

import pymysql
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wow-exporter")

MYSQL_HOST = os.environ.get("MYSQL_HOST", "trinitycore-db")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "trinityroot")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9300"))
TOP_PLAYED = int(os.environ.get("TOP_PLAYED", "20"))

# --- Static lookups (DBC values, stable for 3.3.5a) -------------------------------

RACES = {
    1: "Human", 2: "Orc", 3: "Dwarf", 4: "Night Elf", 5: "Undead", 6: "Tauren",
    7: "Gnome", 8: "Troll", 10: "Blood Elf", 11: "Draenei",
}
CLASSES = {
    1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest",
    6: "Death Knight", 7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid",
}
# Partial zone map — the TDB in use has no `areatable`, so unknown ids fall back to
# the raw numeric id (still usable, just not human-readable).
ZONES = {
    1: "Dun Morogh", 12: "Elwynn Forest", 14: "Durotar", 85: "Tirisfal Glades",
    141: "Teldrassil", 215: "Mulgore", 3430: "Eversong Woods", 3524: "Azuremyst Isle",
    1519: "Stormwind City", 1537: "Ironforge", 1637: "Orgrimmar", 1638: "Thunder Bluff",
    1657: "Darnassus", 1497: "Undercity", 3487: "Silvermoon City", 3557: "The Exodar",
    3703: "Shattrath City", 4395: "Dalaran", 4080: "Isle of Quel'Danas",
    1377: "Silithus", 2597: "Alterac Valley", 3277: "Warsong Gulch",
    3358: "Arathi Basin", 3820: "Eye of the Storm", 4384: "Strand of the Ancients",
}


def q(cur, sql, args=None):
    """Run one query; return rows (or [] on failure so one bad query can't kill the scrape)."""
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("query failed: %s -- %s", sql.split("\n")[0][:80], e)
        return []


class WowCollector(Collector):
    def collect(self):
        t0 = time.time()
        conn = None
        try:
            conn = pymysql.connect(
                host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                password=MYSQL_PASSWORD, charset="utf8mb4", autocommit=True,
                cursorclass=pymysql.cursors.Cursor,
            )
            cur = conn.cursor()
            yield from self._collect(cur)
            up = 1
        except Exception as e:  # noqa: BLE001
            log.error("collect failed: %s", e)
            up = 0
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

        yield GaugeMetricFamily("wow_exporter_up", "1 if the last MySQL scrape succeeded", value=up)
        yield GaugeMetricFamily("wow_exporter_scrape_duration_seconds",
                                "Duration of the collector run", value=time.time() - t0)

    def _collect(self, cur):
        # ---------- headline counts ----------
        rows = q(cur, "SELECT COUNT(*) FROM characters.characters WHERE online = 1")
        yield GaugeMetricFamily("wow_players_online", "Characters currently online",
                                value=rows[0][0] if rows else 0)

        rows = q(cur, "SELECT COUNT(*) FROM auth.account WHERE online = 1")
        yield GaugeMetricFamily("wow_accounts_online", "Accounts currently online",
                                value=rows[0][0] if rows else 0)

        rows = q(cur, "SELECT COUNT(*) FROM characters.characters")
        yield GaugeMetricFamily("wow_characters_total", "Total characters ever created",
                                value=rows[0][0] if rows else 0)

        rows = q(cur, "SELECT COUNT(*) FROM auth.account")
        yield GaugeMetricFamily("wow_accounts_total", "Total accounts", value=rows[0][0] if rows else 0)

        rows = q(cur, "SELECT COUNT(*) FROM characters.guild")
        yield GaugeMetricFamily("wow_guilds_total", "Total guilds", value=rows[0][0] if rows else 0)

        # ---------- account health / security ----------
        rows = q(cur, "SELECT COUNT(*) FROM auth.account WHERE locked = 1")
        yield GaugeMetricFamily("wow_accounts_locked", "Accounts locked out", value=rows[0][0] if rows else 0)

        rows = q(cur, "SELECT COUNT(*) FROM auth.account WHERE failed_logins > 0")
        yield GaugeMetricFamily("wow_accounts_with_failed_logins",
                                "Accounts with at least one failed login",
                                value=rows[0][0] if rows else 0)

        rows = q(cur, "SELECT COALESCE(SUM(failed_logins),0) FROM auth.account")
        yield GaugeMetricFamily("wow_failed_logins_total", "Sum of failed login counters",
                                value=float(rows[0][0]) if rows else 0.0)

        rows = q(cur, "SELECT COALESCE(SUM(mutetime > 0),0) FROM auth.account")
        yield GaugeMetricFamily("wow_accounts_muted", "Accounts currently muted",
                                value=rows[0][0] if rows else 0)

        # ---------- activity windows ----------
        for label, interval in (("24h", "24 HOUR"), ("7d", "7 DAY"), ("30d", "30 DAY")):
            rows = q(cur, f"SELECT COUNT(*) FROM auth.account WHERE joindate > NOW() - INTERVAL {interval}")
            y = GaugeMetricFamily("wow_accounts_created",
                                  "Accounts created within the window", labels=["window"])
            y.add_metric([label], rows[0][0] if rows else 0)
            yield y

            rows = q(cur, f"SELECT COUNT(*) FROM auth.account WHERE last_login > NOW() - INTERVAL {interval}")
            y = GaugeMetricFamily("wow_accounts_active",
                                  "Accounts that logged in within the window", labels=["window"])
            y.add_metric([label], rows[0][0] if rows else 0)
            yield y

        rows = q(cur, "SELECT COUNT(*) FROM auth.account WHERE last_login IS NOT NULL")
        yield GaugeMetricFamily("wow_accounts_ever_logged_in",
                                "Accounts that have logged in at least once",
                                value=rows[0][0] if rows else 0)

        # ---------- character distributions ----------
        for metric, col, lookup in (
            ("wow_characters_by_level", "level", None),
            ("wow_characters_by_class", "class", CLASSES),
            ("wow_characters_by_race", "race", RACES),
        ):
            label = col
            y = GaugeMetricFamily(metric, f"Characters grouped by {col}", labels=[label])
            for value, count in q(cur, f"SELECT {col}, COUNT(*) FROM characters.characters GROUP BY {col}"):
                name = lookup.get(value, str(value)) if lookup else str(value)
                y.add_metric([name], count)
            yield y

        # ---------- online players by zone ----------
        y = GaugeMetricFamily("wow_players_by_zone", "Online characters grouped by zone",
                              labels=["zone", "zone_name"])
        for zone, count in q(cur, "SELECT zone, COUNT(*) FROM characters.characters "
                                  "WHERE online = 1 GROUP BY zone"):
            y.add_metric([str(zone), ZONES.get(zone, str(zone))], count)
        yield y

        # ---------- online players by map ----------
        y = GaugeMetricFamily("wow_players_by_map", "Online characters grouped by map id",
                              labels=["map"])
        for mapid, count in q(cur, "SELECT map, COUNT(*) FROM characters.characters "
                                   "WHERE online = 1 GROUP BY map"):
            y.add_metric([str(mapid)], count)
        yield y

        # ---------- online player positions ----------
        # Only ONLINE players, so cardinality stays bounded and series vanish on
        # logout. Raw world coordinates; the map app converts them to pixels with
        # the DBC rects (see tools/wowmap/transform.py).
        pos_rows = q(cur, "SELECT c.name, c.map, c.zone, c.position_x, c.position_y, "
                          "c.position_z, c.orientation, c.instance_id, c.level, c.class "
                          "FROM characters.characters c WHERE c.online = 1")
        for metric, idx in (("wow_player_position_x", 3),
                            ("wow_player_position_y", 4),
                            ("wow_player_position_z", 5),
                            ("wow_player_orientation", 6)):
            y = GaugeMetricFamily(metric, "World position of an online player",
                                  labels=["character", "map", "zone"])
            for r in pos_rows:
                y.add_metric([r[0], str(r[1]), str(r[2])], float(r[idx]))
            yield y

        # Zones do not have a shared, readily available coordinate bounding box in
        # the characters DB. Use the normal WoW world-coordinate span
        # [-20,000, 20,000] on each axis, split into 20 cells (2,000 units each),
        # and clamp outliers to the edge cells. This gives stable coarse per-zone
        # occupancy cells without adding a DBC dependency; it is intentionally a
        # density groundwork metric rather than map-image pixel coordinates.
        cell_size = 2_000.0
        cells_per_axis = 20
        bucket_counts = {}
        for r in pos_rows:
            cell_x = max(0, min(cells_per_axis - 1, int((float(r[3]) + 20_000) / cell_size)))
            cell_y = max(0, min(cells_per_axis - 1, int((float(r[4]) + 20_000) / cell_size)))
            key = (str(r[2]), str(cell_x), str(cell_y))
            bucket_counts[key] = bucket_counts.get(key, 0) + 1

        y = GaugeMetricFamily("wow_player_position_bucket",
                              "Online players in a coarse world-coordinate grid cell",
                              labels=["zone", "cell_x", "cell_y"])
        for labels, count in bucket_counts.items():
            y.add_metric(list(labels), count)
        yield y

        y = GaugeMetricFamily("wow_online_player_info",
                              "1 per online player, carrying map/zone/instance/level/class",
                              labels=["character", "map", "zone", "instance", "level", "class"])
        for r in pos_rows:
            y.add_metric([r[0], str(r[1]), str(r[2]),
                          "world" if not r[7] else str(r[7]), str(r[8]), str(r[9])], 1.0)
        yield y

        rows = q(cur, "SELECT COUNT(*) FROM characters.characters "
                      "WHERE online = 1 AND instance_id != 0")
        yield GaugeMetricFamily("wow_players_in_instances",
                                "Online players inside a dungeon/raid instance",
                                value=rows[0][0] if rows else 0)

        # ---------- playtime / economy ----------
        rows = q(cur, "SELECT COALESCE(SUM(totaltime),0) FROM characters.characters")
        yield GaugeMetricFamily("wow_playtime_seconds_total",
                                "Total accumulated playtime across all characters (seconds)",
                                value=float(rows[0][0]) if rows else 0.0)

        # MySQL SUM() returns Decimal — cast before any arithmetic or gauge use.
        rows = q(cur, "SELECT COALESCE(SUM(money),0) FROM characters.characters")
        copper = float(rows[0][0]) if rows else 0.0
        yield GaugeMetricFamily("wow_money_gold_total", "Total gold held by all characters",
                                value=copper / 10000.0)
        yield GaugeMetricFamily("wow_money_copper_total", "Total copper held by all characters",
                                value=copper)

        # ---------- most-played characters ----------
        y = GaugeMetricFamily("wow_character_playtime_seconds",
                              "Accumulated playtime per character (top N)",
                              labels=["character"])
        for name, total in q(cur, "SELECT name, totaltime FROM characters.characters "
                                  f"ORDER BY totaltime DESC LIMIT {TOP_PLAYED}"):
            y.add_metric([name], total)
        yield y

        y = GaugeMetricFamily("wow_character_level", "Level per character (top N by playtime)",
                              labels=["character"])
        for name, level in q(cur, "SELECT name, level FROM characters.characters "
                                  f"ORDER BY totaltime DESC LIMIT {TOP_PLAYED}"):
            y.add_metric([name], level)
        yield y

        # ---------- guild sizes ----------
        y = GaugeMetricFamily("wow_guild_members", "Members per guild", labels=["guild"])
        for name, count in q(cur, "SELECT g.name, COUNT(gm.guid) FROM characters.guild g "
                                  "LEFT JOIN characters.guild_member gm ON gm.guildid = g.guildid "
                                  "GROUP BY g.guildid, g.name"):
            y.add_metric([name], count)
        yield y

        # ---------- realm uptime (auth.uptime) ----------
        # NOTE: `starttime` is a plain int holding a Unix epoch, NOT a TIMESTAMP —
        # UNIX_TIMESTAMP(starttime) silently returns 0. Use the value as-is.
        # The newest row is the live one (it gets updated with uptime/maxplayers);
        # older rows are leftovers from previous worldserver invocations.
        rows = q(cur, "SELECT starttime, maxplayers FROM auth.uptime "
                      "ORDER BY starttime DESC LIMIT 1")
        if rows:
            start_ts, maxplayers = rows[0]
            now = time.time()
            yield GaugeMetricFamily("wow_realm_start_time_seconds",
                                    "Unix timestamp of the current realm start",
                                    value=float(start_ts or 0))
            yield GaugeMetricFamily("wow_realm_uptime_seconds",
                                    "Seconds since the current realm started",
                                    value=max(0.0, now - float(start_ts or now)))
            yield GaugeMetricFamily("wow_realm_peak_players",
                                    "Peak concurrent players for the current realm run",
                                    value=float(maxplayers or 0))
        rows = q(cur, "SELECT COALESCE(MAX(maxplayers),0) FROM auth.uptime")
        yield GaugeMetricFamily("wow_realm_peak_players_alltime",
                                "Highest concurrent players ever recorded",
                                value=float(rows[0][0] or 0) if rows else 0.0)

        # ---------- database sizes ----------
        y = GaugeMetricFamily("wow_db_size_bytes", "On-disk size of a game database",
                              labels=["database"])
        for schema, size in q(cur, "SELECT table_schema, SUM(data_length + index_length) "
                                   "FROM information_schema.TABLES "
                                   "WHERE table_schema IN ('auth','characters','world') "
                                   "GROUP BY table_schema"):
            y.add_metric([schema], float(size or 0))
        yield y

        # ---------- mysql connections ----------
        rows = q(cur, "SELECT COUNT(*) FROM information_schema.PROCESSLIST")
        yield GaugeMetricFamily("wow_mysql_connections", "Active MySQL connections",
                                value=rows[0][0] if rows else 0)


if __name__ == "__main__":
    REGISTRY.register(WowCollector())
    start_http_server(LISTEN_PORT)
    log.info("wow-exporter listening on :%d (mysql %s:%d)", LISTEN_PORT, MYSQL_HOST, MYSQL_PORT)
    while True:
        time.sleep(3600)
