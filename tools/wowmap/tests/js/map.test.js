const test = require('node:test');
const assert = require('node:assert/strict');

// Results come from another vm realm, so compare plain copies.
const plain = (v) => JSON.parse(JSON.stringify(v));
const {fns} = require('./load');

const PRELUDE = `
  var MAP_FRAME_W = 1002, MAP_FRAME_H = 668;
  var calibrating = false, draftCalibration = null, currentArea = null;
`;
const m = fns('map.js', ['calibration', 'pxOf', 'px', 'onZoneLand', 'zoneAt', 'areaFor'], PRELUDE
  + 'var areas = [];');

test('pxOf scales normalised coords to the zone art frame', () => {
  assert.deepEqual(plain(m.pxOf(0, 0)), [0, 0]);
  assert.deepEqual(plain(m.pxOf(1, 1)), [1002, 668]);
  assert.deepEqual(plain(m.px({norm_x: 0.5, norm_y: 0.25})), [501, 167]);
});

test('pxOf applies the zone calibration at the last step', () => {
  m.currentArea = {calibration: {dx: 10, dy: -5}};
  assert.deepEqual(plain(m.pxOf(0.5, 0.5)), [511, 329]);
});

test('a draft calibration wins only while calibrating', () => {
  m.currentArea = {calibration: {dx: 10, dy: 0}};
  m.draftCalibration = {dx: 99, dy: 99};
  assert.deepEqual(plain(m.pxOf(0, 0)), [10, 0]);
  m.calibrating = true;
  assert.deepEqual(plain(m.pxOf(0, 0)), [99, 99]);
});

test('zoneAt prefers the zone whose land is under the pointer, else the nearest centre', () => {
  m.currentArea = {subzones: [
    {area_id: 1, hit: [0, 0, 100, 100]},
    {area_id: 2, hit: [50, 0, 100, 100]},
  ]};
  assert.equal(m.zoneAt(10, 10).area_id, 1);
  assert.equal(m.zoneAt(140, 10).area_id, 2);
  assert.equal(m.zoneAt(60, 50).area_id, 1);   // in both boxes: nearest centre wins
  assert.equal(m.zoneAt(90, 50).area_id, 2);
});
