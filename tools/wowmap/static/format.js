// Text helpers shared by the inspect drawer, the player list, marker tooltips and the
// activity feed. Pure functions: tests/js/format.test.js runs them under node.
// Position text shared by the inspect drawer, the player list and marker tooltips.
function placeText(p) {
  return [p.continent_name, p.zone_name, p.subzone_name].filter(Boolean).join(' › ');
}
function mapCoordsText(p) {
  return p.map_coords ? `${p.map_coords.x.toFixed(1)}, ${p.map_coords.y.toFixed(1)}` : '';
}
// Hover text for a marker and a list row: the class, race, level and coordinates
// the row itself leaves out (#175).
function playerTip(p) {
  return `${p.name} — ${p.class_name} ${p.race_name} lvl ${p.level}\n${placeText(p)}`
    + (p.map_coords ? `\n${mapCoordsText(p)}` : '');
}
function worldText(x, y, z) {
  return `${x.toFixed(1)}, ${y.toFixed(1)}, ${z.toFixed(1)}`;
}
// Relative age ("2h ago"), shared by the drawer stats and the activity feed (#176).
function ago(t) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - t));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

