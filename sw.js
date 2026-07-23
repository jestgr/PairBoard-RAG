/* ============================================================
   DocQA Service Worker
   Caches the app shell only. Deliberately does NOT cache:
     - WebLLM / transformers.js CDN bundles (huge, cross-origin)
     - Model weights (the browser's own cache handles these)
   IMPORTANT: bump CACHE_VERSION on every deploy or installed
   users keep the stale shell.
   ============================================================ */
const CACHE_VERSION = 'docqa-v11';   // ← bump this on every deploy
const SHELL = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
];

// Install — pre-cache the shell
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then(cache => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

// Activate — drop old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch strategy:
//   - Same-origin shell  → cache-first (works fully offline)
//   - Everything else    → straight to network (CDN, model weights)
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Only handle same-origin GET requests; let CDN/model traffic pass through
  if (url.origin !== self.location.origin || event.request.method !== 'GET') {
    return;  // default browser handling
  }

  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request).then(resp => {
        // Cache newly-fetched same-origin assets (e.g. icons) for next time
        const copy = resp.clone();
        caches.open(CACHE_VERSION).then(c => c.put(event.request, copy)).catch(()=>{});
        return resp;
      }).catch(() => cached);  // offline + not cached → fail gracefully
    })
  );
});
