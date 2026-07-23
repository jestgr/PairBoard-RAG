/* ============================================================
   DocQA Service Worker

   Two jobs:
   1. Cache the app shell (offline launch).
   2. Inject COOP/COEP headers so SharedArrayBuffer is available,
      which lets wllama run multi-threaded CPU inference. GitHub
      Pages cannot set headers, so the SW adds them to responses.
      'credentialless' is used rather than 'require-corp' so
      cross-origin fetches (CDN, Hugging Face model files) still work.

   Model weights are NOT cached here — wllama manages its own model
   cache, so a model downloads once and reloads from disk after that.

   IMPORTANT: bump CACHE_VERSION on every deploy or installed users
   keep the stale shell.
   ============================================================ */
const CACHE_VERSION = 'docqa-v20';   // <- bump this on every deploy
const SHELL = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
];

// Escape hatch: load any page with ?nocoi=1 to disable header injection
// if COEP ever blocks a resource.
let COI_ENABLED = true;

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then(cache => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('message', e => {
  if (e.data && e.data.type === 'disable-coi') COI_ENABLED = false;
});

// Add cross-origin isolation headers to same-origin responses.
function withCOI(response) {
  if (!COI_ENABLED || !response || response.status === 0) return response;
  const headers = new Headers(response.headers);
  headers.set('Cross-Origin-Embedder-Policy', 'credentialless');
  headers.set('Cross-Origin-Opener-Policy', 'same-origin');
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  if (url.searchParams.get('nocoi') === '1') COI_ENABLED = false;

  // Cross-origin (CDN bundles, Hugging Face model files) - pass straight through.
  if (url.origin !== self.location.origin || event.request.method !== 'GET') return;

  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return withCOI(cached);
      return fetch(event.request).then(resp => {
        const copy = resp.clone();
        caches.open(CACHE_VERSION).then(c => c.put(event.request, copy)).catch(() => {});
        return withCOI(resp);
      }).catch(() => cached);
    })
  );
});
