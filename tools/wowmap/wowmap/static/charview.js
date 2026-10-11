// #171: the inspect drawer's 3D character. Vendored three.js (static/three.min.js,
// loaded on first use) draws the body mesh + skin that extract_models.py wrote to
// MODELS_DIR. Drag to rotate, wheel to zoom. Draws on demand, no animation loop.
// No CDN, no build step. Mesh format: models.py.
(function () {
  'use strict';
  let threeLoad = null;
  let current = null;   // one WebGL context, re-attached to each new drawer render

  function loadThree() {
    if (window.THREE) return Promise.resolve();
    if (!threeLoad) {
      threeLoad = new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = '/static/three.min.js';
        s.onload = resolve;
        s.onerror = () => { threeLoad = null; reject(new Error('three.js did not load')); };
        document.head.append(s);
      });
    }
    return threeLoad;
  }

  function parseBlob(buf) {
    const dv = new DataView(buf);
    if (buf.byteLength < 16 || dv.getUint32(0, true) !== 0x4c444d57 || dv.getUint32(4, true) !== 1) {
      throw new Error('not a model file');
    }
    const nv = dv.getUint32(8, true), ni = dv.getUint32(12, true);
    if (buf.byteLength !== 16 + nv * 32 + ni * 2) throw new Error('truncated model file');
    const f = new Float32Array(buf, 16, nv * 8);
    const pos = new Float32Array(nv * 3), nor = new Float32Array(nv * 3), uv = new Float32Array(nv * 2);
    for (let i = 0; i < nv; i++) {
      // M2 space is x forward, y left, z up; three.js is y up with +z towards the camera.
      pos.set([f[i * 8 + 1], f[i * 8 + 2], f[i * 8]], i * 3);
      nor.set([f[i * 8 + 4], f[i * 8 + 5], f[i * 8 + 3]], i * 3);
      uv.set([f[i * 8 + 6], f[i * 8 + 7]], i * 2);
    }
    return {pos, nor, uv, index: new Uint16Array(buf.slice(16 + nv * 32))};
  }

  function build(base) {
    return Promise.all([
      loadThree(),
      fetch(base + '.bin').then((r) => { if (!r.ok) throw new Error('no model'); return r.arrayBuffer(); }),
    ]).then(([, buf]) => {
      const T = window.THREE, m = parseBlob(buf);
      const geo = new T.BufferGeometry();
      geo.setAttribute('position', new T.BufferAttribute(m.pos, 3));
      geo.setAttribute('normal', new T.BufferAttribute(m.nor, 3));
      geo.setAttribute('uv', new T.BufferAttribute(m.uv, 2));
      geo.setIndex(new T.BufferAttribute(m.index, 1));
      geo.computeBoundingBox();
      const box = geo.boundingBox, mid = box.getCenter(new T.Vector3()), size = box.getSize(new T.Vector3());

      const wrap = document.createElement('div');
      wrap.className = 'charview';
      const renderer = new T.WebGLRenderer({antialias: true, alpha: true});
      renderer.setPixelRatio(window.devicePixelRatio || 1);
      wrap.append(renderer.domElement);

      const material = new T.MeshLambertMaterial({color: 0x888888, side: T.DoubleSide});
      new T.TextureLoader().load(base + '.png', (tex) => {
        tex.flipY = false;                       // M2 uv origin is the image's top left
        tex.colorSpace = T.SRGBColorSpace;
        tex.anisotropy = 4;
        material.map = tex; material.color.set(0xffffff); material.needsUpdate = true; draw();
      }, undefined, () => {});
      const mesh = new T.Mesh(geo, material);
      mesh.position.sub(mid);
      const pivot = new T.Group();
      pivot.add(mesh);
      const scene = new T.Scene();
      scene.add(pivot, new T.AmbientLight(0xffffff, 0.9));
      const sun = new T.DirectionalLight(0xffffff, 1.4);
      sun.position.set(1, 2, 3);
      scene.add(sun);

      const camera = new T.PerspectiveCamera(30, 1, 0.1, 100);
      const fit = size.y / 2 / Math.tan(15 * Math.PI / 180) * 1.15;
      const view = {yaw: 0.4, dist: fit, min: fit * 0.35, max: fit * 1.6};
      function draw() {
        pivot.rotation.y = view.yaw;
        camera.position.set(0, 0, view.dist);
        renderer.render(scene, camera);
      }
      function resize() {
        const w = wrap.clientWidth, h = wrap.clientHeight;
        if (!w || !h) return;
        renderer.setSize(w, h, false);
        camera.aspect = w / h; camera.updateProjectionMatrix(); draw();
      }
      let drag = null;
      const cv = renderer.domElement;
      cv.addEventListener('pointerdown', (e) => { drag = e.clientX; cv.setPointerCapture(e.pointerId); });
      cv.addEventListener('pointermove', (e) => {
        if (drag === null) return;
        view.yaw += (e.clientX - drag) * 0.01; drag = e.clientX; draw();
      });
      const end = () => { drag = null; };
      cv.addEventListener('pointerup', end); cv.addEventListener('pointercancel', end);
      cv.addEventListener('wheel', (e) => {
        e.preventDefault();
        view.dist = Math.min(view.max, Math.max(view.min, view.dist * Math.exp(e.deltaY * 0.001)));
        draw();
      }, {passive: false});
      new ResizeObserver(resize).observe(wrap);
      return {el: wrap, resize, dispose() { renderer.dispose(); geo.dispose(); material.dispose(); }};
    });
  }

  // mount(container, base): `base` is the API's `model` (e.g. "/models/1_0"). Resolves
  // when drawn; rejects (the caller keeps a placeholder) if the model or WebGL is missing.
  function mount(container, base) {
    if (current && current.base === base) {
      container.append(current.view.el); current.view.resize();
      return Promise.resolve();
    }
    return build(base).then((view) => {
      if (current) current.view.dispose();
      current = {base, view};
      container.append(view.el); view.resize();
    });
  }

  window.CharView = {mount, parseBlob};
})();
