"""Online players and the zones they stand in."""
import state
from repo.cache import ttl_cache

ONLINE_SQL = """
    SELECT c.name, c.level, c.class, c.race, c.map, c.zone,
           c.position_x, c.position_y, c.position_z, c.orientation,
           c.instance_id, c.totaltime, c.online
    FROM characters.characters c
    WHERE c.online = 1
    ORDER BY c.name
"""

# characters.logout_time: int unsigned epoch (TrinityCore 3.3.5 characters_database.sql).
OFFLINE_SQL = """
    SELECT c.name, c.level, c.class, c.race, c.map, c.zone,
           c.position_x, c.position_y, c.position_z, c.orientation,
           c.instance_id, c.totaltime, c.online, c.logout_time
    FROM characters.characters c
    WHERE c.online = 0 AND c.logout_time >= UNIX_TIMESTAMP() - 7 * 86400
    ORDER BY c.logout_time DESC
    LIMIT 200
"""


@ttl_cache(1.0)
def online():
    """Rows (name, level, class, race, map, zone, x, y, z, orientation, instance_id,
    totaltime, online) of the online characters, by name."""
    with state.db() as conn, conn.cursor() as cur:
        cur.execute(ONLINE_SQL)
        return cur.fetchall()


@ttl_cache(30.0)
def recent_offline():
    """The columns of online() plus logout_time, for characters that logged out in the last
    7 days, newest logout first (at most 200)."""
    with state.db() as conn, conn.cursor() as cur:
        cur.execute(OFFLINE_SQL)
        return cur.fetchall()


@ttl_cache(1.0)
def online_zones():
    """[(zone, map)] pairs that currently have online players."""
    with state.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT zone, map FROM characters.characters WHERE online = 1")
        return cur.fetchall()
