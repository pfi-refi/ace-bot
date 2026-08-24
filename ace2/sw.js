/* ACE 2.0 service worker.
   NETWORK-FIRST for the SHELL, cache as offline fallback. A zero-build app has no
   filename hashing, and cache-first proved un-updatable in practice: a version bump
   racing a deploy could fill the new bucket with STALE files (the "reload twice —
   or forever" bug, seen live on v34). Shell files are tiny; fetching them fresh on
   every online load costs ~nothing and guarantees every deploy is what users see.
   The cache is only consulted when the network fails — true offline still works.
   API data (/bootstrap, /chat, /tts, /history, /memory, …) and the WebSocket are
   never cached — stale calendar data is worse than none in a command center. */

const CACHE_VERSION = 'ace2-shell-v38';   // v38: sticky scroll + collapsible receipts; v37 timestamps/capture
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

/* PHONE PUSH — how Ace reaches Brady with the app CLOSED. The backend sends a JSON
   payload {title, body, url}; anything unparseable still shows as plain text rather
   than being dropped, because a push that shows NOTHING makes iOS substitute its own
   "This website has been updated in the background" notice — worse than a bare line.
   Dormant unless the server has VAPID keys and a subscription exists: no subscription,
   no push event, and this handler simply never runs. */
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; }
  catch (err) { d = { body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'ACE', {
    body: d.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: d.tag || 'ace',            // a newer brief replaces the older one instead of stacking
    data: { url: d.url || '/' },
  }));
});

/* Tapping it opens the command center: focus the HUD if it's already running (iOS keeps
   an installed PWA alive in the background), otherwise launch it. */
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if ('focus' in c) {
          if (url !== '/' && 'navigate' in c) { try { c.navigate(url).catch(() => {}); } catch (err) {} }
          return c.focus();
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
