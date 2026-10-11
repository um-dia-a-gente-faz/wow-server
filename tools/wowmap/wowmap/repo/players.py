"""Online players and the zones they stand in."""
import state

ONLINE_SQL = """
    SELECT c.name, c.level, c.class, c.race, c.map, c.zone,
           c.position_x, c.position_y, c.position_z, c.orientation,
           c.instance_id, c.totaltime, c.online
    FROM characters.characters c
    WHERE c.online = 1
    ORDER BY c.name
"""


def online():
    """Rows (name, level, class, race, map, zone, x, y, z, orientation, instance_id,
    totaltime, online) of the online characters, by name."""
    with state.db() as conn, conn.cursor() as cur:
        cur.execute(ONLINE_SQL)
        return cur.fetchall()


def online_zones():
    """[(zone, map)] pairs that currently have online players."""
    with state.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT zone, map FROM characters.characters WHERE online = 1")
        return cur.fetchall()
