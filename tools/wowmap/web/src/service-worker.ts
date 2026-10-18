/// <reference types="@sveltejs/kit" />
/// <reference lib="webworker" />
// Caches the app shell (the built files) only. /api, /maps and everything else go to the network.
import { build, files, version } from '$service-worker';

const sw = self as unknown as ServiceWorkerGlobalScope;
const CACHE = `shell-${version}`;
const SHELL = new Set([...build, ...files]);

sw.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll([...SHELL])).then(() => sw.skipWaiting()));
});

sw.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => sw.clients.claim())
  );
});

sw.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== sw.location.origin || !SHELL.has(url.pathname)) return;
  e.respondWith(caches.match(e.request).then((hit) => hit ?? fetch(e.request)));
});
