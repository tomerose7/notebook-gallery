// Service worker: lets the gallery install as an app and reopen offline.
// Page + data: network first (so updates show up), falling back to cache.
// Image packs (t*/f*): cache first; each pack is cached the first time it is viewed.
const VERSION = 'v14';
const SHELL = ['./', 'index.html', 'upload.html', 'data.js', 'manifest.webmanifest', 'icon-192.png', 'icon-512.png'];

self.addEventListener('install', e => {
  // cache: 'reload' so the new version never stores a stale copy from the browser's HTTP cache
  e.waitUntil(caches.open('shell-' + VERSION).then(c => c.addAll(SHELL.map(u => new Request(u, {cache: 'reload'}))))
    .then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(k => k.startsWith('shell-') && k !== 'shell-' + VERSION).map(k => caches.delete(k))
  )).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  const isPack = /\/[tf]\d+\.(jpg|txt)$/.test(url.pathname);
  if (isPack) {
    e.respondWith(caches.open('packs').then(async c => {
      const hit = await c.match(e.request);
      if (hit) return hit;
      const res = await fetch(e.request);
      if (res.ok) c.put(e.request, res.clone());
      return res;
    }));
  } else {
    // no-cache: always ask the server whether the page changed (GitHub Pages sends max-age=600, so a plain
    // fetch could serve a 10-minute-old copy after an update). Unchanged files still come back as a cheap 304.
    e.respondWith(fetch(e.request.url, {cache: 'no-cache', credentials: 'same-origin'}).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open('shell-' + VERSION).then(c => c.put(e.request, copy)); }
      return res;
    }).catch(() => caches.match(e.request).then(r => r || caches.match('index.html'))));
  }
});
