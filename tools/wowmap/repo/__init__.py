"""Data access: every SQL statement wowmap runs lives in this package.

Functions return plain tuples/lists of rows; services (players.py, character.py, ...)
turn them into the API's JSON. Tests patch `state.db` or these functions.
"""
import state


def best_effort(cur, sql, args, label):
    """Run an optional character-detail query without failing the whole response."""
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        state.log.warning("character %s query failed: %s", label, e)
        return []
