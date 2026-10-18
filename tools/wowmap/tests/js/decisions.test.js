// web/src/lib/decisions.js: one row of the Character tab's decision log.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'web', 'src', 'lib', 'decisions.js'), 'utf8');
const ctx = vm.createContext({});
vm.runInContext(src.replace(/^export /gm, '') + '\nthis.decisionRow = decisionRow;', ctx);
const row = (o) => ctx.decisionRow({action: 'loot', args: {}, ok: true, error: null, confidence: 0.914, reason: null, ...o});

test('confidence is a rounded percentage, empty when unknown', () => {
  assert.equal(row({}).confidence, '91%');
  assert.equal(row({confidence: null}).confidence, '');
});

test('a failed action is marked and shows its error when there is no reason', () => {
  const r = row({ok: false, error: 'boom'});
  assert.equal(r.failed, true);
  assert.equal(r.reason, 'boom');
  assert.equal(row({}).failed, false);
  assert.equal(row({reason: 'closest'}).reason, 'closest');
});

test('args are shown short', () => {
  assert.equal(row({args: {guid: 7}}).action, 'loot (guid=7)');
  assert.ok(row({args: {text: 'x'.repeat(100)}}).action.length < 60);
  assert.equal(row({action: null}).action, 'none');
});
