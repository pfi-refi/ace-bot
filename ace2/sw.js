/* ACE 2.0 service worker.
   NETWORK-FIRST for the SHELL, cache as offline fallback. A zero-build app has no
   filename hashing, and cache-first proved un-updatable in practice: a version bump
   racing a deploy could fill the new bucket with STALE files (the "reload twice —
   or forever" bug, seen live on v34). Shell files are tiny; fetching them fresh on
   every online load costs ~nothing and guarantees every deploy is what users see.
   The cache is only consulted when the network fails — true offline still works.
   API data (/bootstrap, /chat, /tts, /history, /memory, …) and the WebSocket are
   never cached — stale calendar data is worse than none in a command center. */

const CACHE_VERSION = 'ace2-shell-v35';   // v35: NETWORK-FIRST shell — deploys always land; atmosphere + rail + viewfinder
const SHELL = ['/', '/styles.css', '/app.js', '/manifest.json',
               '/icon-192.png', '/icon-512.png', '/icon-maskable.png', '/icon-180.png'];

self.addEventListener('install', (e) => {
  // Seed the offline fallback from the NETWORK (cache:'reload' bypasses the browser's
  // HTTP cache). Best-effort: install must not fail if one icon 404s.
  e.waitUntil(caches.open(CACHE_VERSION)
    .then((c) => Promise.allSettled(
      SHELL.map((u) => fetch(new Request(u, { cache: 'reload' }))
        .then((res) => { if (res.ok) return c.put(u, res); }))))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;                    // never cache POSTs (/chat, /tts…)
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;     // third-party: pass through

  const isShell = SHELL.includes(url.pathname) || req.mode === 'navigate';
  if (!isShell) return;                                // API + everything else → network (default)

  // NETWORK-FIRST: fresh when online (and refresh the fallback copy); cache when offline.
  e.respondWith(
    fetch(req).then((res) => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE_VERSION).then((c) => c.put(url.pathname, copy));
      }
      return res;
    }).catch(() => caches.match(url.pathname === '/' || req.mode === 'navigate' ? '/' : url.pathname))
  );
});
