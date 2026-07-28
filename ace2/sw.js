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
  // HTTP cache). Best-effort per file — but only take over (skipWaiting) if the shell
  // root actually seeded, so a failed install can't promote an empty cache.
  e.waitUntil(caches.open(CACHE_VERSION)
    .then((c) => Promise.allSettled(
      SHELL.map((u) => fetch(new Request(u, { cache: 'reload' }))
        .then((res) => { if (res.ok) return c.put(u, res); })))
      .then(() => c.match('/')))
    .then((rootSeeded) => { if (rootSeeded) return self.skipWaiting(); }));
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

  // NETWORK-FIRST: fresh when online (and refresh the fallback copy). Fall back to cache
  // when the network fails, returns a server error (mid-deploy 502s), or is slower than
  // ~3.5s on a warm cache — so the app opens instantly even on a flaky radio.
  const cacheKey = (req.mode === 'navigate' || url.pathname === '/') ? '/' : url.pathname;
  const net = fetch(req);
  e.respondWith(
    Promise.race([
      net,
      new Promise((_, reject) => setTimeout(() => reject(new Error('sw-timeout')), 3500)),
    ]).then((res) => {
      if (!res.ok) {
        return caches.match(cacheKey).then((hit) => hit || res);   // 502 mid-deploy → cached shell
      }
      const copy = res.clone();
      caches.open(CACHE_VERSION).then((c) => c.put(url.pathname, copy));
      return res;
    }).catch(() => caches.match(cacheKey).then((hit) => hit || net))
  );
});
