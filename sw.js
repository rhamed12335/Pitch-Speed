/* Money Printer SW — v5 — never caches HTML so fixes always load */
const CACHE = 'mp-v5';

self.addEventListener('install', e => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API calls — always network
  if(url.pathname.includes('/api/') || url.hostname === '127.0.0.1'){
    e.respondWith(fetch(e.request).catch(() => new Response('{"error":"offline"}')));
    return;
  }

  // HTML files — ALWAYS fetch fresh, never serve from cache
  if(e.request.destination === 'document' || url.pathname.endsWith('.html') || url.pathname.endsWith('/')){
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }

  // Everything else (icons, fonts) — cache first
  e.respondWith(
    caches.match(e.request).then(cached => {
      if(cached) return cached;
      return fetch(e.request).then(res => {
        if(res && res.status === 200){
          caches.open(CACHE).then(c => c.put(e.request, res.clone()));
        }
        return res;
      });
    })
  );
});
