/* ─── Money Printer Service Worker ─── */
const CACHE = 'money-printer-v1';

/* Files to cache on install — these make the shell load offline */
const SHELL = [
  '/',
  '/index.html',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
  'https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,300;0,400;0,500;1,400&family=Barlow+Condensed:wght@400;600;700;800;900&family=Inter:wght@400;500;600;700&display=swap'
];

/* Install — cache the app shell */
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(SHELL))
  );
  self.skipWaiting();
});

/* Activate — delete old caches */
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

/* Fetch — network first for API calls, cache first for shell */
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  /* Always go to network for your API (Statcast data) */
  if (url.pathname.startsWith('/api/') || url.hostname === '127.0.0.1') {
    event.respondWith(fetch(event.request).catch(() =>
      new Response(JSON.stringify({ error: 'Offline — no live data available' }), {
        headers: { 'Content-Type': 'application/json' }
      })
    ));
    return;
  }

  /* Google Fonts — network first, fallback to cache */
  if (url.hostname.includes('googleapis.com') || url.hostname.includes('gstatic.com')) {
    event.respondWith(
      fetch(event.request)
        .then(res => { caches.open(CACHE).then(c => c.put(event.request, res.clone())); return res; })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  /* App shell — cache first, then network */
  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request).then(res => {
        if (res && res.status === 200) {
          caches.open(CACHE).then(c => c.put(event.request, res.clone()));
        }
        return res;
      });
    })
  );
});
