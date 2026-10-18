// web/src/lib/position.js (the SvelteKit map page) must put markers where static/map.js does.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {fns} = require('./load');

// The helper is an ES module; run it with `export` stripped.
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'web', 'src', 'lib', 'position.js'), 'utf8');
const ctx = vm.createContext({});
vm.runInContext(src.replace(/^export /gm, '') + '\nthis.markerXY = markerXY; this.ll = ll;', ctx);
const plain = (v) => JSON.parse(JSON.stringify(v));

// The old page, with currentArea set per call.
const old = fns('map.js', ['calibration', 'pxOf', 'px'],
  'var MAP_FRAME_W = 1002, MAP_FRAME_H = 668; var calibrating = false, draftCalibration = null, currentArea = null;');

const player = (o) => ({in_world: true, zone: 12, continent: '0', norm_x: 0.25, norm_y: 0.5,
                        cont_x: 0.4, cont_y: 0.6, ...o});

test('zone marker equals map.js px() with the zone calibration', () => {
  for (const calibration of [undefined, {dx: 0, dy: 0}, {dx: 10, dy: -5}]) {
    const area = {area_id: 12, calibration};
    old.currentArea = {calibration};
    for (const [nx, ny] of [[0, 0], [1, 1], [0.25, 0.5], [0.333, 0.777]]) {
      const p = player({norm_x: nx, norm_y: ny});
      assert.deepEqual(plain(ctx.markerXY(p, area)), plain(old.px(p)));
    }
  }
  assert.deepEqual(plain(ctx.markerXY(player({}), {area_id: 12, calibration: {dx: 10, dy: -5}})), [260.5, 329]);
});

test('continent marker is cont_x/cont_y times the frame, no calibration', () => {
  const area = {area_id: '0', continent_view: true, calibration: {dx: 99, dy: 99}};
  assert.deepEqual(plain(ctx.markerXY(player({cont_x: 0.1, cont_y: 0.9}), area)), [100.2, 601.2]);
});

test('players not on the area, offline or without coordinates have no marker', () => {
  assert.equal(ctx.markerXY(player({zone: 99}), {area_id: 12}), null);
  assert.equal(ctx.markerXY(player({in_world: false}), {area_id: 12}), null);
  assert.equal(ctx.markerXY(player({norm_x: null}), {area_id: 12}), null);
  assert.equal(ctx.markerXY(player({continent: '1'}), {area_id: '0', continent_view: true}), null);
  assert.equal(ctx.markerXY(player({cont_x: null}), {area_id: '0', continent_view: true}), null);
});

test('ll is Leaflet LatLng(-y, x)', () => {
  assert.deepEqual(plain(ctx.ll(3, 4)), [-4, 3]);
});
