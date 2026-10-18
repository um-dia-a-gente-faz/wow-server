// GET /api/areas is an old-route answer (not in openapi.json), so its shape is typed here:
// only the fields the map page reads.
export interface Area {
  area_id: number | string;
  name: string;
  image: string;
  has_image: boolean;
  continent_view?: boolean;
  calibration: { dx: number; dy: number };
}

export interface AreasResponse {
  areas: Area[];
  continents: Area[];
  in_use: { zone: number; map: number }[];
}
