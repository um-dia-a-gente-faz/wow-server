const $ = (id) => document.getElementById(id);
const CLASS_DEFAULT = "#8b93a7";
let areas = [], players = [], selected = null, follow = false, showTrail = false;
let showLabels = false;
let currentArea = null;
let calibrating = false, draftCalibration = null, calibrationReference = null, calibrationDrag = null;
// Fog of war: with a character open in the inspect drawer the zone art shows only
// what that character has explored. `fog` is its /api/character/<name>/explored
// answer, null with nobody open (then the art is the fully explored zone).
let fogOn = true, fogFor = null, fog = null;

// ---- the map: Leaflet with L.CRS.Simple. Map units are zone-art pixels with y
// pointing down, so image point (x, y) is LatLng(-y, x) and nothing else on the page
// has to know about Leaflet's axes. Normalised coords are fractions of the game's
// 1002x668 map frame, which the 1024x768 tile sheet overflows (transform.MAP_FRAME_W/H,
// #109), so positions scale by the frame and the art is laid over the whole sheet.
const MAP_FRAME_W = 1002, MAP_FRAME_H = 668, SHEET_W = 1024, SHEET_H = 768;
const ll = (x, y) => L.latLng(-y, x);
const FRAME = L.latLngBounds(ll(0, MAP_FRAME_H), ll(MAP_FRAME_W, 0));
const ZOOM_OUT = 1, ZOOM_IN = 3;          // zoom levels around "fit zone": half size to 8x
const map = L.map('map', {
  crs: L.CRS.Simple, attributionControl: false, zoomSnap: 0, zoomDelta: 1,
  maxBounds: FRAME.pad(0.5), maxBoundsViscosity: 0.8,
});
map.setView(FRAME.getCenter(), 0);
map.createPane('art').style.zIndex = 300;        // under trails (overlayPane, 400)
map.createPane('labels').style.zIndex = 550;     // under player markers (markerPane, 600)
const trailLayer = L.layerGroup().addTo(map);
const labelLayer = L.layerGroup().addTo(map);
const playerLayer = L.layerGroup().addTo(map);
const markers = new Map();                       // character name -> L.marker
let art = null, artKey = '', shownZone = null, lastFit = null, calibrationPin = null;
let continents = [], zoneGlow = null;

// The zoom at which the zone's map frame fills the stage. getBoundsZoom clamps to the
// current zoom range, which belongs to the previous stage size, so lift it first.
function fitZoom() {
  map.setMinZoom(-Infinity); map.setMaxZoom(Infinity);
  return map.getBoundsZoom(FRAME);
}
// Fit the zone's map frame in the stage and allow zooming around that level.
function fitZone() {
  // A zoom animation in flight sets its own view when it ends, undoing the fit, and
  // Leaflet has no public way to cancel one (vendored 1.9.4), so fit after it.
  if (map._animatingZoom) { map.once('zoomend', fitZone); return; }
  map.invalidateSize({pan: false});
  const z = fitZoom();
  map.fitBounds(FRAME, {animate: false});
  map.setMinZoom(z - ZOOM_OUT); map.setMaxZoom(z + ZOOM_IN);
  lastFit = {zoom: map.getZoom(), center: map.getCenter()};
}
// True until the user zooms or pans away from the fitted view.
function atFit() {
  if (!lastFit) return true;
  const c = map.getCenter();
  return Math.abs(map.getZoom() - lastFit.zoom) < 1e-6
    && Math.abs(c.lat - lastFit.center.lat) < 0.5 && Math.abs(c.lng - lastFit.center.lng) < 0.5;
}
// The stage changed size (window, sidebar, inspect drawer). A fitted view stays
// fitted; a view the user chose is kept, with the zoom range moved to the new fit.
function onResize() {
  if (atFit()) return fitZone();
  map.invalidateSize({pan: false});
  const z = fitZoom();
  map.setMinZoom(z - ZOOM_OUT); map.setMaxZoom(z + ZOOM_IN);
}
// A DOM pointer event -> zone-art pixels.
function eventPoint(e) {
  const p = map.mouseEventToLatLng(e);
  return {x: p.lng, y: -p.lat};
}

async function loadAreas() {
  const r = await fetch('/api/areas');
  const d = await r.json();
  // Continents are areas too (area_id "c<map>", continent_view): the same stage shows
  // their art, with their zones' boxes as the hover/label/click targets.
  continents = (d.continents || []).filter(c => c.has_image);
  for (const c of continents) {
    c.subzones = c.zones.filter(z => z.box).map(z => ({
      name: z.name, area_id: z.area_id, hit: z.box,
      label: [z.box[0] + z.box[2] / 2, z.box[1] + z.box[3] / 2]}));
  }
  areas = continents.concat(d.areas);
  const sel = $('zone');
  const usable = areas.filter(a => a.has_image);
  const list = usable.length ? usable : areas;
  sel.innerHTML = '';
  const groups = [['Continents', list.filter(a => a.continent_view)],
                  ['Zones', list.filter(a => !a.continent_view)]];
  for (const [label, members] of groups) {
    if (!members.length) continue;
    const g = document.createElement('optgroup');
    g.label = label;
    for (const a of members) {
      const o = document.createElement('option');
      o.value = a.area_id;
      o.textContent = a.name + (a.has_image ? '' : ' (no image)');
      g.appendChild(o);
    }
    sel.appendChild(g);
  }
  // default to the last-viewed zone, else a zone with players, else first
  const inuse = new Set((d.in_use || []).map(z => z.zone));
  const lastZone = loadState('lastZone');
  const pick = list.find(a => String(a.area_id) === String(lastZone))
    || list.find(a => inuse.has(a.area_id)) || list[0];
  if (pick) { sel.value = pick.area_id; currentArea = pick; }
  sel.onchange = () => showArea(list.find(a => String(a.area_id) === sel.value));
}

function areaFor(zone) { return areas.find(a => a.area_id === zone); }
// The continent map a zone is drawn on, if its art is there.
function continentOf(a) {
  if (!a || a.continent_view) return null;
  return continents.find(c => c.zones.some(z => z.area_id === a.area_id)) || null;
}
// Switch the stage to a zone or a continent: like the game's world map, left-click a
// zone on the continent to open it and right-click (or "Continent") to go back out.
function showArea(a) {
  if (!a) return;
  // Calibration is per zone: leave it before a continent disables its button.
  if (a.continent_view && calibrating) $('calibrate').click();
  currentArea = a;
  $('zone').value = a.area_id;
  saveState('lastZone', a.area_id);
  $('subzone-hover').hidden = true;
  resetCalibration();
  draw();
}
// With Follow on, the stage goes where the selected character goes: stay on a
// continent that still holds it, else open its zone.
function followAcrossZones() {
  if (!follow || !selected || calibrating || !currentArea) return;
  const p = players.find(pl => pl.name === selected);
  if (!p || !p.in_world) return;
  if (currentArea.continent_view && p.continent === currentArea.area_id) return;
  const a = areaFor(p.zone);
  if (a && a !== currentArea) showArea(a);
}
function zoomOut() {
  const c = continentOf(currentArea);
  if (c && !calibrating) showArea(c);
}

// Toggle-only highlighting, safe to call from inside a click handler: rebuilding
// #list or #markers here (as renderList()/place() do) would detach the very
// element the click originated on before the event finishes bubbling, which
// makes Inspect's click-away handler misfire and close the drawer it just opened.
function markListAndMarkersSelected() {
  for (const e of document.querySelectorAll('.pl')) e.classList.toggle('sel', e.dataset.name === selected);
  for (const e of document.querySelectorAll('.marker')) e.classList.toggle('sel', e.dataset.name === selected);
}

// Unified selection: called from the player list, a map marker, or a chat sender —
// highlights the character everywhere and pans the map to their zone.
function selectCharacter(name) {
  if (Walk.pickPlayer(name)) return;              // "walk to a player" is armed (#178)
  selected = name;
  if (!calibrating) Inspect.open(name);
  const p = players.find(pl => pl.name === name);
  const onContinent = currentArea && currentArea.continent_view && p
    && p.continent === currentArea.area_id;
  if (p && p.in_world && !onContinent) {
    const a = areaFor(p.zone);
    if (a && (!currentArea || currentArea.area_id !== a.area_id)) showArea(a);
  }
  markListAndMarkersSelected();
}
window.selectCharacter = selectCharacter;

// The zone art to show: fully explored, or as the open character has explored it
// (the base parchment plus that character's revealed overlays, composed by the server).
function artUrl(a) {
  if (!fog || !a.fog) return '/maps/' + a.image;
  const ids = fog.zones[a.area_id] || [];
  return ids.length ? `/maps/${a.area_id}.png?explored=${ids.join(',')}` : `/maps/${a.area_id}_base.png`;
}

async function loadFog() {
  const name = fogFor;
  let next = null;
  if (name) {
    try {
      const r = await fetch(`/api/character/${encodeURIComponent(name)}/explored`);
      if (r.ok) next = await r.json();
    } catch (e) { /* keep the fully explored art */ }
    if (name !== fogFor) return;               // the selection changed while loading
  }
  const changed = JSON.stringify(next && next.zones) !== JSON.stringify(fog && fog.zones);
  fog = next;
  if (changed) draw();
}
// The drawer opens and closes from several places (list, marker, chat, Esc, click
// away), so follow Inspect.current() instead of hooking each of them.
function syncFog() {
  const name = fogOn ? Inspect.current() : null;
  if (name === fogFor) return;
  fogFor = name;
  loadFog();
}

// Zone art and view. The view is only re-fitted when the zone changes, so the 5 s
// refresh (tick -> place) never resets the user's zoom or pan.
function draw() {
  const a = currentArea;
  const key = a && a.has_image ? artUrl(a) : '';
  if (key !== artKey) {
    if (art) art.remove();
    art = null;
    artKey = key;
    if (key) {
      art = L.imageOverlay(key, [ll(0, SHEET_H), ll(SHEET_W, 0)], {pane: 'art'}).addTo(map);
      const layer = art;
      // The extractor writes the whole 1024x768 sheet; follow the file if that changes.
      layer.on('load', () => {
        const im = layer.getElement();
        if (im.naturalWidth) layer.setBounds([ll(0, im.naturalHeight), ll(im.naturalWidth, 0)]);
      });
    }
  }
  $('map').classList.toggle('grid-fallback', !key);
  $('map').classList.toggle('continent', !!(a && a.continent_view));
  $('toContinent').disabled = !continentOf(a);
  $('calibrate').disabled = !!(a && a.continent_view);   // offsets are per zone
  if (zoneGlow) { zoneGlow.remove(); zoneGlow = null; }
  const zone = a ? a.area_id : null;
  if (zone !== shownZone) { shownZone = zone; fitZone(); }
  place();
}

function calibration() {
  if (calibrating && draftCalibration) return draftCalibration;
  return (currentArea && currentArea.calibration) || {dx: 0, dy: 0};
}

// Keep calibration at the final world->normalised->pixel step: offsets are zone-art
// pixels, which are also the map's units, so they hold at every zoom level.
function pxOf(nx, ny) {
  const c = calibration();
  return [nx * MAP_FRAME_W + c.dx, ny * MAP_FRAME_H + c.dy];
}
function px(p) { return pxOf(p.norm_x, p.norm_y); }

function resetCalibration() {
  draftCalibration = currentArea ? {...(currentArea.calibration || {dx: 0, dy: 0})} : null;
  calibrationReference = null;
}

// Fill a marker's icon. The children are only rebuilt when what they show changes:
// replacing them during a click would detach the click target (see
// markListAndMarkersSelected).
function fillMarker(e, p) {
  const shown = `${p.name}|${p.level}|${p.class_color}`;
  if (e.dataset.shown !== shown) {
    e.dataset.shown = shown;
    e.dataset.name = p.name;
    const ring = document.createElement('div');
    ring.className = 'ring';
    ring.style.background = ring.style.color = p.class_color;
    const lbl = document.createElement('div');
    lbl.className = 'lbl';
    const lvl = document.createElement('span');
    lvl.style.opacity = '.6';
    lvl.textContent = p.level;
    lbl.append(p.name + ' ', lvl);
    e.replaceChildren(ring, lbl);
  }
  e.classList.toggle('sel', p.name === selected);
  e.title = playerTip(p);
}

function place() {
  const a = currentArea;
  const onContinent = !!(a && a.continent_view);
  const here = !a ? [] : onContinent
    ? players.filter(p => p.in_world && p.continent === a.area_id)
    : players.filter(p => p.in_world && p.zone === a.area_id && p.norm_x !== null);
  const seen = new Set();
  for (const p of here) {
    // A continent has no calibration: it is one transform for the whole map.
    const [x, y] = onContinent ? [p.cont_x * MAP_FRAME_W, p.cont_y * MAP_FRAME_H] : px(p);
    seen.add(p.name);
    let m = markers.get(p.name);
    if (!m) {
      m = L.marker(ll(x, y), {icon: L.divIcon({className: 'marker', iconSize: null, html: ''}),
                              keyboard: false, riseOnHover: true}).addTo(playerLayer);
      m.on('click', () => { if (!calibrating) selectCharacter(p.name); });
      markers.set(p.name, m);
    } else {
      m.setLatLng(ll(x, y));
    }
    fillMarker(m.getElement(), p);
    if (p.name === selected && follow && !calibrating) map.panTo(ll(x, y));
  }
  for (const [name, m] of markers) {
    if (!seen.has(name)) { m.remove(); markers.delete(name); }
  }
  if (calibrationPin) { calibrationPin.remove(); calibrationPin = null; }
  if (calibrating && calibrationReference) {
    const c = calibration();
    calibrationPin = L.marker(ll(calibrationReference.x + c.dx, calibrationReference.y + c.dy), {
      icon: L.divIcon({className: 'calibration-marker', iconSize: [18, 18], html: ''}),
      interactive: false, keyboard: false, title: 'Landmark — drag to adjust'}).addTo(map);
  }
  $('map').classList.toggle('calibrating', calibrating);
  if (calibrating) map.dragging.disable(); else map.dragging.enable();
  AgentMind.tag($('map'));
  drawLabels();
  drawTrails();
  $('f-note').textContent = `${here.length} ${onContinent ? 'on this continent' : 'in this zone'}`;
}

// ---- trails: where each character has been since this page was opened. The server
// keeps no position history, so points are collected from the 5 s refresh, as
// normalised coords (calibration is applied when drawing).
const TRAIL_MAX_POINTS = 720;                    // an hour at one point per refresh
const trails = new Map();                        // name -> {zone, color, pts: [[nx, ny], ...]}
function recordTrails() {
  const online = new Set();
  for (const p of players) {
    online.add(p.name);
    if (!p.in_world || p.norm_x === null) continue;
    let t = trails.get(p.name);
    if (!t || t.zone !== p.zone) { t = {zone: p.zone, pts: []}; trails.set(p.name, t); }
    t.color = p.class_color;
    const last = t.pts[t.pts.length - 1];
    if (last && last[0] === p.norm_x && last[1] === p.norm_y) continue;
    t.pts.push([p.norm_x, p.norm_y]);
    if (t.pts.length > TRAIL_MAX_POINTS) t.pts.shift();
  }
  for (const name of trails.keys()) if (!online.has(name)) trails.delete(name);
}
function drawTrails() {
  trailLayer.clearLayers();
  if (!showTrail || !currentArea) return;
  for (const t of trails.values()) {
    if (t.zone !== currentArea.area_id || t.pts.length < 2) continue;
    L.polyline(t.pts.map(([nx, ny]) => ll(...pxOf(nx, ny))),
               {color: t.color || CLASS_DEFAULT, weight: 2, opacity: 0.85, interactive: false})
      .addTo(trailLayer);
  }
}

// ---- subzones: WorldMapOverlay hit rects, already in image pixels (see /api/areas)
function drawLabels() {
  labelLayer.clearLayers();
  const a = currentArea;
  if (!showLabels || !a || !a.has_image) return;
  for (const sz of a.subzones || []) {
    const text = document.createElement('span');
    text.textContent = sz.name;
    L.marker(ll(sz.label[0], sz.label[1]), {
      icon: L.divIcon({className: 'subzone-label', iconSize: null, html: text}),
      pane: 'labels', interactive: false, keyboard: false}).addTo(labelLayer);
  }
}

// The smallest rect under the point wins: Ruins of Silvermoon sits inside Silvermoon City.
function subzoneAt(x, y) {
  if (currentArea && currentArea.continent_view) return zoneAt(x, y);
  let best = null, bestArea = Infinity;
  for (const sz of (currentArea && currentArea.subzones) || []) {
    const r = sz.hit || sz.art;
    if (!r || x < r[0] || y < r[1] || x > r[0] + r[2] || y > r[1] + r[3]) continue;
    if (r[2] * r[3] < bestArea) { best = sz; bestArea = r[2] * r[3]; }
  }
  return best;
}

// On a continent the zone rects are bounding boxes that overlap a lot (half of
// Kalimdor is under two or more), so "the box under the pointer" is often the wrong
// zone. A zone's explored-area rects (its subzones) cover its land much more tightly:
// prefer the zones whose land is under the pointer, then the nearest box centre.
// Cities have no subzones and count as land all over their small box.
function onZoneLand(z, x, y) {
  const a = areaFor(z.area_id);
  if (!a || !a.subzones || !a.subzones.length) return true;
  const [bx, by, bw, bh] = z.hit;
  const zx = (x - bx) / bw * MAP_FRAME_W, zy = (y - by) / bh * MAP_FRAME_H;
  return a.subzones.some(sz => {
    const r = sz.hit || sz.art;
    return r && zx >= r[0] && zy >= r[1] && zx <= r[0] + r[2] && zy <= r[1] + r[3];
  });
}
function zoneAt(x, y) {
  const under = currentArea.subzones.filter(z => {
    const [bx, by, bw, bh] = z.hit;
    return x >= bx && y >= by && x <= bx + bw && y <= by + bh;
  });
  const land = under.filter(z => onZoneLand(z, x, y));
  let best = null, bestD = Infinity;
  for (const z of (land.length ? land : under)) {
    const [bx, by, bw, bh] = z.hit;
    const d = Math.hypot(x - bx - bw / 2, y - by - bh / 2);
    if (d < bestD) { best = z; bestD = d; }
  }
  return best;
}

function renderList() {
  const l = $('list');
  const query = ($('search').value || '').trim().toLowerCase();
  const filtered = query ? players.filter(p => p.name.toLowerCase().includes(query)) : players;
  if (!players.length) { l.innerHTML = '<div class="empty">Nobody online</div>'; return; }
  if (!filtered.length) { l.innerHTML = '<div class="empty">No character matches the search</div>'; return; }
  l.innerHTML = '';
  for (const p of filtered) {
    const e = document.createElement('div');
    e.className = 'pl' + (p.name === Inspect.current() ? ' sel' : '');
    e.dataset.name = p.name;
    e.title = playerTip(p);
    // #175: class, race and coords left the visible row; the label keeps them for screen readers.
    e.setAttribute('role', 'button');
    e.setAttribute('aria-label', e.title.replace(/\n/g, ', '));
    e.tabIndex = 0;
    e.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); selectCharacter(p.name); } };
    const dot = document.createElement('span');
    dot.className = 'dot';
    dot.style.background = p.class_color;
    dot.style.boxShadow = `0 0 6px ${p.class_color}`;
    // #175: >= 4.5:1 numeral; only DK red and Shaman blue are too dark for black.
    dot.style.color = ['#C41F3B', '#0070DE'].includes(p.class_color) ? '#ffffff' : '#000000';
    dot.textContent = p.level;
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = p.name;
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = p.in_world ? p.zone_name : p.continent_name + ' (instance)';
    e.append(dot, nm, meta);
    e.onclick = () => selectCharacter(p.name);
    l.appendChild(e);
  }
}

async function tick() {
  try {
    const [pr, sr] = await Promise.all([fetch('/api/players'), fetch('/api/summary')]);
    players = (await pr.json()).players;
    const s = await sr.json();
    $('s-online').textContent = s.online;
    $('s-world').textContent = s.in_world;
    $('s-inst').textContent = s.in_instance;
    $('sub').textContent = s.zones.length ? s.zones.join(' · ') : 'nobody in the world';
    $('f-refresh').textContent = 'updated ' + new Date().toLocaleTimeString();
    recordTrails();
    followAcrossZones();
    renderList(); place();
  } catch (e) { $('sub').textContent = 'error: ' + e; }
  Inspect.refresh();
  if (fogFor) loadFog();                       // the character may have explored more
}

$('fit').onclick = () => fitZone();
$('tglTrail').onclick = (e) => {
  showTrail = !showTrail;
  e.target.textContent = 'Trail: ' + (showTrail ? 'on' : 'off');
  e.target.classList.toggle('on', showTrail);
  drawTrails();
};
$('follow').onclick = (e) => {
  follow = !follow;
  e.target.textContent = 'Follow: ' + (follow ? 'on' : 'off');
  e.target.classList.toggle('on', follow);
  place();
};
$('calibrate').onclick = (e) => {
  calibrating = !calibrating;
  if (calibrating) resetCalibration();
  e.target.textContent = 'Calibrate: ' + (calibrating ? 'on' : 'off');
  e.target.classList.toggle('on', calibrating);
  $('saveCalibration').hidden = !calibrating;
  place();
};
// Calibration: press on a landmark and drag it to where it should be. Dragging the
// map is off meanwhile (see place()); wheel and pinch zoom still work, so the
// offset can be set zoomed in.
$('map').addEventListener('pointerdown', (e) => {
  if (!calibrating || !currentArea || e.target.closest('.leaflet-control')) return;
  const point = eventPoint(e);
  const c = calibration();
  calibrationReference = {x: point.x - c.dx, y: point.y - c.dy};
  calibrationDrag = {x: point.x, y: point.y, dx: c.dx, dy: c.dy};
  $('map').setPointerCapture(e.pointerId);
  place();
});
$('map').addEventListener('pointermove', (e) => {
  if (!calibrationDrag || !draftCalibration) return;
  const {x, y} = eventPoint(e);
  draftCalibration.dx = Math.round((calibrationDrag.dx + x - calibrationDrag.x) * 100) / 100;
  draftCalibration.dy = Math.round((calibrationDrag.dy + y - calibrationDrag.y) * 100) / 100;
  place();
});
$('map').addEventListener('pointermove', (e) => {
  const hover = $('subzone-hover');
  if (calibrating || !currentArea || !currentArea.has_image) { hover.hidden = true; return; }
  const {x, y} = eventPoint(e);
  const sz = subzoneAt(x, y);
  hover.hidden = !sz;
  if (currentArea.continent_view) {
    if (zoneGlow) { zoneGlow.remove(); zoneGlow = null; }
    if (sz) {
      const [bx, by, bw, bh] = sz.hit;
      zoneGlow = L.rectangle([ll(bx, by + bh), ll(bx + bw, by)],
                             {className: 'zone-glow', interactive: false}).addTo(map);
    }
  }
  if (!sz) return;
  hover.textContent = sz.name;
  const at = map.mouseEventToContainerPoint(e);
  hover.style.left = at.x + 'px'; hover.style.top = at.y + 'px';
});
$('map').addEventListener('pointerleave', () => {
  $('subzone-hover').hidden = true;
  if (zoneGlow) { zoneGlow.remove(); zoneGlow = null; }
});
$('tglLabels').onclick = (e) => {
  showLabels = !showLabels;
  e.target.textContent = 'Labels: ' + (showLabels ? 'on' : 'off');
  e.target.classList.toggle('on', showLabels);
  drawLabels();
};
$('tglFog').onclick = (e) => {
  fogOn = !fogOn;
  e.target.textContent = 'Fog of war: ' + (fogOn ? 'on' : 'off');
  e.target.classList.toggle('on', fogOn);
  syncFog();
};
setInterval(syncFog, 300);
for (const event of ['pointerup', 'pointercancel']) {
  $('map').addEventListener(event, () => { calibrationDrag = null; });
}
$('saveCalibration').onclick = async () => {
  if (!currentArea || !draftCalibration) return;
  const r = await fetch('/api/calibrate', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({area_id: currentArea.area_id, ...draftCalibration})});
  const saved = await r.json();
  if (!r.ok) { $('f-note').textContent = 'save failed: ' + saved.error; return; }
  currentArea.calibration = {dx: saved.dx, dy: saved.dy};
  draftCalibration = {...currentArea.calibration};
  place();
  $('f-note').textContent = `calibration saved: ${saved.dx}px, ${saved.dy}px`;
};
addEventListener('resize', onResize);
// A plain click on the map closes the inspect drawer; Leaflet fires no click after a drag.
// On a continent it opens the zone under the pointer instead.
map.on('click', (e) => {
  if (calibrating) return;
  if (Walk.pickPoint(e)) return;                  // "walk to a point" is armed (#178)
  if (currentArea && currentArea.continent_view) {
    const z = zoneAt(e.latlng.lng, -e.latlng.lat);
    const a = z && areaFor(z.area_id);
    if (a && a.has_image) return showArea(a);     // only zones the selector offers
  }
  Inspect.close();
});
map.on('contextmenu', zoomOut);                  // also keeps the browser menu away
$('toContinent').onclick = zoomOut;

// ---- UI state persisted in localStorage: sidebar width/collapse, active tab,
// last zone (used above in loadAreas) and pinned character. Every access is
// wrapped in try/catch: private-browsing / disabled storage must not break the page.
const STATE_PREFIX = 'wowmap.';
function loadState(key) {
  try { return JSON.parse(localStorage.getItem(STATE_PREFIX + key)); }
  catch (e) { return null; }
}
function saveState(key, value) {
  try { localStorage.setItem(STATE_PREFIX + key, JSON.stringify(value)); }
  catch (e) { /* storage unavailable, state just won't persist */ }
}

const MIN_ASIDE_W = 220, MAX_ASIDE_W = 560;
function setAsideWidth(w) {
  const clamped = Math.max(MIN_ASIDE_W, Math.min(MAX_ASIDE_W, w));
  document.documentElement.style.setProperty('--sidebar-w', clamped + 'px');
  return clamped;
}
(() => {
  const saved = loadState('asideWidth');
  if (saved) setAsideWidth(saved);
})();
let resizeDrag = null;
$('resize-handle').addEventListener('pointerdown', (e) => {
  resizeDrag = {startX: e.clientX, startW: $('aside').getBoundingClientRect().width};
  $('resize-handle').setPointerCapture(e.pointerId);
});
$('resize-handle').addEventListener('pointermove', (e) => {
  if (!resizeDrag) return;
  setAsideWidth(resizeDrag.startW + (e.clientX - resizeDrag.startX));
  onResize();
});
for (const event of ['pointerup', 'pointercancel']) {
  $('resize-handle').addEventListener(event, () => {
    if (resizeDrag) saveState('asideWidth', $('aside').getBoundingClientRect().width);
    resizeDrag = null;
  });
}

function setCollapsed(collapsed) {
  document.body.classList.toggle('aside-collapsed', collapsed);
  $('expand').hidden = !collapsed;
  saveState('collapsed', collapsed);
  dispatchEvent(new Event('resize'));
}
$('collapse').onclick = () => setCollapsed(true);
$('expand').onclick = () => setCollapsed(false);
if (loadState('collapsed')) setCollapsed(true);

// Tabs: players list vs. chat panel share the sidebar; chat only connects (SSE)
// while its tab is visible, so a viewer who never opens it costs nothing extra.
function setTab(tab) {
  const isChat = tab === 'chat';
  $('tab-players').classList.toggle('on', !isChat);
  $('tab-chat').classList.toggle('on', isChat);
  $('list').hidden = isChat;
  $('search-wrap').hidden = isChat;
  $('chat').hidden = !isChat;
  $('chat-status').hidden = !isChat || Chat.shown();
  saveState('tab', tab);
  if (isChat) Chat.connect(); else Chat.disconnect();
}
$('tab-players').onclick = () => setTab('players');
$('tab-chat').onclick = () => setTab('chat');
setTab(loadState('tab') === 'chat' ? 'chat' : 'players');

// Keyboard: `/` focuses the character search, `c` toggles the chat tab, Esc for
// the drawer is handled by Inspect itself. Ignore these while typing elsewhere.
addEventListener('keydown', (e) => {
  const typing = /^(input|textarea|select)$/i.test(e.target.tagName);
  if (e.key === '/' && !typing) {
    e.preventDefault();
    setTab('players');
    $('search').focus();
  } else if (e.key === 'c' && !typing) {
    setTab($('tab-chat').classList.contains('on') ? 'players' : 'chat');
  }
});
$('search').addEventListener('input', renderList);

// Remember the pinned (open) character across reloads.
document.getElementById('inspect-close').addEventListener('click', () => saveState('pinned', null));
addEventListener('keydown', (e) => { if (e.key === 'Escape') saveState('pinned', null); });
const originalSelectCharacter = selectCharacter;
selectCharacter = (name) => { originalSelectCharacter(name); saveState('pinned', name); };
window.selectCharacter = selectCharacter;
if (!location.hash) {
  const pinned = loadState('pinned');
  if (pinned) Inspect.open(pinned);
}

// draw() loads the zone art; tick() alone only places markers, which left the stage
// an empty grid on first load until the zone was changed.
loadAreas().then(() => { draw(); return tick(); });
setInterval(tick, 5000);
