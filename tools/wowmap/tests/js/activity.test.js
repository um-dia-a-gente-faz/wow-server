const test = require('node:test');
const assert = require('node:assert/strict');
const {fns} = require('./load');

const f = fns('activity.js', ['sourceLabel']);

test('sourceLabel names each source of a feed event, agent events included', () => {
  assert.equal(f.sourceLabel('agent'), 'agent event');
  assert.equal(f.sourceLabel('db'), 'database');
  assert.equal(f.sourceLabel('chat'), 'public chat');
  assert.equal(f.sourceLabel('audit'), 'agent log');
  assert.equal(f.sourceLabel('other'), 'other');
});
