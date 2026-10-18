// web/src/lib/lastseen.js: the offline label's relative time.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'web', 'src', 'lib', 'lastseen.js'), 'utf8');
const ctx = vm.createContext({});
vm.runInContext(src.replace(/^export /gm, '') + '\nthis.lastSeenText = lastSeenText;', ctx);
const at = (secondsAgo) => ctx.lastSeenText(1000000 - secondsAgo, 1000000);

test('under a minute is just now', () => {
  assert.equal(at(0), 'just now');
  assert.equal(at(59), 'just now');
  assert.equal(at(-5), 'just now');   // clock skew: logout slightly in the future
});

test('minutes up to 59', () => {
  assert.equal(at(60), '1 min ago');
  assert.equal(at(300), '5 min ago');
  assert.equal(at(3599), '59 min ago');
});

test('hours up to 23', () => {
  assert.equal(at(3600), '1 h ago');
  assert.equal(at(3 * 3600 + 59), '3 h ago');
  assert.equal(at(86399), '23 h ago');
});

test('days from 24 h', () => {
  assert.equal(at(86400), '1 d ago');
  assert.equal(at(2 * 86400), '2 d ago');
  assert.equal(at(7 * 86400), '7 d ago');
});
