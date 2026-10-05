"""Zone tiles (/api/areas): WorldMapArea rects, art availability, subzones, continents."""
import os

import fog
import state
from calibration import calibrations
from overlays import subzones
from repo import players as players_repo
from transform import MAP_FRAME_H, MAP_FRAME_W


def fetch_areas(map_id=None):
    t = state.tables()
    rows = []
    seen = set()
    for r in t._wm:  # noqa: SLF001 - internal read, kept local to this module
        if r[3] == 0:
            continue
        if map_id is not None and r[1] != map_id:
            continue
        area_id = r[2]
        if not area_id or area_id in seen:      # area 0 rows are the continents
            continue
        seen.add(area_id)
        rect = t.rects.get(area_id)
        if not rect:
            continue
        xmin, xmax, ymin, ymax = rect
        img = f"{area_id}.png"
        rows.append({
            "area_id": area_id, "name": t.zone_name(area_id), "map": r[1],
            "map_name": t.map_name(r[1]),
            "xmin": round(xmin, 1), "xmax": round(xmax, 1),
            "ymin": round(ymin, 1), "ymax": round(ymax, 1),
            "image": img, "has_image": os.path.exists(os.path.join(state.MAPS_DIR, img)),
            # Per-overlay art is extracted, so the page can draw this zone with a
            # character's fog of war (/maps/<area_id>.png?explored=...).
            "fog": fog.has_art(state.MAPS_DIR, area_id, state.overlays().get(r[0], [])),
            "calibration": calibrations.get(str(area_id), {"dx": 0, "dy": 0}),
            # Explored-area rects in image pixels (the 1024x768 canvas), not world
            # coordinates, so they don't go through the marker transform.
            "subzones": subzones(state.overlays().get(r[0], []), t.area_names),
        })
    rows.sort(key=lambda a: a["name"])
    return rows


def fetch_continents():
    """The four continent maps as areas the page can show like a zone (UM-78).

    `zones` are the zones drawn on the continent, each with `box`: its WorldMapArea rect
    in the continent's map-frame pixels [x, y, w, h], or None when the rect does not
    fit the continent (see transform.zone_box). Clicking a box opens the zone.
    """
    t = state.tables()
    out = []
    for map_id in t.continent_rects:
        zones = []
        for area_id in t.rects:
            if not area_id or t.zone_continent(area_id) != map_id:
                continue
            box = t.zone_box(area_id)
            zones.append({
                "area_id": area_id, "name": t.zone_name(area_id),
                "box": box and [round(box[0] * MAP_FRAME_W, 1), round(box[1] * MAP_FRAME_H, 1),
                                round((box[2] - box[0]) * MAP_FRAME_W, 1),
                                round((box[3] - box[1]) * MAP_FRAME_H, 1)],
            })
        zones.sort(key=lambda z: z["name"])
        img = f"continent_{map_id}.png"
        out.append({
            "area_id": f"c{map_id}", "continent_view": True, "map": map_id,
            "name": t.map_display_names.get(map_id) or t.map_name(map_id),
            "map_name": t.map_name(map_id),
            "image": img, "has_image": os.path.exists(os.path.join(state.MAPS_DIR, img)),
            "fog": False, "calibration": {"dx": 0, "dy": 0}, "subzones": [],
            "zones": zones,
        })
    out.sort(key=lambda c: c["name"])
    return out


def zones_in_use():
    """Zones that currently have online players — the useful default filter."""
    return [{"zone": z, "map": m} for z, m in players_repo.online_zones()]
