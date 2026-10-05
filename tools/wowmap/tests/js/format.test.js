const test = require('node:test');
const assert = require('node:assert/strict');
const {fns} = require('./load');

let nowMs = 0;
const f = fns('format.js', ['placeText', 'mapCoordsText', 'playerTip', 'worldText', 'ago'], '',
  {Date: {now: () => nowMs}});

test('placeText joins what is known with a chevron', () => {
  assert.equal(f.placeText({continent_name: 'Kalimdor', zone_name: 'Durotar', subzone_name: null}),
    'Kalimdor › Durotar');
  assert.equal(f.placeText({}), '');
});

test('mapCoordsText is empty without coordinates', () => {
  assert.equal(f.mapCoordsText({}), '');
  assert.equal(f.mapCoordsText({map_coords: {x: 45.123, y: 6}}), '45.1, 6.0');
});

test('playerTip: name, class, race, level, place and coordinates', () => {
  const p = {name: 'Aria', class_name: 'Mage', race_name: 'Human', level: 12,
    zone_name: 'Elwynn Forest', map_coords: {x: 1, y: 2}};
  assert.equal(f.playerTip(p), 'Aria — Mage Human lvl 12\nElwynn Forest\n1.0, 2.0');
  delete p.map_coords;
  assert.equal(f.playerTip(p), 'Aria — Mage Human lvl 12\nElwynn Forest');
});

test('worldText has one decimal per axis', () => {
  assert.equal(f.worldText(1, 2.25, -3), '1.0, 2.3, -3.0');
});

test('ago picks the largest unit and never goes negative', () => {
  nowMs = 1_000_000 * 1000;
  assert.equal(f.ago(1_000_000 + 5), '0s ago');          // future clamps to 0
  assert.equal(f.ago(1_000_000 - 59), '59s ago');
  assert.equal(f.ago(1_000_000 - 120), '2m ago');
  assert.equal(f.ago(1_000_000 - 7200), '2h ago');
  assert.equal(f.ago(1_000_000 - 3 * 86400), '3d ago');
});
