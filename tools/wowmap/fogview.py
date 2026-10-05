"""Fog of war glue: which overlays a character explored, and the composed zone art."""
import fog
import state
from repo import characters as characters_repo

MAX_FOG_OVERLAYS = 128   # ids accepted in /maps/<area>.png?explored=; no zone has that many


def zone_overlays(area_id):
    """A zone's WorldMapOverlay rows by its AreaTable ID. As in areas.fetch_areas(), the
    first WorldMapArea row of an area is the one the page shows."""
    for r in state.tables()._wm:  # noqa: SLF001 - internal read
        if r[3] and r[2] == area_id:
            return state.overlays().get(r[0], [])
    return []


def fetch_explored(name):
    """Which overlays a character has revealed, per zone (see fog.py), or None for
    an unknown character. Zones are keyed by area ID, as in /api/areas."""
    row = characters_repo.explored_zones(name)
    if not row:
        return None
    t = state.tables()
    bits = fog.explored_bits(row[1])
    by_wma = state.overlays()
    zones, seen = {}, set()
    for r in t._wm:  # noqa: SLF001
        if not r[3] or r[2] in seen:
            continue
        seen.add(r[2])
        ids = fog.revealed(by_wma.get(r[0], []), t.area_bits, bits)
        if ids:
            zones[str(r[2])] = ids
    return {"name": row[0], "explored_bits": len(bits), "zones": zones}


def fog_image(area_id, explored):
    """PNG bytes for /maps/<area_id>.png?explored=<overlay ids>, or None.

    Only this zone's own overlays count, so the query can't name arbitrary files.
    """
    try:
        ids = {int(v) for v in explored.split(",") if v}
    except ValueError:
        return None
    if len(ids) > MAX_FOG_OVERLAYS:
        return None
    return fog.compose(state.MAPS_DIR, area_id, zone_overlays(area_id), ids)
