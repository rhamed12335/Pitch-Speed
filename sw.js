const CACHE = 'mp-v3';
const SHELL = [
  '/Pitch-Speed/',
  '/Pitch-Speed/index.html',
  '/Pitch-Speed/manifest.json',
  '/Pitch-Speed/icon-192.png',
  '/Pitch-Speed/icon-512.png'
];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL).catch(()=>{})));
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if(url.pathname.includes('/api/') || url.hostname==='127.0.0.1'){
    e.respondWith(fetch(e.request).catch(()=>new Response('{"error":"offline"}')));
    return;
  }
  e.respondWith(
    caches.match(e.request).then(cached => {
      if(cached) return cached;
      return fetch(e.request).then(res => {
        if(res && res.status===200 && res.type==='basic'){
          caches.open(CACHE).then(c=>c.put(e.request,res.clone()));
        }
        return res;
      });
    })
  );
});
