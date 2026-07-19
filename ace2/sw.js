/* ACE 2.0 service worker.
   Cache-first for the SHELL only; network for everything else. A zero-build app
   has no filename hashing, so without CACHE_VERSION + an activate cleanup it
   becomes un-updatable — bump CACHE_VERSION on any shell change to force refresh.
   API data (/bootstrap, /chat, /tts, /history, /memory, …) and the WebSocket are
   never cached — stale calendar data is worse than none in a command center. */

const CACHE_VERSION = 'ace2-shell-v16';   // v16: voice/chat mode toggle + preloaded chat history + confirm guardrails
const SHELL = ['/', '/styles.css', '/app.js', '/manifest.json',
               '/icon-192.png', '/icon-512.png', '/icon-maskable.png', '/icon-180.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE_VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
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

  // Shell paths only → cache-first (offline-capable UI).
  const isShell = SHELL.includes(url.pathname);
  if (!isShell) return;                                // API + everything else → network (default)

  e.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
      return res;
    }).catch(() => hit))
  );
});
