const test = require('node:test');
const assert = require('node:assert/strict');
const {fns} = require('./load');

const NOTE = 'Body only: equipment is not drawn';

// A minimal element: enough of the DOM for characterModel (className, textContent, title,
// append, replaceChildren) to run and be inspected.
function fakeElement(tag) {
  return {
    tag, className: '', textContent: '', title: '', children: [],
    append(...xs) { this.children.push(...xs); },
    replaceChildren(...xs) { this.children = xs; },
  };
}

// Loads characterModel (and the el helper it uses) out of the Inspect IIFE in character.js.
// `mount` is the stub window.CharView.mount: it decides whether the 3D model draws.
function loadModel(mount) {
  const f = fns('character.js', ['el', 'characterModel'], '', {
    document: {createElement: fakeElement},
    window: mount === undefined ? {} : {CharView: {mount}},
  });
  return f.characterModel;
}

// Let the mount promise settle.
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test('with a model, the viewer box gets the body-only caption once it mounts', async () => {
  const model = loadModel((box, base) => {
    assert.equal(base, '/models/1_0');
    box.append(fakeElement('canvas'));
    return Promise.resolve();
  });
  const box = model({model: '/models/1_0'});
  await settle();
  assert.ok(box.children.some((k) => k.className === 'model-note' && k.textContent === NOTE));
  assert.match(box.title, /Drag to rotate, scroll to zoom\. Body only: equipment is not drawn\./);
});

test('without a model, the box has no caption', () => {
  const box = loadModel(() => Promise.resolve())({model: null});
  assert.equal(box.children[0].textContent, 'No 3D model extracted');
  assert.equal(box.children.some((k) => k.className === 'model-note'), false);
});

test('when the viewer is missing or the model fails, the box has no caption', async () => {
  const unavailable = loadModel(undefined)({model: '/models/1_0'});
  assert.equal(unavailable.children[0].textContent, '3D viewer unavailable');
  const failed = loadModel(() => Promise.reject(new Error('no WebGL')))({model: '/models/1_0'});
  await settle();
  assert.equal(failed.children[0].textContent, '3D model unavailable');
  assert.equal(failed.children.some((k) => k.className === 'model-note'), false);
});
