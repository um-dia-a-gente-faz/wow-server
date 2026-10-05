"""One character's rows: account kind, exploration bits and the detail tables."""
import item_tooltip
import state
from repo import best_effort


def account_of(name):
    """(character name, account username) or None for an unknown character."""
    with state.db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT c.name, a.username FROM characters.characters c
                       LEFT JOIN auth.account a ON a.id = c.account
                       WHERE c.name = %s LIMIT 1""", (name,))
        return cur.fetchone()


def explored_zones(name):
    """(character name, exploredZones) or None for an unknown character."""
    with state.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, exploredZones FROM characters.characters WHERE name = %s LIMIT 1",
                    (name,))
        return cur.fetchone()


def item_names(cur, entries):
    """[(entry, name)] of item_template rows, for the item set block of tooltips."""
    return best_effort(
        cur, "SELECT entry, name FROM world.item_template WHERE entry IN (%s)"
        % ", ".join(["%s"] * len(entries)), entries, "item set pieces")


def detail(name, set_context):
    """A character's row plus its optional detail tables, on one connection, or None.

    `set_context(cur, inventory_rows)` builds the item set context (it may query via
    `item_names`). Returns a dict: row (the characters columns), inventory, set_context,
    talents, reputation, stats, achievements."""
    character_sql = """
        SELECT guid, name, level, race, class, gender, zone, map,
               position_x, position_y, position_z, orientation, money,
               totaltime, logout_time, online,
               health, power1, power2, power3, power4, power5, power6, power7
        FROM characters.characters
        WHERE name = %s
        LIMIT 1
    """
    with state.db() as conn, conn.cursor() as cur:
        cur.execute(character_sql, (name,))
        row = cur.fetchone()
        if not row:
            return None
        guid = row[0]

        # `bag` is 0 for the character's own slots, otherwise the item_instance guid
        # of the container holding the item — `item_guid` lets callers resolve it.
        inventory = best_effort(cur, """
            SELECT ci.bag, ci.slot, ci.item, ii.itemEntry,
                   COALESCE(it.name, CONCAT('Item ', ii.itemEntry)), ii.count,
                   it.displayid, it.Quality, ii.flags, ii.durability, """
            + ", ".join(f"it.`{c}`" for c in item_tooltip.COLUMNS) + """,
                   ii.randomPropertyId, ii.enchantments
            FROM characters.character_inventory ci
            JOIN characters.item_instance ii ON ci.item = ii.guid
            LEFT JOIN world.item_template it ON ii.itemEntry = it.entry
            WHERE ci.guid = %s
            ORDER BY ci.bag, ci.slot
        """, (guid,), "inventory")
        context = set_context(cur, inventory)
        talents = best_effort(cur, """
            SELECT spell, talentGroup
            FROM characters.character_talent
            WHERE guid = %s
            ORDER BY talentGroup, spell
        """, (guid,), "talents")
        reputation = best_effort(cur, """
            SELECT faction, standing, flags
            FROM characters.character_reputation
            WHERE guid = %s
            ORDER BY faction
        """, (guid,), "reputation")
        # Max health/power only exist when the worldserver persists them
        # (PlayerSave.Stats.MinLevel > 0; Player::_SaveStats). No row -> empty.
        stats = best_effort(cur, """
            SELECT maxhealth, maxpower1, maxpower2, maxpower3, maxpower4,
                   maxpower5, maxpower6, maxpower7
            FROM characters.character_stats
            WHERE guid = %s
        """, (guid,), "stats")
        achievements = best_effort(cur, """
            SELECT achievement, date
            FROM characters.character_achievement
            WHERE guid = %s
            ORDER BY date DESC, achievement
        """, (guid,), "achievements")
    return {"row": row, "inventory": inventory, "set_context": context, "talents": talents,
            "reputation": reputation, "stats": stats, "achievements": achievements}
