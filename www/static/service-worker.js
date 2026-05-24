// WineTone — minimal service worker.
//
// Goals:
//   1. Cache static assets (CSS, favicon, icons) so repeat loads are instant.
//   2. Network-first for all app routes — we don't want stale auth/dashboard
//      content served from cache.
//   3. Don't break offline-detection in the browser — we let the browser
//      handle offline UIs.
//
// This is intentionally simple. If we ever need offline-first behavior
// (e.g., to view your cached labels while offline), it deserves a real
// design pass — not a one-line extension of this.

// Bump this whenever a cached asset under /static/ changes — the
// activate handler purges every cache whose name doesn't match this,
// so a returning user gets fresh CSS/JS on their next page load.
const CACHE_NAME = 'winetone-static-v9';
const STATIC_ASSETS = [
  '/static/style.css',
  '/static/favicon.svg',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/apple-touch-icon.png',
  '/static/manifest.json',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  // Clean up old caches when the SW version changes.
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  // Only intercept GETs to our own origin.
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/static/')) {
    // Cache-first for static assets.
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request).then((resp) => {
        // Only cache 200 OK responses.
        if (resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(request, copy));
        }
        return resp;
      }))
    );
  } else {
    // Network-first for app routes. Don't cache HTML — it's user-specific.
    // Just pass through so the browser handles errors normally.
    event.respondWith(fetch(request));
  }
});
