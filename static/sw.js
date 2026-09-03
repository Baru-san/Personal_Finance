// Ledger service worker — makes the app load instantly from cache on a bad
// connection and lets already-visited pages work fully offline.
//
// Strategy:
//   - App shell (CSS/JS/fonts/icons): cache-first, precached on install.
//   - Page navigations (dashboard, ledger, journal, ...): network-first, so
//     figures are fresh when online, falling back to the last-cached copy
//     (or static/offline.html) when not.
//   - Never intercepted: non-GET requests. Writes always hit the network
//     directly; static/offline.js is what queues them when that fails.
//
// Bump CACHE_VERSION to invalidate everything precached below.
const CACHE_VERSION = 'v4';
const CACHE_NAME = 'ledger-' + CACHE_VERSION;

const PRECACHE_URLS = [
  '/static/style.css',
  '/static/chart.js',
  '/static/offline.js',
  '/static/manifest.json',
  '/static/offline.html',
  '/static/fonts/vt323-400.woff2',
  '/static/fonts/press-start-2p-400.woff2',
  '/static/fonts/space-mono-400.woff2',
  '/static/fonts/space-mono-700.woff2',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-maskable-512.png',
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(function (cache) { return cache.addAll(PRECACHE_URLS); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(
          keys.filter(function (key) { return key !== CACHE_NAME; })
              .map(function (key) { return caches.delete(key); })
        );
      })
      .then(function () { return self.clients.claim(); })
  );
});

function networkFirst(req) {
  return fetch(req)
    .then(function (res) {
      // Only cache a real, direct 200 — never a redirect (e.g. the
      // unauthenticated bounce to /login) or an error page. Caching those
      // under the dashboard's URL would make the login form the permanent
      // offline fallback for every page.
      if (res.ok && !res.redirected) {
        var copy = res.clone();
        caches.open(CACHE_NAME).then(function (cache) { cache.put(req, copy); });
      }
      return res;
    })
    .catch(function () {
      return caches.match(req).then(function (cached) {
        return cached || caches.match('/static/offline.html');
      });
    });
}

function cacheFirst(req) {
  return caches.match(req).then(function (cached) {
    if (cached) return cached;
    return fetch(req).then(function (res) {
      var copy = res.clone();
      caches.open(CACHE_NAME).then(function (cache) { cache.put(req, copy); });
      return res;
    });
  });
}

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') return;

  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (req.mode === 'navigate') {
    event.respondWith(networkFirst(req));
  } else if (url.pathname.startsWith('/static/')) {
    event.respondWith(cacheFirst(req));
  }
});
