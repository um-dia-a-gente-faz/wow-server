// Where a player's marker goes on the map. Plain JS with JSDoc types so the Node test in
// tests/js/ can load it without a build. Reproduces static/map.js (pxOf, place, ll).
export const MAP_FRAME_W = 1002;
export const MAP_FRAME_H = 668;
export const SHEET_W = 1024;
export const SHEET_H = 768;

/** Image pixel (x, y), y down, as a Leaflet [lat, lng] under L.CRS.Simple: LatLng(-y, x). */
export const ll = (/** @type {number} */ x, /** @type {number} */ y) => /** @type {[number, number]} */ ([-y, x]);

/**
 * Marker position in map-frame pixels, or null when the player is not on this area.
 * A zone applies its calibration offsets at the last step; a continent has none.
 * @param {{in_world: boolean, zone: number | null, continent: string | null,
 *   norm_x: number | null, norm_y: number | null, cont_x: number | null, cont_y: number | null}} p
 * @param {{area_id: number | string, continent_view?: boolean, calibration?: {dx: number, dy: number}}} area
 * @returns {[number, number] | null}
 */
export function markerXY(p, area) {
  if (!p.in_world) return null;
  if (area.continent_view) {
    if (p.continent !== area.area_id || p.cont_x === null || p.cont_y === null) return null;
    return [p.cont_x * MAP_FRAME_W, p.cont_y * MAP_FRAME_H];
  }
  if (p.zone !== area.area_id || p.norm_x === null || p.norm_y === null) return null;
  const c = area.calibration || { dx: 0, dy: 0 };
  return [p.norm_x * MAP_FRAME_W + c.dx, p.norm_y * MAP_FRAME_H + c.dy];
}
